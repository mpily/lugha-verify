"""SMS segment counter: GSM-7 (160/153) vs UCS-2 (70/67)."""
from __future__ import annotations

_BASIC = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
             "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_EXT = set("^{}\\[~]|€\f")


def is_gsm7(text: str) -> bool:
    return all(c in _BASIC or c in _EXT for c in text)


def count_segments(text: str) -> tuple[str, int]:
    if is_gsm7(text):
        units = sum(2 if c in _EXT else 1 for c in text)
        single, multi, enc = 160, 153, "GSM-7"
    else:
        units = sum(2 if ord(c) > 0xFFFF else 1 for c in text)
        single, multi, enc = 70, 67, "UCS-2"
    if units <= single:
        return enc, 1
    return enc, -(-units // multi)
