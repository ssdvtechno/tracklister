"""Look for a track on other services when Spotify doesn't have it.

All keyless public endpoints: Beatport, Bandcamp, SoundCloud, YouTube, Apple Music (iTunes
Search API) and Deezer. Each hit is scored with the same matcher as Spotify, and Beatport/
Deezer hits carry an ISRC that can locate the exact recording on Spotify.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

from . import parser
from .spotify import score

log = logging.getLogger(__name__)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
TIMEOUT = 12
# Other services return lots of loosely related uploads, so be stricter than for Spotify.
SHOW, SURE, ISRC_SURE = 0.7, 0.82, 0.88
SERVICES = ["Beatport", "Bandcamp", "SoundCloud", "YouTube", "Apple Music", "Deezer"]

_http = requests.Session()
_http.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
_sc_lock = threading.Lock()
_sc_client_id: str | None = None


def search_links(track: parser.Track) -> dict[str, str]:
    """Plain search-page links for every service (always works, no lookup)."""
    q = urllib.parse.quote_plus(f"{track.artist} {track.search_title}".strip())
    return {
        "Beatport": f"https://www.beatport.com/search?q={q}",
        "Bandcamp": f"https://bandcamp.com/search?q={q}&item_type=t",
        "SoundCloud": f"https://soundcloud.com/search/sounds?q={q}",
        "YouTube": f"https://www.youtube.com/results?search_query={q}",
        "Apple Music": f"https://music.apple.com/search?term={q}",
        "Deezer": f"https://www.deezer.com/search/{q}",
        "Discogs": f"https://www.discogs.com/search/?q={q}&type=all",
    }


def _hit(service, name, artists, url, image=None, isrc=None, extra=None) -> dict:
    return {"service": service, "name": name or "", "artists": [a for a in artists if a],
            "url": url, "image": image, "isrc": isrc, "extra": extra or ""}


# --------------------------------------------------------------------------- services


def _beatport(q: str) -> list[dict]:
    r = _http.get("https://www.beatport.com/search/tracks", params={"q": q}, timeout=TIMEOUT)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return []
    queries = json.loads(m.group(1))["props"]["pageProps"]["dehydratedState"]["queries"]
    data = next((qq["state"]["data"]["data"] for qq in queries
                 if isinstance(qq.get("state", {}).get("data"), dict) and "data" in qq["state"]["data"]), [])
    out = []
    for x in data[:8]:
        mix = x.get("mix_name") or ""
        name = x.get("track_name", "") + (f" ({mix})" if mix and mix.lower() != "original mix" else "")
        slug = re.sub(r"[^a-z0-9]+", "-", x.get("track_name", "").lower()).strip("-") or "track"
        img = (x.get("release") or {}).get("release_image_uri", "").replace("1400x1400", "100x100") or None
        label = (x.get("label") or {}).get("label_name", "")
        out.append(_hit("Beatport", name, [a.get("artist_name") for a in x.get("artists") or []],
                        f"https://www.beatport.com/track/{slug}/{x.get('track_id')}", img, x.get("isrc"), label))
    return out


def _bandcamp(q: str) -> list[dict]:
    r = _http.post("https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic", timeout=TIMEOUT,
                   json={"search_text": q, "search_filter": "t", "full_page": False, "fan_id": None})
    res = (r.json().get("auto") or {}).get("results") or []
    return [_hit("Bandcamp", x.get("name"), [x.get("band_name")], x.get("item_url_path"), x.get("img"))
            for x in res if x.get("type") == "t" and x.get("item_url_path")]


def _soundcloud_id() -> str | None:
    global _sc_client_id
    with _sc_lock:
        if _sc_client_id:
            return _sc_client_id
        html = _http.get("https://soundcloud.com", timeout=TIMEOUT).text
        for src in reversed(re.findall(r'<script crossorigin src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html)):
            m = re.search(r'client_id\s*:\s*"([A-Za-z0-9]{32})"', _http.get(src, timeout=TIMEOUT).text)
            if m:
                _sc_client_id = m.group(1)
                break
        return _sc_client_id


def _soundcloud(q: str) -> list[dict]:
    cid = _soundcloud_id()
    if not cid:
        return []
    j = _http.get("https://api-v2.soundcloud.com/search/tracks", timeout=TIMEOUT,
                  params={"q": q, "client_id": cid, "limit": 8}).json()
    out = []
    for x in j.get("collection") or []:
        title, uploader = x.get("title", ""), (x.get("user") or {}).get("username", "")
        p = parser.parse_line(title)  # uploads are often titled "Artist - Title"
        artists = [p.artist, uploader] if p and p.artist else [x.get("publisher_metadata", {}).get("artist") or uploader]
        out.append(_hit("SoundCloud", p.title if p and p.artist else title, artists, x.get("permalink_url"),
                        x.get("artwork_url"), extra=uploader))
    return out


def _youtube(q: str) -> list[dict]:
    r = _http.get("https://www.youtube.com/results", params={"search_query": q, "hl": "en"},
                  cookies={"CONSENT": "YES+1"}, timeout=TIMEOUT)
    m = re.search(r"ytInitialData\s*=\s*(\{.+?\})\s*;\s*</script", r.text)
    if not m:
        return []
    found: list[dict] = []

    def walk(o):
        if len(found) >= 8:
            return
        if isinstance(o, dict):
            if "videoRenderer" in o:
                found.append(o["videoRenderer"])
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(json.loads(m.group(1)))
    out = []
    for v in found:
        title = "".join(x.get("text", "") for x in (v.get("title") or {}).get("runs", []))
        channel = "".join(x.get("text", "") for x in (v.get("ownerText") or {}).get("runs", []))
        channel = re.sub(r"\s+-\s+Topic$", "", channel)  # "Artist - Topic" auto channels
        p = parser.parse_line(title)
        name, artists = (p.title, [p.artist, channel]) if p and p.artist else (title, [channel])
        out.append(_hit("YouTube", name, artists, f"https://www.youtube.com/watch?v={v['videoId']}",
                        f"https://i.ytimg.com/vi/{v['videoId']}/default.jpg", extra=channel))
    return out


def _apple(q: str) -> list[dict]:
    j = _http.get("https://itunes.apple.com/search", params={"term": q, "entity": "song", "limit": 8},
                  timeout=TIMEOUT).json()
    return [_hit("Apple Music", x.get("trackName"), [x.get("artistName")], x.get("trackViewUrl"),
                 x.get("artworkUrl60")) for x in j.get("results") or []]


def _deezer(q: str) -> list[dict]:
    j = _http.get("https://api.deezer.com/search", params={"q": q, "limit": 8}, timeout=TIMEOUT).json()
    return [_hit("Deezer", x.get("title"), [(x.get("artist") or {}).get("name")], x.get("link"),
                 (x.get("album") or {}).get("cover_small"), extra=str(x.get("id")))
            for x in j.get("data") or []]


_SEARCHERS = {"Beatport": _beatport, "Bandcamp": _bandcamp, "SoundCloud": _soundcloud,
              "YouTube": _youtube, "Apple Music": _apple, "Deezer": _deezer}
_pool = ThreadPoolExecutor(max_workers=12)


def _deezer_isrc(track_id: str) -> str | None:
    try:
        return _http.get(f"https://api.deezer.com/track/{track_id}", timeout=TIMEOUT).json().get("isrc")
    except Exception:
        return None


# --------------------------------------------------------------------------- public


def find(track: parser.Track, dj: str | None = None) -> dict:
    """Search every service in parallel.

    Returns {"hits": [best hit per service, strongest first], "links": {service: search url},
             "isrcs": [(isrc, service) of confident hits, for a Spotify retry]}.
    """
    if not track.artist and dj:
        track = parser.Track(dj, track.title, track.timestamp, track.raw)
    q = f"{track.artist} {track.base_title}".strip()
    sure = SURE if track.artist else 0.9
    futures = {name: _pool.submit(fn, q) for name, fn in _SEARCHERS.items()}
    hits = []
    for name, fut in futures.items():
        try:
            results = fut.result(timeout=TIMEOUT + 5)
        except Exception as exc:
            log.info("%s search failed: %s", name, exc)
            continue
        best, best_s = None, 0.0
        for h in results:
            s = score(track, {"name": h["name"], "artists": [{"name": a} for a in h["artists"]]})
            if s > best_s:
                best, best_s = h, s
        if best and best_s >= SHOW:
            best["score"] = round(best_s, 2)
            best["sure"] = best_s >= sure
            hits.append(best)
    hits.sort(key=lambda h: (-h["sure"], SERVICES.index(h["service"]), -h["score"]))

    isrcs = [(h["isrc"], h["service"]) for h in hits if h["score"] >= ISRC_SURE and h.get("isrc")]
    deezer = next((h for h in hits if h["service"] == "Deezer" and h["score"] >= ISRC_SURE), None)
    if deezer and not isrcs and (code := _deezer_isrc(deezer["extra"])):
        isrcs.append((code, "Deezer"))
    for h in hits:
        h["artists"] = ", ".join(dict.fromkeys(h["artists"]))
        h.pop("isrc", None)
        if h["service"] == "Deezer":
            h["extra"] = ""
    return {"hits": hits, "links": search_links(track), "isrcs": list(dict.fromkeys(isrcs))}
