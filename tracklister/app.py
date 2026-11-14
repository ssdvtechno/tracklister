"""Desktop window + the bridge the HTML UI calls (window.pywebview.api.*)."""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, config, elsewhere, parser, source
from .spotify import REDIRECT_URI, Auth, Client, SpotifyError

log = logging.getLogger(__name__)

URL_RE = re.compile(r"^\s*(https?://|www\.|youtu\.?be|m\.youtube|soundcloud\.com|mixcloud\.com)\S*\s*$", re.I)


def _ui_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "tracklister" / "ui"


class Api:
    def __init__(self):
        self.auth = Auth()
        self.sp = Client(self.auth)
        self.jobs: dict[str, dict] = {}

    # ------------------------------------------------------------- account / setup

    def state(self) -> dict:
        cfg = config.load()
        user = None
        if self.auth.logged_in():
            try:
                user = self.sp.me()
            except SpotifyError as exc:
                log.warning("me(): %s", exc)
        return {"client_id": cfg.get("client_id", ""), "logged_in": bool(user), "user": user,
                "redirect_uri": REDIRECT_URI}

    def login(self, client_id: str) -> dict:
        try:
            self.auth.start_login(client_id)
            return {"ok": True}
        except SpotifyError as exc:
            return {"ok": False, "error": str(exc)}

    def login_status(self) -> dict:
        out = {"status": self.auth.status, "error": self.auth.error}
        if self.auth.status == "ok":
            try:
                out["user"] = self.sp.me()
            except SpotifyError as exc:
                out = {"status": "error", "error": _friendly(exc)}
        return out

    def logout(self) -> None:
        self.auth.logout()
        self.auth.status = "idle"

    def reset_setup(self) -> None:
        cfg = config.load()
        for k in ("client_id", "token", "user"):
            cfg.pop(k, None)
        config.save(cfg)
        self.auth.status = "idle"

    # ------------------------------------------------------------- helpers

    def open(self, url: str) -> None:
        if url.startswith(("https://", "spotify:")):
            webbrowser.open(url)

    def copy(self, text: str) -> bool:
        try:
            if sys.platform == "darwin":
                subprocess.run(["pbcopy"], input=text.encode(), check=True)
            elif sys.platform == "win32":  # BOM makes clip.exe read UTF-16; no console flash
                subprocess.run(["clip"], input=("\ufeff" + text).encode("utf-16le"), check=True,
                               creationflags=0x08000000)
            else:
                subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=True)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------- the main job

    def start(self, text: str) -> str:
        job_id = uuid.uuid4().hex[:8]
        self.jobs[job_id] = {"phase": "loading", "message": "Starting…", "info": None, "where": "",
                             "tracks": [], "done": 0, "error": None}
        threading.Thread(target=self._run, args=(job_id, text.strip()), daemon=True).start()
        return job_id

    def poll(self, job_id: str) -> dict:
        return self.jobs.get(job_id) or {"phase": "error", "error": "Unknown job"}

    def _run(self, job_id: str, text: str) -> None:
        job = self.jobs[job_id]
        try:
            lines = [ln for ln in text.splitlines() if ln.strip()]
            if lines and URL_RE.match(lines[0]) and len(lines) == 1:
                info, tracks, where = source.find_tracklist(lines[0], lambda m: job.update(message=m))
                url_info = {"title": info.title, "uploader": info.uploader, "thumbnail": info.thumbnail, "url": info.url}
            else:
                url_info = {"title": "My set", "uploader": "", "thumbnail": None, "url": ""}
                if lines and URL_RE.match(lines[0]):  # link + pasted tracklist below it
                    try:
                        info, _ = source.fetch(lines[0])
                        url_info = {"title": info.title, "uploader": info.uploader,
                                    "thumbnail": info.thumbnail, "url": info.url}
                    except source.SourceError:
                        pass
                    text = "\n".join(lines[1:])
                tracks, where = parser.parse(text, loose=True), "pasted"
            job.update(info=url_info, where=where)
            if not tracks:
                job.update(phase="error", error="No tracklist found. Paste the tracklist into the box "
                                                "(one track per line, like “Artist – Title”).")
                return
            dj = parser.guess_dj(url_info["title"])
            job["tracks"] = [{**t.to_dict(), "i": i, "status": "pending", "alternatives": [], "pick": None,
                              "links": elsewhere.search_links(t), "elsewhere": [], "ew": "none"}
                             for i, t in enumerate(tracks)]
            job.update(phase="matching", message=f"Finding {len(tracks)} tracks on Spotify…")

            lock = threading.Lock()

            def one(i: int):
                row = job["tracks"][i]
                try:
                    row.update(self.sp.match(tracks[i], dj))
                except SpotifyError as exc:
                    row.update(status="missing", error=_friendly(exc))
                except Exception as exc:  # network etc.
                    row.update(status="missing", error=str(exc))
                with lock:
                    job["done"] += 1

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(one, range(len(tracks))))

            # Not (surely) on Spotify: look on Beatport, Bandcamp, SoundCloud, YouTube, Apple Music, Deezer.
            todo = [i for i, r in enumerate(job["tracks"]) if r["status"] in ("missing", "maybe")]
            if todo:
                for i in todo:
                    job["tracks"][i]["ew"] = "pending"
                job.update(phase="elsewhere", ew_done=0, ew_total=len(todo),
                           message=f"Looking for {len(todo)} tracks on other services…")

                def other(i: int):
                    job["tracks"][i].update(self._elsewhere(tracks[i], dj, job["tracks"][i]))
                    with lock:
                        job["ew_done"] += 1

                with ThreadPoolExecutor(max_workers=3) as pool:
                    list(pool.map(other, todo))
            job["phase"] = "review"
        except source.SourceError as exc:
            job.update(phase="error", error=str(exc))
        except SpotifyError as exc:
            job.update(phase="error", error=_friendly(exc))
        except Exception as exc:
            log.exception("job failed")
            job.update(phase="error", error=f"Unexpected problem: {exc}")

    def _elsewhere(self, track: parser.Track, dj: str | None, row: dict | None = None) -> dict:
        """Other-service hits, plus a Spotify rescue via ISRC when one of them is a sure match."""
        try:
            res = elsewhere.find(track, dj)
        except Exception as exc:
            log.warning("elsewhere failed for %s: %s", track.display, exc)
            return {"elsewhere": [], "links": elsewhere.search_links(track), "ew": "done"}
        out = {"elsewhere": res["hits"], "links": res["links"], "ew": "done"}
        for code, service in res["isrcs"]:
            try:
                alt = self.sp.by_isrc(track, [code])
            except SpotifyError:
                alt = None
            if alt:
                old = [a for a in (row or {}).get("alternatives", []) if a["uri"] != alt["uri"]]
                out.update(status="matched", pick=alt["uri"], alternatives=[alt] + old, rescued=service)
                break
        return out

    def find_elsewhere(self, query: str) -> dict:
        """On-demand lookup for one row (globe button)."""
        t = parser.parse_line(query)
        if not t:
            return {"elsewhere": [], "links": {}, "ew": "done"}
        return self._elsewhere(t, None)

    def research(self, query: str) -> dict:
        t = parser.parse_line(query)
        if not t:
            return {"status": "missing", "alternatives": [], "pick": None}
        try:
            res = self.sp.match(t)
        except SpotifyError as exc:
            return {"status": "missing", "alternatives": [], "pick": None, "error": _friendly(exc)}
        if res["alternatives"] and res["status"] == "missing":  # user typed it: trust their top hit
            res.update(status="maybe", pick=res["alternatives"][0]["uri"])
        return res

    def create(self, name: str, public: bool, uris: list[str], description: str) -> dict:
        try:
            pl = self.sp.create_playlist(name.strip() or "DJ Set", bool(public), description)
            self.sp.add(pl["id"], uris)
            return {"ok": True, "url": (pl.get("external_urls") or {}).get("spotify"),
                    "uri": pl.get("uri"), "count": len(set(uris))}
        except SpotifyError as exc:
            return {"ok": False, "error": _friendly(exc)}


def _friendly(exc: Exception) -> str:
    msg = str(exc)
    if " 403" in msg or "403:" in msg:
        return ("Spotify refused (403). In your Spotify Developer app open Settings → User Management and add "
                "the email of the Spotify account you log in with, then reconnect.")
    return msg


def _selftest() -> int:
    """Used by CI on the frozen app: load every module and the GUI backend, then exit."""
    import importlib

    import webview  # noqa: F401
    import yt_dlp  # noqa: F401

    backend = {"darwin": "webview.platforms.cocoa", "win32": "webview.platforms.winforms"}.get(sys.platform)
    if backend:
        importlib.import_module(backend)
    if sys.platform == "win32":
        importlib.import_module("webview.platforms.edgechromium")
    assert (_ui_dir() / "index.html").is_file(), "UI files missing"
    assert parser.parse("0:00 A - B\n1:00 C - D") and elsewhere.search_links(parser.Track("A", "B"))
    return 0


def main() -> None:
    if "--selftest" in sys.argv:
        try:
            code = _selftest()
        except Exception as exc:  # report and fail
            print(f"selftest failed: {exc!r}", file=sys.stderr)
            code = 1
        (Path.home() / ".tracklister-selftest").write_text(str(code))
        sys.exit(code)

    import webview

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    api = Api()
    webview.create_window(f"Tracklister {__version__}", str(_ui_dir() / "index.html"), js_api=api,
                          width=1040, height=780, min_size=(720, 560), background_color="#0e0f11")
    webview.start(debug="--debug" in sys.argv)


if __name__ == "__main__":
    main()
