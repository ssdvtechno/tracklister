"""Spotify: PKCE login (Client ID only, no secret), track matching, playlist creation."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from . import config
from .parser import VERSION_WORDS, Track

log = logging.getLogger(__name__)

REDIRECT_PORT = 43110
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
SCOPES = "playlist-modify-public playlist-modify-private user-read-private"
API = "https://api.spotify.com/v1"
MATCH, TITLE_ONLY_MATCH, MAYBE = 0.62, 0.80, 0.45


class SpotifyError(RuntimeError):
    pass


# --------------------------------------------------------------------------- auth


class Auth:
    def __init__(self):
        self.status = "idle"  # idle | waiting | ok | error
        self.error = ""
        self._server: HTTPServer | None = None

    @property
    def client_id(self) -> str:
        return config.load().get("client_id", "")

    def token(self) -> str:
        cfg = config.load()
        tok = cfg.get("token") or {}
        if not tok.get("access_token"):
            raise SpotifyError("Not logged in to Spotify.")
        if tok.get("expires_at", 0) - 60 < time.time():
            tok = self._refresh(cfg["client_id"], tok["refresh_token"])
        return tok["access_token"]

    def logged_in(self) -> bool:
        return bool((config.load().get("token") or {}).get("refresh_token"))

    def logout(self):
        cfg = config.load()
        cfg.pop("token", None)
        cfg.pop("user", None)
        config.save(cfg)

    def _store(self, data: dict, client_id: str) -> dict:
        cfg = config.load()
        old = cfg.get("token") or {}
        tok = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or old.get("refresh_token"),
            "expires_at": time.time() + int(data.get("expires_in", 3600)),
        }
        cfg["token"], cfg["client_id"] = tok, client_id
        config.save(cfg)
        return tok

    def _refresh(self, client_id: str, refresh_token: str) -> dict:
        r = requests.post("https://accounts.spotify.com/api/token", timeout=20, data={
            "grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id})
        if r.status_code != 200:
            self.logout()
            raise SpotifyError("Spotify login expired — please connect again.")
        return self._store(r.json(), client_id)

    def start_login(self, client_id: str) -> None:
        client_id = client_id.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", client_id):
            raise SpotifyError("That doesn't look like a Client ID (it's 32 letters/numbers).")
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        auth = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if not self.path.startswith("/callback"):
                    self.send_response(404)
                    self.end_headers()
                    return
                ok, msg = False, ""
                if q.get("state", [""])[0] != state:
                    msg = "Login check failed (state mismatch). Try again from the app."
                elif "error" in q:
                    msg = f"Spotify said: {q['error'][0]}"
                else:
                    r = requests.post("https://accounts.spotify.com/api/token", timeout=20, data={
                        "grant_type": "authorization_code", "code": q.get("code", [""])[0],
                        "redirect_uri": REDIRECT_URI, "client_id": client_id, "code_verifier": verifier})
                    if r.status_code == 200:
                        auth._store(r.json(), client_id)
                        ok = True
                    else:
                        msg = f"Token exchange failed: {r.text[:200]}"
                auth.status, auth.error = ("ok", "") if ok else ("error", msg)
                body = _done_page(ok, msg).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                threading.Thread(target=self.server.shutdown, daemon=True).start()

        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        try:
            self._server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
        except OSError as exc:
            raise SpotifyError(f"Port {REDIRECT_PORT} is busy — close other copies of the app.") from exc
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

        cfg = config.load()
        cfg["client_id"] = client_id
        config.save(cfg)
        self.status, self.error = "waiting", ""
        webbrowser.open("https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
            "scope": SCOPES, "state": state, "code_challenge_method": "S256",
            "code_challenge": challenge, "show_dialog": "true"}))


def _done_page(ok: bool, msg: str) -> str:
    color, head = ("#1ed760", "Connected!") if ok else ("#ff5c5c", "Something went wrong")
    sub = "You can close this tab and go back to Tracklister." if ok else msg
    return (f"<html><body style='font-family:-apple-system,sans-serif;background:#111;color:#eee;"
            f"display:grid;place-items:center;height:100vh;margin:0'><div style='text-align:center'>"
            f"<h1 style='color:{color}'>{head}</h1><p>{sub}</p></div>"
            f"{'<script>setTimeout(()=>window.close(),1500)</script>' if ok else ''}</body></html>")


# --------------------------------------------------------------------------- api


class Client:
    def __init__(self, auth: Auth):
        self.auth = auth
        self._market: str | None = "from_token"

    def _req(self, method: str, path: str, **kw) -> dict:
        for attempt in range(4):
            r = requests.request(method, f"{API}/{path}", timeout=20,
                                 headers={"Authorization": f"Bearer {self.auth.token()}"}, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(int(r.headers.get("Retry-After", 1 + attempt)), 10))
                continue
            if r.status_code >= 400:
                try:
                    detail = r.json().get("error", {}).get("message", r.text)
                except ValueError:
                    detail = r.text
                err = SpotifyError(f"Spotify error {r.status_code}: {detail}")
                err.status = r.status_code  # type: ignore[attr-defined]
                raise err
            return r.json() if r.content else {}
        raise SpotifyError("Spotify is rate-limiting us — wait a minute and retry.")

    def me(self) -> dict:
        cfg = config.load()
        if not cfg.get("user"):
            u = self._req("GET", "me")
            cfg["user"] = {"id": u["id"], "name": u.get("display_name") or u["id"],
                           "image": ((u.get("images") or [{}])[0] or {}).get("url")}
            config.save(cfg)
        return cfg["user"]

    def search(self, q: str) -> list[dict]:
        params = {"q": q, "type": "track", "limit": 10}
        if self._market:
            params["market"] = self._market
        try:
            data = self._req("GET", "search", params=params)
        except SpotifyError as exc:
            if getattr(exc, "status", 0) == 403 and self._market:
                self._market = None
                return self.search(q)
            raise
        return [i for i in (data.get("tracks") or {}).get("items") or [] if i and i.get("uri")]

    def create_playlist(self, name: str, public: bool, description: str) -> dict:
        body = {"name": name[:100] or "DJ Set", "public": public, "description": _squash(description)[:300]}
        try:
            return self._req("POST", "me/playlists", json=body)
        except SpotifyError as exc:
            if getattr(exc, "status", 0) not in (404, 405):
                raise
        return self._req("POST", f"users/{self.me()['id']}/playlists", json=body)

    def add(self, playlist_id: str, uris: list[str]) -> None:
        uris = list(dict.fromkeys(uris))
        for i in range(0, len(uris), 100):
            chunk = {"uris": uris[i:i + 100]}
            try:
                self._req("POST", f"playlists/{playlist_id}/items", json=chunk)
            except SpotifyError as exc:
                if getattr(exc, "status", 0) not in (404, 405):
                    raise
                self._req("POST", f"playlists/{playlist_id}/tracks", json=chunk)

    # ------------------------------------------------------------------ matching

    def by_isrc(self, track: Track, isrcs: list[str]) -> dict | None:
        """Exact-recording lookup with ISRCs found on other services (Beatport, Deezer)."""
        for code in isrcs:
            try:
                items = self.search(f"isrc:{code}")
            except SpotifyError:
                continue
            ranked = sorted(((score(track, it), it) for it in items), key=lambda v: -v[0])
            if ranked and ranked[0][0] >= 0.5:  # ISRC is exact; only guard against a bad code
                return _simplify(ranked[0][1], max(ranked[0][0], MATCH))
        return None

    def match(self, track: Track, dj: str | None = None) -> dict:
        """Returns {status, score, pick, alternatives[]} with simplified track dicts."""
        if not track.artist and dj:
            hinted = self.match(Track(dj, track.title, track.timestamp, track.raw))
            if hinted["status"] == "matched":
                return hinted
        threshold = MATCH if track.artist else TITLE_ONLY_MATCH
        pool: dict[str, tuple[float, dict]] = {}
        for q in queries(track):
            for item in self.search(q):
                s = score(track, item)
                if item["uri"] not in pool or pool[item["uri"]][0] < s:
                    pool[item["uri"]] = (s, item)
            if pool and max(v[0] for v in pool.values()) >= threshold:
                break
        ranked = sorted(pool.values(), key=lambda v: -v[0])
        alts = [_simplify(it, s) for s, it in ranked[:6] if s >= 0.3]
        best = ranked[0][0] if ranked else 0.0
        status = "matched" if best >= threshold else "maybe" if best >= MAYBE else "missing"
        return {"status": status, "score": round(best, 2), "pick": alts[0]["uri"] if alts and status != "missing" else None,
                "alternatives": alts}


def _simplify(item: dict, s: float) -> dict:
    imgs = (item.get("album") or {}).get("images") or []
    return {
        "uri": item["uri"],
        "name": item.get("name", ""),
        "artists": ", ".join(a.get("name", "") for a in item.get("artists") or []),
        "album": (item.get("album") or {}).get("name", ""),
        "image": imgs[-1]["url"] if imgs else None,
        "url": (item.get("external_urls") or {}).get("spotify"),
        "score": round(s, 2),
    }


def _squash(s: str) -> str:
    return " ".join((s or "").split())


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", " and ").replace("$", "s")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    r = SequenceMatcher(None, a, b).ratio()
    if f" {a} " in f" {b} " or f" {b} " in f" {a} ":
        r = max(r, 0.88)
    return r


def score(track: Track, item: dict) -> float:
    name = item.get("name") or ""
    cand_artists = [norm(a.get("name", "")) for a in item.get("artists") or []]
    n_name = norm(name)
    cand_base = norm(re.sub(r"\([^()]*\)|\[[^\[\]]*\]", " ", re.sub(r"\s+-\s+.*$", "", name)))
    t = max(sim(norm(track.search_title), n_name), sim(norm(track.base_title), cand_base))

    if track.remix:
        words = [w for w in norm(track.remix).split() if w not in {"remix", "edit", "mix", "rmx", "version"}]
        hay = n_name.split() + " ".join(cand_artists).split()
        t = min(1.0, t + 0.1) if words and all(w in hay for w in words) else t - 0.08
    elif VERSION_WORDS.search(re.sub(r"(?i)original|extended|radio|remaster(ed)?", "", name)):
        t -= 0.05

    if not track.artist:
        return max(0.0, t)
    want = [norm(a) for a in track.artists] or [norm(track.artist)]
    a = max((sim(w, c) for w in want for c in cand_artists), default=0.0)
    joined = f" {' '.join(cand_artists)} "
    if any(w and f" {w} " in joined for w in want):
        a = max(a, 0.92)
    return max(0.0, 0.55 * t + 0.45 * a)


def queries(track: Track) -> list[str]:
    first = track.artists[0] if track.artists else ""
    qs = []
    if track.artist:
        qs += [f"{_squash(re.sub(r'[(][^()]*[)]', ' ', track.artist))} {track.search_title}",
               f"track:{track.base_title} artist:{first}",
               f"{first} {track.base_title}"]
    qs += [track.search_title, track.base_title]
    out, seen = [], set()
    for q in qs:
        q = _squash(q.replace('"', ""))[:250]
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


__all__ = ["Auth", "Client", "SpotifyError", "REDIRECT_URI"]
