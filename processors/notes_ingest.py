"""Parser for notes.txt — the maintainer's Google Doc mirror.

Grammar (README "Daily note-taking convention"), one song per line:

    title [±N] [女版|男版] [!] [唱] [URL] [／artist] [# comment]

Membership rule: every song line of the Google Doc (notes.txt, parsed with
`listed=True`) belongs to the setlist — the Doc is the performer's song list,
and markers (key offset / gender / `!` / `唱`) are only presets on top. Lines
from notes_inbox.txt (browser-extension picks) join the setlist only when they
carry a marker; a bare inbox pick feeds the research corpus only.

The parser is deliberately tolerant of rich-text paste noise (bullets,
full-width spaces, tabs) but NEVER silently drops what it can't parse —
unrecognized fragments come back as warnings so a typo'd song can't
vanish without a trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class NoteEntry:
    title: str
    artist_hint: str | None = None
    key_offset: int = 0
    gender_variant: str | None = None    # "female" / "male" / None
    favorite: bool = False
    sing: bool = False                   # bare 唱 marker (original key)
    youtube_url: str | None = None
    comment: str | None = None
    lineno: int = 0
    raw_line: str = ""
    warnings: list[str] = field(default_factory=list)
    listed: bool = False                 # a Google Doc line (always in setlist)

    @property
    def has_markers(self) -> bool:
        """Any performance marker (key / gender / `!` / `唱`)."""
        return (self.sing or self.favorite or self.key_offset != 0
                or self.gender_variant is not None)

    @property
    def in_setlist(self) -> bool:
        """Doc lines always; inbox picks only when they carry a marker."""
        return self.listed or self.has_markers


# Lines that are structure, not songs: comments, headings, dividers.
_SKIP_LINE = re.compile(r"^\s*(#|＃|【|=|─|—{2,}|-{3,}|\*{3,}|_{3,})")
# Leading bullet / list markers from rich-text paste.
_BULLET = re.compile(r"^\s*[•·◦‣▪–*]\s*|^\s*-\s+")
_KEY = re.compile(r"^(?:[+\-−]|降|升)(\d{1,2})$")
_URL = re.compile(r"^https?://\S+$|^youtu\.be/\S+$")
_GENDER = {"女版": "female", "男版": "male"}
# Markers the Doc often glues onto neighbours ("情歌-7", "女版-4", "男版！").
_GLUED = re.compile(r"(女版|男版|[+\-−]\d{1,2}(?!\d)|[!！]|(?<![\w])[降升]\d{1,2}(?!\d))")
# "或男版" / "可以降2": an alternative the singer may use — kept as a comment.
_ALT = ("或", "可以")


def _split_token(tok: str) -> list[str]:
    """Split glued markers off a token: '情歌-7' → ['情歌', '-7']."""
    if _URL.match(tok):
        return [tok]
    return [p for p in _GLUED.split(tok) if p]


def _normalize(line: str) -> str:
    """Collapse paste noise: full-width spaces, tabs, repeated spaces."""
    line = line.replace("　", " ").replace("\t", " ")
    line = _BULLET.sub("", line)
    return re.sub(r" {2,}", " ", line).strip()


def parse_notes(text: str, listed: bool = False) -> tuple[list[NoteEntry], list[str]]:
    """Parse notes.txt content → (entries, global_warnings).

    `listed=True` marks every entry as a Google Doc line (setlist member).

    Later duplicate lines override earlier ones (the doc's latest word
    wins), with a warning so silent self-conflicts are visible.
    """
    entries: dict[str, NoteEntry] = {}
    global_warnings: list[str] = []

    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or _SKIP_LINE.match(raw):
            continue
        line = _normalize(raw)
        # "錄音歌序：" style section labels are headings, not songs.
        if not line or line.endswith(("：", ":")):
            continue

        # Split off the trailing free-form comment first.
        comment = None
        for marker in ("#", "＃"):
            if marker in line:
                line, comment = line.split(marker, 1)
                comment = comment.strip() or None
                line = line.strip()
                break
        if not line:
            continue

        alts = []
        tokens = []
        for tok in line.split(" "):
            if tokens and tok.startswith(_ALT):
                alts.append(tok)
            else:
                tokens.extend(_split_token(tok))
        if not tokens:
            continue
        entry = NoteEntry(title=tokens[0], lineno=lineno,
                          raw_line=raw.strip(), comment=comment, listed=listed)
        if alts:
            entry.comment = " ".join(alts + ([comment] if comment else []))
            entry.sing = True

        marker_seen = False
        for tok in tokens[1:]:
            if not tok:
                continue
            m = _KEY.match(tok)
            if m:
                sign = -1 if tok[0] in "-−降" else 1
                entry.key_offset = sign * int(m.group(1))
                if abs(entry.key_offset) > 6:
                    entry.warnings.append(
                        f"key_offset {entry.key_offset:+d} outside typical ±6")
                marker_seen = True
            elif tok in _GENDER:
                entry.gender_variant = _GENDER[tok]
                marker_seen = True
            elif tok in ("!", "！"):
                entry.favorite = True
                marker_seen = True
            elif tok == "唱":
                entry.sing = True
                marker_seen = True
            elif _URL.match(tok):
                entry.youtube_url = tok
            elif not marker_seen:
                # Still inside a multi-word title (e.g. English titles).
                entry.title += " " + tok
            else:
                entry.warnings.append(f"unrecognized token {tok!r}")

        # ／artist (or /artist) disambiguation, checked after title is final.
        for sep in ("／", "/"):
            if sep in entry.title:
                entry.title, artist = entry.title.split(sep, 1)
                entry.title = entry.title.strip()
                entry.artist_hint = artist.strip() or None
                break

        if not entry.title:
            global_warnings.append(f"line {lineno}: no title after parsing: {raw.strip()!r}")
            continue

        if entry.title in entries:
            global_warnings.append(
                f"line {lineno}: duplicate entry for {entry.title!r} "
                f"(overrides line {entries[entry.title].lineno})")
        entries[entry.title] = entry

        for w in entry.warnings:
            global_warnings.append(f"line {lineno} ({entry.title}): {w}")

    return list(entries.values()), global_warnings


_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})")


def video_id(url: str | None) -> str | None:
    """YouTube video id from any common URL shape (None if unparseable)."""
    if not url:
        return None
    m = _VIDEO_ID.search(url)
    return m.group(1) if m else None


def format_line(entry: NoteEntry) -> str:
    """Render an entry back into notes grammar (inverse of parse_notes)."""
    parts = [entry.title + (f"／{entry.artist_hint}" if entry.artist_hint else "")]
    if entry.key_offset:
        parts.append(f"{entry.key_offset:+d}")
    if entry.gender_variant:
        parts.append({"female": "女版", "male": "男版"}[entry.gender_variant])
    if entry.favorite:
        parts.append("!")
    if entry.sing and not (entry.key_offset or entry.gender_variant or entry.favorite):
        parts.append("唱")
    if entry.youtube_url:
        parts.append(entry.youtube_url)
    if entry.comment:
        parts.append(f"# {entry.comment}")
    return " ".join(parts)


def merge_inbox(doc: list[NoteEntry],
                inbox: list[NoteEntry]) -> tuple[list[NoteEntry], list[str]]:
    """Fold notes_inbox.txt (browser-extension picks) into the Doc entries.

    The Google Doc stays the source of truth, but only for what it actually
    says: a Doc line's performance markers win when it has any, otherwise
    the inbox's markers apply; a Doc line without a URL takes the inbox's
    pinned video. Inbox-only titles are appended as-is.
    """
    by_title = {e.title: e for e in doc}
    merged = list(doc)
    warnings: list[str] = []
    for ib in inbox:
        d = by_title.get(ib.title)
        if d is None:
            merged.append(ib)
            continue
        if not d.has_markers and ib.has_markers:
            d.key_offset, d.gender_variant = ib.key_offset, ib.gender_variant
            d.favorite, d.sing = ib.favorite, ib.sing
        elif d.has_markers and ib.has_markers and (
                (d.key_offset, d.gender_variant) != (ib.key_offset, ib.gender_variant)):
            warnings.append(
                f"{d.title}: Google Doc says {format_line(d)!r}, inbox says "
                f"{format_line(ib)!r} — Doc wins; edit the Doc to change it")
        if not d.youtube_url and ib.youtube_url:
            d.youtube_url = ib.youtube_url
        if not d.artist_hint and ib.artist_hint:
            d.artist_hint = ib.artist_hint
    return merged, warnings
