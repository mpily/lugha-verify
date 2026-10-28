"""choose_delivery_text: the single place that decides what a recipient gets.

Rule (CLAUDE.md §2): in a review-required language a recipient gets either a
`verified` translation or the fallback. Machine or speaker_corrected text in
a review-required language is never returned, on any path.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeliveryChoice:
    text: str
    lang: str
    was_fallback: bool


def fallback(source_text: str, source_lang: str, fallback_lang: str,
             swahili_text: str | None) -> DeliveryChoice:
    if fallback_lang == "swh" and swahili_text:
        return DeliveryChoice(swahili_text, "swh", True)
    return DeliveryChoice(source_text, source_lang, True)


def choose_delivery_text(*, recipient_lang: str, source_text: str, source_lang: str,
                         fallback_lang: str, swahili_text: str | None,
                         needs_review: bool, translation_text: str | None,
                         translation_status: str | None) -> DeliveryChoice:
    """translation_* describe the best translation in recipient_lang (or None).
    needs_review = settings.needs_review(recipient_lang)."""
    fb = fallback(source_text, source_lang, fallback_lang, swahili_text)
    try:
        if recipient_lang == source_lang:
            return DeliveryChoice(source_text, source_lang, False)
        if recipient_lang == "swh" and swahili_text:
            return DeliveryChoice(swahili_text, "swh", False)
        if not translation_text or not translation_text.strip():
            return fb
        if needs_review:
            if translation_status == "verified":
                return DeliveryChoice(translation_text, recipient_lang, False)
            return fb
        return DeliveryChoice(translation_text, recipient_lang, False)
    except Exception:  # noqa: BLE001  any surprise -> fallback, never machine text
        return fb
