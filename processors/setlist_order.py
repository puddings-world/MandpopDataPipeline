"""Ordering rules for setlist.jsonl — the performer's song menu.

Favorites (`!` in the Doc) are pinned to the top; everything else is grouped
by artist. Both blocks sort artists, then titles, by pinyin so the menu reads
like a phone contact list. Songs with no known artist go last.

`artist` values in the dataset come from LRC tags, YouTube titles and APIs,
so they are noisy: bilingual ("張韶涵 Angela Zhang"), simplified, several
artists ("陳奕迅, 王菲"), English-only ("JJ Lin"). `artist_group()` folds
those into one grouping key; English names are mapped to the Chinese name
that the corpus pairs them with elsewhere. A wrong artist (e.g. an uploader
channel) is fixed in the Doc with `歌名／歌手`, which wins over the dataset.
"""

from __future__ import annotations

import re
from collections import Counter

try:
    from zhconv import convert as _zh
except ImportError:                       # pragma: no cover - optional
    _zh = None

try:
    from pypinyin import lazy_pinyin as _pinyin
except ImportError:                       # pragma: no cover - optional
    _pinyin = None

_CJK = r"㐀-鿿豈-﫿"
_CJK_RUN = re.compile(f"[{_CJK}]+")
# Separators between several credited artists; only the first one groups.
_MULTI = re.compile(r"\s*(?:,|，|、|&|＆|/|／|×|\bfeat\.?|\bft\.?|\bwith\b)\s*",
                    re.IGNORECASE)
_BRACKET = re.compile(r"[（(〈《【\[]([^）)〉》】\]]*)[）)〉》】\]]")
_EDGE_PUNCT = re.compile(r"^[^\w.]+|[^\w.]+$")   # keep "F.I.R."
_LATIN = re.compile(r"[A-Za-z]")


# Characters that are both a valid traditional surname and a simplified form
# zhconv "corrects" (郁可唯 → 鬱可唯); 钟 as a surname is 鍾, not 鐘.
_NAME_CHARS = {"郁": "郁", "于": "于", "范": "范", "余": "余", "姜": "姜",
               "干": "干", "钟": "鍾", "游": "游", "冯": "馮"}
_NAME_SPLIT = re.compile("([" + "".join(_NAME_CHARS) + "])")


def _trad(s: str) -> str:
    """Traditional spelling of a name, leaving surname characters alone."""
    if not _zh:
        return s
    return "".join(_NAME_CHARS.get(part, part) if len(part) == 1 and part in _NAME_CHARS
                   else _zh(part, "zh-tw") for part in _NAME_SPLIT.split(s) if part)


def _first_credit(name: str) -> str:
    return _MULTI.split(name.strip())[0] or name.strip()


def _label(name: str | None) -> str:
    """Display form of the first credited artist, characters kept as written."""
    if not name or not name.strip():
        return ""
    s = _first_credit(name)
    inner = [m for m in _BRACKET.findall(s) if _CJK_RUN.search(m)]
    s = _BRACKET.sub(" ", s)
    runs = _CJK_RUN.findall(s)
    if runs:
        return runs[0]
    if inner:                             # "en（王翊恩）" → 王翊恩
        return _CJK_RUN.findall(inner[0])[0]
    s = _EDGE_PUNCT.sub("", re.sub(r"\s+", " ", s))
    return s if _LATIN.search(s) else ""


def build_aliases(artists) -> dict[str, str]:
    """English name → Chinese name, learned from bilingual credits.

    "林俊傑 JJ Lin" anywhere in the corpus lets a bare "JJ Lin" join 林俊傑.
    """
    seen: dict[str, Counter] = {}
    for raw in artists:
        if not raw:
            continue
        s = _BRACKET.sub(" ", _first_credit(raw))
        runs = _CJK_RUN.findall(s)
        latin = _EDGE_PUNCT.sub("", re.sub(r"\s+", " ", _CJK_RUN.sub(" ", s)))
        if runs and _LATIN.search(latin):
            seen.setdefault(latin.lower(), Counter())[_trad(runs[0])] += 1
    return {k: c.most_common(1)[0][0] for k, c in seen.items()}


def artist_group(name: str | None, aliases: dict[str, str] | None = None) -> str:
    """Grouping key for a raw artist credit ("" when unknown).

    Simplified spellings are shown in traditional characters.
    """
    label = _label(name)
    if not label:
        return ""
    if not _CJK_RUN.search(label):
        return (aliases or {}).get(label.lower(), label)
    return _trad(label)


def _sort_key(text: str) -> str:
    if _pinyin:
        return " ".join(_pinyin(text)).lower()
    return text.lower()


def order_setlist(rows: list[dict], aliases: dict[str, str] | None = None) -> list[dict]:
    """Return rows in menu order and stamp `artist_group` + `order` on each.

    Each row needs `title`, `artist` (may be None) and `favorite`.
    """
    for r in rows:
        r["artist_group"] = artist_group(r.get("artist"), aliases)

    def key(r: dict) -> tuple:
        group = r["artist_group"]
        return (not r.get("favorite"), group == "", _sort_key(group),
                _sort_key(r["title"]))

    ordered = sorted(rows, key=key)
    for i, r in enumerate(ordered):
        r["order"] = i
    return ordered
