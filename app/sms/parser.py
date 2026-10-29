"""Pure SMS parser (CLAUDE.md §6). No DB access: whether a code is known,
expired or someone else's is decided by the review engine."""
from __future__ import annotations

import re
from dataclasses import dataclass

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
KEYWORDS = {"YES", "STOP", "HELP"}
_QUOTES = {"’": "'", "‘": "'", "ʼ": "'", "`": "'",
           "“": '"', "”": '"'}


def normalize_text(s: str) -> str:
    for a, b in _QUOTES.items():
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def normalize_for_compare(s: str) -> str:
    """Used to decide whether two corrections are 'identical'."""
    s = normalize_text(s).casefold()
    return re.sub(r"[\s.!?,;:]+$", "", s)


@dataclass(frozen=True)
class Keyword:
    word: str  # YES | STOP | HELP


@dataclass(frozen=True)
class Reply:
    code: str
    stars: int | None
    text: str | None
    share: bool


@dataclass(frozen=True)
class ParseError:
    reason: str  # empty | bad_code | bad_stars | no_content


Parsed = Keyword | Reply | ParseError


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1].strip()
    return s


def strip_keyword(text: str, keyword: str | None) -> str:
    """On a shared shortcode every message starts with our keyword
    (AT_KEYWORD), e.g. "LUGHA K7P2 5". Drop it; text without it is kept as is."""
    if not keyword:
        return text
    first, _, rest = text.partition(" ")
    if first.rstrip(".,:;!").upper() == keyword.upper():
        return rest.strip()
    return text


def parse_sms(raw: str, keyword: str | None = None) -> Parsed:
    text = strip_keyword(normalize_text(raw or ""), keyword)
    if not text:
        return ParseError("empty")
    bare = re.sub(r"[.!]+$", "", text).upper()
    if bare in KEYWORDS:
        return Keyword(bare)
    tokens = text.split(" ")
    if tokens[0].upper() == "STOP":  # always honour opt-out
        return Keyword("STOP")
    code = tokens[0].upper()
    if len(code) != 4 or any(c not in CODE_ALPHABET for c in code):
        return ParseError("bad_code")
    rest = tokens[1:]
    share = False
    if rest and rest[-1].upper().rstrip(".!") == "SHARE":
        share = True
        rest = rest[:-1]
    stars = None
    if rest and rest[0].isdigit():
        if len(rest[0]) == 1 and 1 <= int(rest[0]) <= 5:
            stars = int(rest[0])
            rest = rest[1:]
        else:
            return ParseError("bad_stars")
    body = _strip_quotes(" ".join(rest).strip()) or None
    if stars is None and body is None:
        return ParseError("no_content")
    return Reply(code=code, stars=stars, text=body, share=share)
