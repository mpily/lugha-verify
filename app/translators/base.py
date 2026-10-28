from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class MTResult:
    text: str | None  # None = no candidate (never a guess)
    model_ref: str | None


class Translator(Protocol):
    def supports(self, src: str, tgt: str) -> bool: ...
    def translate(self, text: str, src: str, tgt: str) -> MTResult: ...


class FakeTranslator:
    """Tests: a fixed table {(text, tgt): output}; anything else -> None."""

    def __init__(self, table: dict[tuple[str, str], str] | None = None,
                 pairs: set[tuple[str, str]] | None = None):
        self.table = table or {}
        self.pairs = pairs if pairs is not None else {("swh", "dav")}
        self.calls: list[tuple[str, str, str]] = []

    def supports(self, src: str, tgt: str) -> bool:
        return (src, tgt) in self.pairs

    def translate(self, text: str, src: str, tgt: str) -> MTResult:
        self.calls.append((text, src, tgt))
        return MTResult(self.table.get((text, tgt)), "fake")
