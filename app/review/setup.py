"""Create the review items for one source text."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Candidate, ReviewItem, SourceText


@dataclass
class CandidateIn:
    text: str | None  # None = no candidate (speakers translate from scratch)
    origin: str = "uploaded"  # default_api|customer_model|uploaded
    model_ref: str | None = None


def create_review_items(db: Session, settings: Settings, source: SourceText,
                        candidates: dict[str, CandidateIn]) -> list[ReviewItem]:
    """One item per review-required target language. Machine Swahili (no
    customer Swahili) gets its own item too, so it can be verified and used
    as the source for reviewers who don't read the original language."""
    src_lang = source.campaign.source_lang
    items = []
    for lang, c in candidates.items():
        if lang == src_lang:
            continue
        if lang == "swh":
            if source.swahili_text or not settings.review_machine_swahili:
                continue
        elif not settings.needs_review(lang):
            continue
        cand = Candidate(source=source, lang=lang, text=c.text, origin=c.origin,
                         model_ref=c.model_ref)
        item = ReviewItem(candidate=cand, lang=lang, budget_kes=settings.payout_per_item_kes)
        db.add_all([cand, item])
        items.append(item)
    db.flush()
    return items
