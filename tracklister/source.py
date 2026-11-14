"""Get a set's title, description, chapters and comments.

YouTube: read the public watch page directly (no API key, and not hit by the
"sign in to confirm you're not a bot" wall that blocks yt-dlp on many networks).
Anything else (SoundCloud, Mixcloud, …) and YouTube fallback: yt-dlp.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import requests

from . import parser

log = logging.getLogger(__name__)
MIN_TRACKS = 3
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")


class SourceError(RuntimeError):
    pass


@dataclass
class SetInfo:
    url: str = ""
    title: str = "DJ Set"
    uploader: str = ""
    thumbnail: str | None = None
    description: str = ""
    chapters: list[tuple[str, str]] = field(default_factory=list)  # (timestamp, title)
    comments: list[str] | None = None  # filled lazily


# --------------------------------------------------------------------------- youtube


def youtube_id(url: str) -> str | None:
    url = url.strip()
    if re.fullmatch(r"[\w-]{11}", url):
        return url
    p = urlparse(url if "://" in url else "https://" + url)
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("music.")
    cand = None
    if host == "youtu.be":
        cand = p.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "youtube-nocookie.com"):
        cand = (parse_qs(p.query).get("v") or [None])[0]
        parts = [x for x in p.path.split("/") if x]
        if not cand and len(parts) >= 2 and parts[0] in ("live", "shorts", "embed", "v"):
            cand = parts[1]
    return cand if cand and re.fullmatch(r"[\w-]{11}", cand) else None


def _walk(o, key: str, out: list) -> list:
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                out.append(v)
            _walk(v, key, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, key, out)
    return out


def _text(node) -> str:
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(r.get("text", "") for r in node.get("runs", []))


class _YouTube:
    def __init__(self, vid: str):
        self.vid = vid
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self.s.cookies.set("CONSENT", "YES+1", domain=".youtube.com")
        self.s.cookies.set("SOCS", "CAI", domain=".youtube.com")
        r = self.s.get(f"https://www.youtube.com/watch?v={vid}&hl=en", timeout=20)
        r.raise_for_status()
        self.html = r.text
        m = re.search(r"ytInitialData\s*=\s*(\{.+?\})\s*;\s*</script", self.html)
        if not m:
            raise SourceError("YouTube page didn't contain video data")
        self.data = json.loads(m.group(1))

    def info(self) -> SetInfo:
        d = self.data
        desc = _walk(d, "attributedDescriptionBodyText", [])
        description = desc[0].get("content", "") if desc else ""
        if not description:
            m = re.search(r'"shortDescription":"((?:[^"\\]|\\.)*)"', self.html)
            description = json.loads(f'"{m.group(1)}"') if m else ""
        title = next((_text(r.get("title")) for r in _walk(d, "videoPrimaryInfoRenderer", []) if r.get("title")), "")
        if not title:
            m = re.search(r"<title>(.*?)(?: - YouTube)?</title>", self.html)
            title = m.group(1) if m else "DJ Set"
        owner = next((_text(r.get("title")) for r in _walk(d, "videoOwnerRenderer", []) if r.get("title")), "")
        chapters = []
        for mk in _walk(d, "macroMarkersListItemRenderer", []):
            ts, name = _text(mk.get("timeDescription")), _text(mk.get("title"))
            if ts and name and (ts, name) not in chapters:
                chapters.append((ts, name))
        if not title and not description:
            raise SourceError("Video is unavailable (private, deleted or age-restricted).")
        return SetInfo(url=f"https://www.youtube.com/watch?v={self.vid}", title=title, uploader=owner,
                       thumbnail=f"https://i.ytimg.com/vi/{self.vid}/hqdefault.jpg",
                       description=description, chapters=chapters)

    def comments(self, pages: int = 3) -> list[str]:
        key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', self.html)
        ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', self.html)
        token = None
        for sec in _walk(self.data, "itemSectionRenderer", []):
            if sec.get("sectionIdentifier") == "comment-item-section":
                cmds = _walk(sec, "continuationCommand", [])
                token = cmds[0].get("token") if cmds else None
        if not (key and ver and token):
            return []
        ctx = {"client": {"clientName": "WEB", "clientVersion": ver.group(1), "hl": "en"}}
        out: list[str] = []
        for _ in range(pages):
            r = self.s.post(f"https://www.youtube.com/youtubei/v1/next?key={key.group(1)}",
                            json={"context": ctx, "continuation": token}, timeout=20)
            if r.status_code != 200:
                break
            j = r.json()
            for p in _walk(j, "payload", []):
                if isinstance(p, dict) and "commentEntityPayload" in p:
                    out.append(p["commentEntityPayload"].get("properties", {}).get("content", {}).get("content", ""))
            nxt = [c.get("token") for c in _walk(j.get("onResponseReceivedEndpoints", []), "continuationCommand", [])]
            if not nxt:
                break
            token = nxt[-1]
        return out


# --------------------------------------------------------------------------- yt-dlp


def _ydl(url: str, extra: dict | None = None) -> dict:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "socket_timeout": 20, **(extra or {})}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False) or {}
    except Exception as exc:
        msg = re.sub(r"\s+See\s+https?://\S+.*$", "", str(exc).replace("ERROR: ", ""), flags=re.S)
        raise SourceError(f"Couldn't open that link: {msg}") from exc


def _fmt(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _ydl_info(url: str) -> SetInfo:
    info = _ydl(url)
    return SetInfo(
        url=info.get("webpage_url") or url,
        title=info.get("title") or "DJ Set",
        uploader=info.get("uploader") or info.get("channel") or "",
        thumbnail=info.get("thumbnail"),
        description=info.get("description") or "",
        chapters=[(_fmt(c.get("start_time") or 0), c.get("title") or "") for c in info.get("chapters") or []],
    )


# --------------------------------------------------------------------------- public


def fetch(url: str) -> tuple[SetInfo, _YouTube | None]:
    url = url.strip()
    vid = youtube_id(url)
    if vid:
        try:
            yt = _YouTube(vid)
            return yt.info(), yt
        except Exception as exc:
            log.warning("YouTube page read failed (%s); trying yt-dlp", exc)
    elif not re.match(r"^(https?://)", url):
        url = "https://" + url
    return _ydl_info(url), None


def find_tracklist(url: str, progress=lambda msg: None) -> tuple[SetInfo, list[parser.Track], str]:
    """Returns (info, tracks, where-it-was-found)."""
    progress("Opening link…")
    info, yt = fetch(url)
    tracks, where = parser.parse(info.description), "description"

    if len(tracks) < MIN_TRACKS and info.chapters:
        ch = parser.parse("\n".join(f"{ts} {name}" for ts, name in info.chapters))
        if len(ch) > len(tracks):
            tracks, where = ch, "chapters"

    if len(tracks) < MIN_TRACKS and yt:
        progress("No tracklist in the description — checking the comments…")
        best: list[parser.Track] = []
        try:
            comments = yt.comments()
        except Exception as exc:
            log.warning("comments failed: %s", exc)
            comments = []
        for text in comments:
            found = parser.parse(text)
            if len(found) > len(best):
                best = found
        if len(best) > len(tracks):
            tracks, where = best, "comments"

    if len(tracks) < 2:  # a lone timestamped line is usually a chatty comment, not a tracklist
        tracks = []
    return info, tracks, where
