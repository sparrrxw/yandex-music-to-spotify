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

# Пометки, которые меняют суть записи: ремикс и оригинал — разные треки.
SIGNIFICANT_TAGS = ("remix", "live", "acoustic", "instrumental", "cover", "karaoke",
                    "slowed", "sped up", "speed up", "nightcore", "edit", "rmx",
                    "ремикс", "акустика", "инструментал", "кавер")
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
    low = (s or "").lower()
    return {t for t in SIGNIFICANT_TAGS if re.search(rf"\b{re.escape(t)}\b", low)}


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


def score(src: Source, cand: dict) -> float:
    """Оценка 0–100 для кандидата из ответа Spotify /search (объект track)."""
    c_title = cand.get("name") or ""
    c_artists = [a.get("name", "") for a in cand.get("artists", [])]
    c_album = (cand.get("album") or {}).get("name") or ""
    c_dur = cand.get("duration_ms")

    # Название (40)
    t1, t2 = normalize(src.full_title), normalize(c_title)
    t1b = normalize(src.title)
    def _sim(a: str, b: str) -> float:
        # token_set прощает лишние слова, ratio штрафует за них ("love" vs "love me do")
        return 0.6 * fuzz.token_set_ratio(a, b) + 0.4 * fuzz.ratio(a, b)

    title_sim = max(_sim(t1, t2), _sim(t1b, t2))
    s = 40 * title_sim / 100

    # Артисты (30): лучшее совпадение для главного артиста + доля совпавших остальных
    src_art = [normalize(a) for a in src.artists if a] or [""]
    cand_art = [normalize(a) for a in c_artists if a] or [""]
    main = max(fuzz.token_sort_ratio(src_art[0], ca) for ca in cand_art)
    joined = fuzz.token_set_ratio(" ".join(src_art), " ".join(cand_art))
    s += 30 * max(main, joined) / 100

    # Длительность (20)
    if src.duration_ms and c_dur:
        diff = abs(src.duration_ms - c_dur) / 1000
        if diff <= 3:
            s += 20
        elif diff <= 10:
            s += 20 * (10 - diff) / 7
        elif diff > 30:
            s -= 10
    else:
        s += 10

    # Альбом (10)
    if src.album and c_album:
        s += 10 * fuzz.token_set_ratio(normalize(src.album), normalize(c_album)) / 100
    else:
        s += 5

    # Ремикс/лайв/акустика должны совпадать
    src_tags = tags_of(src.full_title)
    cand_tags = tags_of(c_title)
    if src_tags != cand_tags:
        s -= 15 * len(src_tags ^ cand_tags)

    # Совпал ISRC — это та же запись, даже если Spotify подписал её иначе
    # (латиницей, с другим альбомом). Но не даём ISRC перебить явное расхождение версий.
    if src.isrc and cand.get("isrc") and src.isrc.upper() == cand["isrc"].upper() and src_tags == cand_tags:
        s = max(s, 97.0)

    return round(max(0.0, min(100.0, s)), 1)


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


def candidate_view(cand: dict, sc: float) -> dict:
    return {
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
