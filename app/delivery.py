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
    needs_review = settings.needs_review_for(recipient_lang, customer_swahili).
    swahili_text must be customer-written or `verified` Swahili, never
    unreviewed machine Swahili (it is also used as the fallback)."""
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


@dataclass(frozen=True)
class SentenceInfo:
    source_text: str
    customer_swahili: str | None
    verified_swahili: str | None
    translation_text: str | None     # in the recipient's language
    translation_status: str | None


def compose_message(*, recipient_lang: str, source_lang: str, fallback_lang: str,
                    sentences: list[SentenceInfo],
                    needs_review_for) -> DeliveryChoice:
    """Whole message for one recipient. Every sentence goes through
    choose_delivery_text; if any sentence would fall back, the whole message is
    sent in the fallback, so nobody gets a mixed-language message."""
    choices = [choose_delivery_text(
        recipient_lang=recipient_lang, source_text=s.source_text, source_lang=source_lang,
        fallback_lang=fallback_lang,
        swahili_text=s.customer_swahili or s.verified_swahili,
        needs_review=needs_review_for(recipient_lang, bool(s.customer_swahili)),
        translation_text=s.translation_text, translation_status=s.translation_status)
        for s in sentences]
    if choices and all(not c.was_fallback and c.lang == recipient_lang for c in choices):
        return DeliveryChoice(" ".join(c.text for c in choices), recipient_lang, False)
    sw = [s.customer_swahili or s.verified_swahili for s in sentences]
    if fallback_lang == "swh" and all(sw):
        return DeliveryChoice(" ".join(sw), "swh", True)
    return DeliveryChoice(" ".join(s.source_text for s in sentences), source_lang, True)
