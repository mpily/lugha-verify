from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class MTResult:
    text: str | None  # None = no candidate (never a guess)
    model_ref: str | None


class Translator(Protocol):
    def translate(self, text: str, src: str, tgt: str) -> MTResult: ...


class FakeTranslator:
    """Tests: a fixed table {(text, tgt): output}; anything else -> None."""

    def __init__(self, table: dict[tuple[str, str], str] | None = None):
        self.table = table or {}
        self.calls: list[tuple[str, str, str]] = []

    def translate(self, text: str, src: str, tgt: str) -> MTResult:
        self.calls.append((text, src, tgt))
        return MTResult(self.table.get((text, tgt)), "fake")
