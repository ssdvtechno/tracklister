from tracklister import parser
from tracklister.source import youtube_id
from tracklister.spotify import queries, score


def d(text, **kw):
    return [t.display for t in parser.parse(text, **kw)]


def test_timestamps_labels_and_noise():
    txt = """Tracklist:
00:00 Intro
01:23 Bicep - Glue (Original Mix) [Ninja Tune]
12:00 ID - ID
1:02:03 Peggy Gou - Nanana [XL Recordings] 🔥
Kerri Chandler - Rain 1:10:00
1:15:00 Charlotte de Witte – Overdrive - Amelie Lens Remix FREE DOWNLOAD
04:30 Chicane – Saltwater (ARMADA)
"""
    assert d(txt) == [
        "Bicep – Glue",
        "Peggy Gou – Nanana",
        "Kerri Chandler – Rain",
        "Charlotte de Witte – Overdrive (Amelie Lens Remix)",
        "Chicane – Saltwater",
    ]


def test_numbered_and_loose():
    assert d("1. Daft Punk - One More Time\n2) Justice - Genesis\nthanks!") == [
        "Daft Punk – One More Time", "Justice – Genesis"]
    assert d('Moderat - A New Error\nRoyksopp "Eple"\nhello', loose=True) == [
        "Moderat – A New Error", "Royksopp – Eple"]


def test_one_line_tracklist():
    assert len(parser.parse("0:00 A - B 3:00 C - D 6:00 E - F")) == 3


def test_remix_kept_and_guess_dj():
    t = parser.parse_line("Disclosure ft. Sam Smith - Latch (Kaytranada Remix)")
    assert t.remix == "Kaytranada Remix" and t.base_title == "Latch"
    assert t.artists == ["Disclosure", "Sam Smith"]
    assert parser.guess_dj("Peggy Gou @ Boiler Room Seoul") == "Peggy Gou"
    assert parser.guess_dj("Boiler Room: London") is None


def test_youtube_id():
    for u in ("https://youtu.be/dQw4w9WgXcQ", "youtube.com/watch?v=dQw4w9WgXcQ&t=1",
              "https://www.youtube.com/live/dQw4w9WgXcQ", "https://m.youtube.com/watch?v=dQw4w9WgXcQ"):
        assert youtube_id(u) == "dQw4w9WgXcQ"
    assert youtube_id("https://soundcloud.com/x/y") is None


def test_scoring_prefers_right_version():
    t = parser.parse_line("Waterboys - The Whole Of The Moon (Chicane Remix)")
    remix = {"name": "The Whole Of The Moon - Chicane Remix", "artists": [{"name": "The Waterboys"}, {"name": "Chicane"}]}
    orig = {"name": "The Whole Of The Moon", "artists": [{"name": "The Waterboys"}]}
    other = {"name": "Moon River", "artists": [{"name": "Frank Ocean"}]}
    assert score(t, remix) > score(t, orig) > score(t, other)
    assert queries(t)[0].startswith("Waterboys The Whole Of The Moon")


def test_combined_lines_split_and_commentary_dropped():
    from pathlib import Path

    tracks = parser.parse((Path(__file__).parent / "fixtures" / "dax_comment.txt").read_text("utf-8"))
    shown = [t.display for t in tracks]
    assert len(tracks) == 26
    for pair in [("Dynamic Forces – Ander", "Lars Huismann – Grid"),
                 ("Nemesis – Camino Al Fondo", "JALA – Resonant Core"),
                 ("Quartz – Calling", "Seen Links Schlosser Rechts – Die Nacht")]:
        i = shown.index(pair[0])
        assert shown[i + 1] == pair[1]
    assert "Maxx – Pulse Shaper" in shown
    assert "Rama 1 – C'est La Vie (Hard Mix)" in shown
    assert not any("unreleased" in s.lower() or "sounds like" in s.lower() for s in shown)


def test_with_inside_titles_is_not_split():
    assert d("0:00 Billy Idol - Dancing with Myself\n1:00 A - B") == ["Billy Idol – Dancing with Myself", "A – B"]
    assert d("0:00 A - B into C - D\n1:00 E - F") == ["A – B", "C – D", "E – F"]
