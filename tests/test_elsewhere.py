from tracklister import elsewhere, parser


def fake(service, name, artist, isrc=None):
    return lambda q: [elsewhere._hit(service, name, [artist], f"https://{service}.example/x", isrc=isrc)]


def test_find_filters_junk_and_collects_isrcs(monkeypatch):
    monkeypatch.setattr(elsewhere, "_SEARCHERS", {
        "Beatport": fake("Beatport", "Ander", "Dynamic Forces", isrc="QM4TW2610067"),
        "Bandcamp": fake("Bandcamp", "Something Else Entirely", "Other Person"),
        "YouTube": fake("YouTube", "Ander", "Dynamic Forces"),
        "SoundCloud": lambda q: (_ for _ in ()).throw(RuntimeError("down")),  # one service failing is fine
        "Apple Music": lambda q: [],
        "Deezer": lambda q: [],
    })
    res = elsewhere.find(parser.parse_line("Dynamic Forces - Ander"))
    assert [h["service"] for h in res["hits"]] == ["Beatport", "YouTube"]
    assert all(h["sure"] for h in res["hits"])
    assert res["isrcs"] == [("QM4TW2610067", "Beatport")]
    assert "isrc" not in res["hits"][0]
    assert set(res["links"]) >= {"Beatport", "Bandcamp", "SoundCloud", "YouTube", "Discogs"}


def test_weak_hit_gives_no_isrc(monkeypatch):
    monkeypatch.setattr(elsewhere, "_SEARCHERS", {"Beatport": fake("Beatport", "12 (Live)", "The 1975", isrc="X")})
    res = elsewhere.find(parser.parse_line("The Tuss - Rushup I Bank 12"))
    assert res["isrcs"] == [] and res["hits"] == []
