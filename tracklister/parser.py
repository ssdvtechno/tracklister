"""Turn messy tracklist text (video descriptions, comments, pasted lists) into tracks."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass

TS = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"
_NUM = r"(?:#?\d{1,3}\s*[.)]\s*|#\d{1,3}\s+)?"

LEAD_TS = re.compile(rf"^\s*{_NUM}[\[(]?(?P<ts>{TS})[\])]?(?:\s*[-–—|:.)>~•]+\s*|\s+)(?P<rest>.+)$")
TRAIL_TS = re.compile(rf"^\s*{_NUM}(?P<rest>.*[^\W\d_].*?)[\s\-–—|@]*[\[(]?(?<![\d:])(?P<ts>{TS})[\])]?\s*$")
NUMBERED = re.compile(r"^\s*(?:#|track\s*)?(?P<n>\d{1,3})\s*[.):\-–—]\s*(?P<rest>.+)$", re.I)
ANY_TS = re.compile(rf"(?<![\d:])[\[(]?{TS}[\])]?(?![\d:])")
RANGE_TS = re.compile(rf"^\s*[-–—]?\s*[\[(]?{TS}[\])]?\s*[-–—|:.]*\s*")

URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
HANDLE = re.compile(r"(?<![\w.])[@#][\w.]+")
SEP = re.compile(r"\s+[-–—~−]+\s+|\s+[–—]\s*|\s*[–—]\s+")
LOOSE_SEP = re.compile(r"\s*[–—]\s*|(?<=\S)\s+-|-\s+(?=\S)")
BY = re.compile(r"^(?P<title>.+?)\s+by\s+(?P<artist>.+)$", re.I)
QUOTED = re.compile(r'^(?P<artist>.+?)\s+"(?P<title>.+?)"\s*$')

VERSION_WORDS = re.compile(
    r"\b(remix|rmx|edit|re-?edit|rework|bootleg|flip|vip|dub|mix|version|refix|mash-?up|"
    r"cover|remake|reprise|acoustic|instrumental|live)\b",
    re.I,
)
NOISE_TAG = re.compile(
    r"^(original|extended|club|radio|album|single|main|full)(\s+(mix|version|edit|club mix))?$|"
    r"^(\d{4}\s+)?remaster(ed)?(\s+\d{4})?$|^(clean|explicit|dirty|hq|hd|lyrics?|mixed|official (video|audio))$|"
    r"^(id|unreleased|forthcoming|premiere|white label)$",
    re.I,
)
PROMO = re.compile(
    r"\b(out now|free\s*(dl|download)|buy|stream|forthcoming|coming soon|pre-?order|available|"
    r"supported by|played by|label|records|recordings|music group)\b",
    re.I,
)
TRAILING_PROMO = re.compile(r"\s*[-–—:|]?\s*\b(free\s*(dl|download)|out now|forthcoming|coming soon)\b.*$", re.I)
FEAT = re.compile(r"\s*[(\[]?\b(feat\.?|ft\.?|featuring)\s+[^)\]]*[)\]]?", re.I)
ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\+|\band\b|\bx\b|\bvs\.?\s|\bfeat\.?|\bft\.?|\bfeaturing\b|\bb2b\b)\s*", re.I)

# "A - X mixed with B - Y": a second track layered over the first. Text after it without an
# "Artist - Title" shape is commentary ("mixed with another track") and is dropped.
MIXED_WITH = re.compile(r"\s+(?:mixed|blended|layered|mashed(?:\s*up)?|played|looped)\s+(?:with|into|over)\s+", re.I)
# Plain joiners only split when BOTH sides look like "Artist - Title" ("Dancing with Myself" stays whole).
JOINER = re.compile(r"\s+(?:with|w/|into|vs\.?|over|\+|&|x)\s+|\s*\+\s*|\s+/\s+", re.I)
FILLER_TAIL = re.compile(r"(?:\s+(?:either|possibly|maybe|probably|perhaps|and|or|then|also))+\s*$", re.I)
COMMENTARY = re.compile(
    r"\b(?:unreleased|forthcoming|sounds like|not sure|no idea|unknown|release from|anyone know|"
    r"who knows|track id|can'?t find|help me|edit of)\b", re.I)

ID_WORDS = {"id", "unknown", "tba", "tbc", "?", "??", "???", "track id", "unreleased"}
SKIP = {"intro", "outro", "tracklist", "track list", "tracklisting", "setlist", "start", "end",
        "break", "interlude", "applause", "talking", "speech", "encore"}
EDGES = " \t-–—|:;,•·*\"'>~_/\\"


@dataclass
class Track:
    artist: str
    title: str
    timestamp: str | None = None
    raw: str = ""

    @property
    def display(self) -> str:
        return f"{self.artist} – {self.title}" if self.artist else self.title

    @property
    def artists(self) -> list[str]:
        return [a.strip() for a in ARTIST_SPLIT.split(self.artist) if a and a.strip()]

    @property
    def remix(self) -> str | None:
        for m in re.finditer(r"\(([^()]*)\)", self.title):
            if VERSION_WORDS.search(m.group(1)):
                return m.group(1).strip()
        return None

    @property
    def base_title(self) -> str:
        t = FEAT.sub(" ", self.title)
        t = re.sub(r"\([^()]*\)", " ", t)
        return _squash(t).strip(EDGES) or self.title

    @property
    def search_title(self) -> str:
        return _squash(re.sub(r"[()\[\]]", " ", FEAT.sub(" ", self.title)))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["display"] = self.display
        return d


def parse(text: str, *, loose: bool = False) -> list[Track]:
    """Parse a block of text. ``loose`` also accepts bare "Artist - Title" lines (for pasted lists)."""
    lines = _lines(text or "")
    best: list[Track] = []
    for strategy in (_timestamped, _numbered) + ((_plain,) if loose else ()):
        found = strategy(lines)
        if len(found) > len(best):
            best = found
        if len(best) >= 3:
            break
    return _dedupe(best)


def parse_line(text: str) -> Track | None:
    m = LEAD_TS.match(text or "")
    if m:
        found = _tracks(m.group("rest"), m.group("ts"), text, need_artist=False, split=False)
    else:
        found = _tracks(text or "", None, text or "", need_artist=False, split=False)
    return found[0] if found else None


def guess_dj(video_title: str) -> str | None:
    """'Peggy Gou @ Boiler Room Seoul' -> 'Peggy Gou'."""
    head = re.split(r"\s+[-–—|@]\s+|\s*[|@(\[]\s*|\s+(?:live|dj set|at|in|for|presents|b2b)\b",
                    video_title or "", maxsplit=1, flags=re.I)[0].strip(EDGES)
    if not head or len(head) > 40 or re.search(r"\b(boiler room|cercle|mix|set|podcast|radio|episode|live)\b", head, re.I):
        return None
    return head


# --------------------------------------------------------------------------- internals


def _squash(s: str) -> str:
    return " ".join(s.split())


def _lines(text: str) -> list[str]:
    out = []
    for line in text.replace("\r", "\n").split("\n"):
        stamps = [m.start() for m in ANY_TS.finditer(line)]
        if len(stamps) >= 3:  # entire tracklist on one line
            if line[: stamps[0]].strip():
                out.append(line[: stamps[0]])
            out += [line[a:b] for a, b in zip(stamps, stamps[1:] + [len(line)])]
        else:
            out.append(line)
    return [ln for ln in out if ln.strip()]


def _timestamped(lines):
    out = []
    for line in lines:
        m = LEAD_TS.match(line) or TRAIL_TS.match(line)
        if m:
            out += _tracks(m.group("rest"), m.group("ts"), line, need_artist=False)
    return out


def _numbered(lines):
    out = []
    for line in lines:
        m = NUMBERED.match(line)
        if m:
            out += _tracks(m.group("rest"), None, line, need_artist=True)
    return out


def _plain(lines):
    return [t for line in lines for t in _tracks(line, None, line, need_artist=True)]


def _bracket(m: re.Match) -> str:
    open_, inner = m.group(1), m.group(2).strip()
    if not inner or PROMO.search(inner) or NOISE_TAG.match(inner):
        return " "
    if open_ == "[" and not VERSION_WORDS.search(inner):
        return " "  # [Label Name]
    if not VERSION_WORDS.search(inner) and inner.isupper() and len(inner.split()) <= 3 and len(inner) > 2:
        return " "  # (ARMADA), (SONY BMG): shouty label credits
    if re.fullmatch(TS, inner):
        return " "
    return f" ({inner}) "


def clean(text: str) -> str:
    s = unicodedata.normalize("NFKC", text)
    s = s.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "´": "'"}))
    s = URL.sub(" ", s)
    s = HANDLE.sub(" ", s)
    s = "".join(c if unicodedata.category(c)[0] in "LNPZ" or c in "&+'$!?.,-" else " " for c in s)
    for _ in range(2):
        s = re.sub(r"([(\[{])\s*([^()\[\]{}]*?)\s*[)\]}]", _bracket, s)
    s = re.sub(r"[\[\]{}]", " ", s)
    s = TRAILING_PROMO.sub("", s)
    if "|" in s:  # "Artist - Title | Label" -> keep the part that looks like a track
        parts = [p for p in s.split("|") if p.strip()]
        s = next((p for p in parts if SEP.search(p)), parts[0] if parts else "")
    return _squash(s).strip(EDGES.replace('"', ""))


def _split(text: str, loose: bool) -> tuple[str, str]:
    m = SEP.search(text)
    if m and 0 < m.start() and m.end() < len(text):
        return text[: m.start()], text[m.end():]
    if q := QUOTED.match(text):
        return q.group("artist"), q.group("title")
    if loose and (m := LOOSE_SEP.search(text)) and 0 < m.start() and m.end() < len(text):
        return text[: m.start()], text[m.end():]
    if b := BY.match(text):
        return b.group("artist"), b.group("title")
    return "", text


def _has_sep(text: str) -> bool:
    m = SEP.search(text)
    return bool(m and 0 < m.start() and m.end() < len(text))


def _segments(text: str) -> list[str]:
    """Split one line that names several tracks into one string per track."""
    parts = MIXED_WITH.split(text, maxsplit=1)
    if len(parts) == 2:
        head, tail = parts
        out = _segments(head)
        if _has_sep(tail):
            out += _segments(tail)
        return out
    pieces = JOINER.split(text)
    if len(pieces) > 1 and all(_has_sep(p) for p in pieces):
        return [seg for p in pieces for seg in _segments(p)]
    return [text]


def _tracks(rest: str, ts: str | None, raw: str, *, need_artist: bool, split: bool = True) -> list[Track]:
    rest = RANGE_TS.sub("", rest) if ts else rest
    rest = re.sub(rf"\s*[\[(]?(?<![\d:]){TS}[\])]?\s*$", "", rest)
    text = clean(rest)
    if not text:
        return []
    segs = _segments(text) if split else [text]
    out = []
    for seg in segs:
        seg = FILLER_TAIL.sub("", seg).strip(EDGES.replace('"', ""))
        if (t := _one(seg, ts, raw, need_artist=need_artist)):
            out.append(t)
    return out


def _one(text: str, ts: str | None, raw: str, *, need_artist: bool) -> Track | None:
    artist, title = _split(text, loose=True)
    artist, title = artist.strip(EDGES), title.strip(EDGES)
    # "Title - Fisher Remix" -> "Title (Fisher Remix)"
    if (v := re.search(r"\s+[-–—]\s+([^-–—()]+)$", title)) and VERSION_WORDS.search(v.group(1)):
        title = f"{title[: v.start()]} ({v.group(1).strip()})"
    if not title or (need_artist and not artist):
        return None
    lo_t, lo_a = title.lower().strip(" ?"), artist.lower().strip(" ?")
    if lo_t in ID_WORDS or lo_t.strip("?") == "" or (not artist and lo_t in SKIP):
        return None
    if lo_a in ID_WORDS and lo_t in ID_WORDS:
        return None
    if not artist and COMMENTARY.search(title):
        return None  # "Sounds like an unreleased Dax track"
    if not re.search(r"[^\W\d_]", title) or len(title) > 120:
        return None
    return Track(artist=artist, title=title, timestamp=ts, raw=raw.strip())


def _dedupe(tracks: list[Track]) -> list[Track]:
    seen, out = set(), []
    for t in tracks:
        key = re.sub(r"\W+", "", t.display.lower())
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out
