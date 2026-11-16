"""Сопоставление треков Яндекс Музыки с результатами поиска Spotify."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

AUTO_THRESHOLD = 85.0    # от этого балла трек принимается автоматически
REVIEW_THRESHOLD = 60.0  # от этого балла трек уходит на ручную проверку
GOOD_ENOUGH = 75.0       # после двух запросов дальше не ищем, если уже есть такой кандидат

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "і": "i", "ї": "yi", "є": "ye", "ґ": "g",
}

# Пометки, которые меняют суть записи: ремикс, живое исполнение и оригинал — разные треки.
# Вид версии → шаблоны (ищутся в названии и версии трека, а для части видов ещё в альбоме и артисте).
VERSION_KINDS: dict[str, str] = {
    "remix": r"remix(es|ed)?|rmx|re-?mix|bootleg|rework|re-?edit|vip mix|club mix|dub mix|extended mix|mashup|"
             r"ремикс|ремикс[ыа]?",
    # «Live Forever», «Cover Me», «Demons» — обычные названия, поэтому эти пометки ищем только в скобках или после тире
    "live": r"[\(\[]\s*live|[-–—]\s*live|live at|live from|live in|live version|live session|unplugged|in concert|"
            r"концерт\w*|живьём|живое исполнение",
    "acoustic": r"acoustic|акустик\w*|акустическ\w*",
    "instrumental": r"instrumental|инструментал\w*|минус(?:овка)?",
    "karaoke": r"karaoke|караоке|in the style of|made famous by|originally performed by|backing track",
    "cover": r"[\(\[]\s*cover|[-–—]\s*cover|cover version|кавер\w*|[\(\[]\s*tribute",
    "sped": r"slowed|sped up|speed up|speeded up|nightcore|reverb|daycore|замедленн\w*|ускоренн\w*",
    "arrangement": r"piano version|orchestral|symphonic|lullaby|8-bit|8 bit|string quartet|a cappella|acapella",
    "demo": r"[\(\[]\s*demo|[-–—]\s*demo|demo version|демо(?:-?версия)?",
}
VERSION_NAMES = {
    "remix": "ремикс", "live": "живое исполнение", "acoustic": "акустическая версия",
    "instrumental": "инструментал", "karaoke": "караоке", "cover": "кавер", "sped": "ускоренная/замедленная версия",
    "arrangement": "переаранжировка", "demo": "демо",
}
# По альбому и имени артиста узнаём только то, что там правда пишут («Live at Wembley», «Karaoke Hits»,
# «Vitamin String Quartet», «Rockabye Baby!»), а не «Remix» в названии сборника.
_ALBUM_KINDS = ("live", "karaoke", "cover", "arrangement")
_ARTIST_RE = re.compile(r"karaoke|караоке|tribute|string quartet|rockabye baby|lullaby|piano tribute|"
                        r"8-bit|8 bit|cover band|кавер", re.I)
_KIND_RE = {k: re.compile(rf"(?<![\w])(?:{v})(?![\w])", re.I) for k, v in VERSION_KINDS.items()}

# Пометки, которые на суть не влияют и только мешают сравнению.
NOISE_RE = re.compile(
    r"\b(remaster(ed)?(\s+\d{4})?|\d{4}\s+remaster(ed)?|original mix|radio edit|album version|"
    r"single version|explicit|clean|mono|stereo|deluxe( edition)?|bonus track)\b",
    re.I,
)
FEAT_RE = re.compile(r"[\(\[]?\s*\b(feat\.?|ft\.?|featuring|при уч\.?)\s+[^\)\]]*[\)\]]?", re.I)


def translit(s: str) -> str:
    return "".join(_TRANSLIT.get(ch, _TRANSLIT.get(ch.lower(), ch)) for ch in s)


def has_cyrillic(s: str) -> bool:
    return bool(re.search(r"[а-яёіїєґ]", s, re.I))


def normalize(s: str) -> str:
    """Нижний регистр, без feat., без «шумовых» пометок, без пунктуации, кириллица → латиница."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower().replace("ё", "е")
    s = FEAT_RE.sub(" ", s)
    s = NOISE_RE.sub(" ", s)
    s = re.sub(r"[\(\)\[\]\-–—_/\\.,:;!?'\"«»&+*#]", " ", s)
    s = translit(s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


def tags_of(s: str) -> set[str]:
    """Виды версии по тексту названия: {"remix", "live", ...}."""
    low = NOISE_RE.sub(" ", unicodedata.normalize("NFKC", s or "").lower())
    return {k for k, rx in _KIND_RE.items() if rx.search(low)}


def version_kinds(title: str, album: str | None = None, artists: list[str] | None = None) -> set[str]:
    kinds = tags_of(title)
    kinds |= {k for k in tags_of(album or "") if k in _ALBUM_KINDS}
    if any(_ARTIST_RE.search(a or "") for a in artists or []):
        kinds.add("cover")
    return kinds


@dataclass
class Source:
    title: str
    version: str | None
    artists: list[str]
    album: str | None
    duration_ms: int | None
    isrc: str | None = None

    @property
    def full_title(self) -> str:
        return f"{self.title} ({self.version})" if self.version else self.title


def _cand_fields(cand: dict) -> tuple[str, list[str], str]:
    """Название, артисты, альбом кандидата: и из ответа Spotify, и из сохранённого вида для страницы проверки."""
    arts = cand.get("artists") or []
    if isinstance(arts, str):
        arts = [a.strip() for a in arts.split(",")]
    arts = [a.get("name", "") if isinstance(a, dict) else a for a in arts]
    album = cand.get("album") or ""
    if isinstance(album, dict):
        album = album.get("name") or ""
    return cand.get("name") or "", [a for a in arts if a], album


def _parts(src: Source, cand: dict) -> dict:
    """Составляющие оценки: похожесть названия, артиста, альбома, разница длительности, виды версий."""
    c_title, c_artists, c_album = _cand_fields(cand)

    def _sim(a: str, b: str) -> float:
        # token_set прощает лишние слова, ratio штрафует за них ("love" vs "love me do")
        return 0.6 * fuzz.token_set_ratio(a, b) + 0.4 * fuzz.ratio(a, b)

    t2 = normalize(c_title)
    title_sim = max(_sim(normalize(src.full_title), t2), _sim(normalize(src.title), t2))
    src_art = [normalize(a) for a in src.artists if a] or [""]
    cand_art = [normalize(a) for a in c_artists] or [""]
    main = max(fuzz.token_sort_ratio(src_art[0], ca) for ca in cand_art)
    joined = fuzz.token_set_ratio(" ".join(src_art), " ".join(cand_art))
    c_dur = cand.get("duration_ms")
    return {
        "title": title_sim,
        "artist": max(main, joined) if any(src_art) else 100.0,
        "album": fuzz.token_set_ratio(normalize(src.album), normalize(c_album)) if src.album and c_album else None,
        "dur_diff": abs(src.duration_ms - c_dur) / 1000 if src.duration_ms and c_dur else None,
        "src_kinds": version_kinds(src.full_title, src.album, src.artists),
        "cand_kinds": version_kinds(c_title, c_album, c_artists),
        "isrc": bool(src.isrc and cand.get("isrc") and src.isrc.upper() == cand["isrc"].upper()),
    }


def score(src: Source, cand: dict) -> float:
    """Оценка 0–100 для кандидата из ответа Spotify /search (объект track)."""
    p = _parts(src, cand)
    s = 40 * p["title"] / 100 + 30 * p["artist"] / 100           # название (40) и артисты (30)
    d = p["dur_diff"]                                                # длительность (20)
    if d is None:
        s += 10
    elif d <= 3:
        s += 20
    elif d <= 10:
        s += 20 * (10 - d) / 7
    elif d > 30:
        s -= 10
    s += 5 if p["album"] is None else 10 * p["album"] / 100       # альбом (10)

    same_kind = p["src_kinds"] == p["cand_kinds"]
    if not same_kind:
        # Ремикс вместо оригинала (и наоборот) — штраф, и такой трек никогда не принимается сам:
        # максимум «спорный», решает человек.
        s -= 15 * len(p["src_kinds"] ^ p["cand_kinds"])
        s = min(s, AUTO_THRESHOLD - 1)
    elif p["isrc"]:
        # Совпал ISRC — это та же запись, даже если сервис подписал её иначе (латиницей, с другим альбомом).
        s = max(s, 97.0)
    return round(max(0.0, min(100.0, s)), 1)


def explain(src: Source, cand: dict) -> list[str]:
    """Почему кандидат не идеален: короткие причины по-русски (пусто, если всё совпало)."""
    p = _parts(src, cand)
    why = []
    for k in sorted(p["cand_kinds"] - p["src_kinds"]):
        why.append(f"найден {VERSION_NAMES[k]}, а в источнике обычная версия")
    for k in sorted(p["src_kinds"] - p["cand_kinds"]):
        why.append(f"в источнике {VERSION_NAMES[k]}, а найдено другое")
    if p["isrc"] and p["src_kinds"] == p["cand_kinds"]:
        return why
    if p["artist"] < 80:
        why.append("другой артист" if p["artist"] < 50 else "артист записан иначе")
    if p["title"] < 85:
        why.append("другое название" if p["title"] < 60 else "название немного отличается")
    if p["dur_diff"] is not None and p["dur_diff"] > 10:
        why.append(f"длительность отличается на {fmt_secs(p['dur_diff'])}")
    if p["album"] is not None and p["album"] < 50:
        why.append("другой альбом")
    return why


def fmt_secs(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec} с" if sec < 60 else f"{sec // 60}:{sec % 60:02d}"


def build_queries(src: Source) -> list[str]:
    """Поисковые запросы к Spotify, от самого точного к самому свободному."""
    artist = src.artists[0] if src.artists else ""
    title = FEAT_RE.sub(" ", src.title).strip()
    queries = []
    if src.isrc:
        queries.append(f"isrc:{src.isrc}")
    if artist:
        queries.append(f'track:"{title}" artist:"{artist}"')
    if has_cyrillic(title + artist):
        queries.append(f"{translit(artist.lower())} {translit(title.lower())}".strip())
    free = f"{artist} {title}"
    if src.version:
        free += f" {src.version}"
    queries.append(free.strip())
    seen, out = set(), []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def candidate_view(cand: dict, sc: float, src: Source | None = None) -> dict:
    return {
        "why": explain(src, cand) if src is not None else [],
        "uri": cand.get("uri"),
        "name": cand.get("name"),
        "artists": ", ".join(a.get("name", "") for a in cand.get("artists", [])),
        "album": (cand.get("album") or {}).get("name"),
        "duration_ms": cand.get("duration_ms"),
        "url": (cand.get("external_urls") or {}).get("spotify"),
        "score": sc,
    }


def classify(best: float) -> str:
    if best >= AUTO_THRESHOLD:
        return "auto"
    if best >= REVIEW_THRESHOLD:
        return "review"
    return "not_found"


def kinds_differ(src: Source, cand: dict) -> bool:
    """Версии не совпадают (ремикс вместо оригинала и т. п.): такой трек сам не принимается."""
    p = _parts(src, cand)
    return p["src_kinds"] != p["cand_kinds"]
