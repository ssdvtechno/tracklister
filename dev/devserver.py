"""Browser preview of the UI without pywebview (for UI work).

Serves the UI on http://127.0.0.1:8765 and bridges window.pywebview.api.* to the real
Api over HTTP. Pass --fake-spotify to stand in for Spotify with Deezer's public catalog (no login needed).
"""

from __future__ import annotations

import json
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracklister import app  # noqa: E402

UI = Path(app.__file__).parent / "ui" / "index.html"
SHIM = """<script>
window.pywebview = { api: new Proxy({}, { get: (_, name) => async (...args) => {
  const r = await fetch('/api/' + name, { method: 'POST', body: JSON.stringify(args) });
  return r.json();
}})};
addEventListener('DOMContentLoaded', () => dispatchEvent(new Event('pywebviewready')));
// Screenshot automation: http://127.0.0.1:8765/#demo=results&src=<text>&open=2&filter=elsewhere
addEventListener('pywebviewready', async () => {
  const p = new URLSearchParams(location.hash.slice(1)), demo = p.get('demo');
  if (!demo) return;
  await new Promise((r) => setTimeout(r, 300));
  if (demo === 'setup') { enterSetup(); document.getElementById('clientId').value = ''; document.title = 'ready'; return; }
  document.getElementById('src').value = p.get('src') || '';
  if (demo === 'start') { document.title = 'ready'; return; }
  await go();
  if (p.get('open')) rows[+p.get('open')].open = 'alts';
  if (p.get('filter')) filter = p.get('filter');
  render();
  if (demo === 'done') document.getElementById('create').click();
  await new Promise((r) => setTimeout(r, 1500));
  scrollTo(0, 0); document.title = 'ready';
});
</script>"""

api = app.Api()

if "--fake-spotify" in sys.argv:
    # Stand-in for Spotify that needs no login: real catalog results from Deezer's public API,
    # scored with the real matcher, so screenshots and UI behaviour look like the real thing.
    import requests

    from tracklister.spotify import MATCH, MAYBE, TITLE_ONLY_MATCH, score

    def fake_match(track, dj=None):
        q = f"{track.artist or dj or ''} {track.base_title}".strip()
        data = requests.get("https://api.deezer.com/search", params={"q": q, "limit": 8}, timeout=15).json().get("data") or []
        items = [{"uri": f"spotify:track:dz{x['id']}", "name": x["title"], "artists": [{"name": x["artist"]["name"]}],
                  "album": {"name": x["album"]["title"], "images": [{"url": x["album"]["cover_medium"]}]},
                  "external_urls": {"spotify": x["link"]}} for x in data]
        ranked = sorted(((score(track, it), it) for it in items), key=lambda v: -v[0])
        alts = [{"uri": it["uri"], "name": it["name"], "artists": it["artists"][0]["name"], "album": it["album"]["name"],
                 "image": it["album"]["images"][0]["url"], "url": it["external_urls"]["spotify"], "score": round(sc, 2)}
                for sc, it in ranked[:6] if sc >= 0.3]
        best = ranked[0][0] if ranked else 0
        th = MATCH if track.artist else TITLE_ONLY_MATCH
        status = "matched" if best >= th else "maybe" if best >= MAYBE else "missing"
        return {"status": status, "score": round(best, 2), "pick": alts[0]["uri"] if alts and status != "missing" else None,
                "alternatives": alts}

    api.sp.match = fake_match
    api.sp.by_isrc = lambda track, codes: None
    api.sp.me = lambda: {"id": "dev", "name": "Demo", "image": None}
    api.auth.logged_in = lambda: True
    api.create = lambda name, public, uris, desc: {"ok": True, "url": "https://open.spotify.com", "uri": "spotify:playlist:x", "count": len(set(uris))}
    api.open = lambda url: print("open", url)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        html = UI.read_text("utf-8").replace("<head>", "<head>" + SHIM, 1)
        self._send(200, html.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        name = self.path.removeprefix("/api/")
        args = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "[]")
        try:
            result = getattr(api, name)(*args)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        self._send(200, json.dumps(result).encode(), "application/json")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print("http://127.0.0.1:8765")
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
