from app import matcher
from app.matcher import Source


def cand(name, artists, album="", dur=None):
    return {"uri": f"spotify:track:{abs(hash(name + str(artists))) % 10**22:022d}", "name": name,
            "artists": [{"name": a} for a in artists], "album": {"name": album}, "duration_ms": dur}


def test_exact_match_is_auto():
    src = Source("Bohemian Rhapsody", None, ["Queen"], "A Night at the Opera", 354000)
    c = cand("Bohemian Rhapsody - Remastered 2011", ["Queen"], "A Night At The Opera (2011 Remaster)", 355000)
    assert matcher.score(src, c) >= matcher.AUTO_THRESHOLD


def test_cyrillic_vs_latin_artist():
    src = Source("Группа крови", None, ["Кино"], "Группа крови", 286000)
    c = cand("Группа крови", ["KINO"], "Gruppa krovi", 285000)
    assert matcher.score(src, c) >= matcher.AUTO_THRESHOLD


def test_remix_vs_original_penalized():
    src = Source("Blinding Lights", None, ["The Weeknd"], "After Hours", 200000)
    orig = cand("Blinding Lights", ["The Weeknd"], "After Hours", 200040)
    remix = cand("Blinding Lights - Chromatics Remix", ["The Weeknd"], "Blinding Lights (Remixes)", 210000)
    assert matcher.score(src, orig) > matcher.score(src, remix) + 15
    assert matcher.classify(matcher.score(src, remix)) != "auto"


def test_version_remix_matches_remix():
    src = Source("Blinding Lights", "Chromatics Remix", ["The Weeknd"], None, 210000)
    remix = cand("Blinding Lights - Chromatics Remix", ["The Weeknd"], "Blinding Lights (Remixes)", 210000)
    assert matcher.score(src, remix) >= matcher.AUTO_THRESHOLD


def test_wrong_artist_not_auto():
    src = Source("Hello", None, ["Adele"], "25", 295000)
    c = cand("Hello", ["Lionel Richie"], "Can't Slow Down", 247000)
    assert matcher.score(src, c) < matcher.AUTO_THRESHOLD


def test_feat_ignored():
    src = Source("Stay (feat. Justin Bieber)", None, ["The Kid LAROI", "Justin Bieber"], "Stay", 141000)
    c = cand("STAY (with Justin Bieber)", ["The Kid LAROI", "Justin Bieber"], "STAY", 141800)
    assert matcher.score(src, c) >= matcher.AUTO_THRESHOLD


def test_queries_include_translit_for_cyrillic():
    qs = matcher.build_queries(Source("Звезда по имени Солнце", None, ["Кино"], None, None))
    assert qs[0] == 'track:"Звезда по имени Солнце" artist:"Кино"'
    assert any("kino" in q and "zvezda" in q for q in qs)


def test_parse_track_ref():
    from app.spotify_client import parse_track_ref
    tid = "4uLU6hMCjMI75M1A2tKUQC"
    assert parse_track_ref(f"https://open.spotify.com/track/{tid}?si=abc") == f"spotify:track:{tid}"
    assert parse_track_ref(f"spotify:track:{tid}") == f"spotify:track:{tid}"
    assert parse_track_ref("nonsense") is None


def test_parse_yandex_token():
    from app.yandex_client import parse_token
    assert parse_token("https://music.yandex.ru/#access_token=y0_ABC&token_type=bearer&expires_in=1") == "y0_ABC"
    assert parse_token("  y0_XYZ ") == "y0_XYZ"


def test_parse_yandex_links():
    from app.yandex_client import parse_link
    assert parse_link("https://music.yandex.ru/users/friend.login/playlists/3?utm_source=x") == \
        {"type": "user_playlist", "user": "friend.login", "kind": 3}
    assert parse_link("music.yandex.com/users/vasya/tracks") == {"type": "user_likes", "user": "vasya"}
    assert parse_link("https://music.yandex.ru/playlists/lk.1a2b-3c4d?ref=share") == \
        {"type": "uuid_playlist", "uuid": "lk.1a2b-3c4d"}
    import pytest
    with pytest.raises(ValueError):
        parse_link("https://open.spotify.com/playlist/abc")


def test_isrc_match():
    src = Source("Группа крови", None, ["Кино"], "Группа крови", 286000, isrc="SUA018800100")
    assert matcher.build_queries(src)[0] == "isrc:SUA018800100"
    c = cand("Gruppa krovi (Remastered)", ["Kino"], "Legenda", 290000)
    c["isrc"] = "sua018800100"
    assert matcher.score(src, c) >= 97
