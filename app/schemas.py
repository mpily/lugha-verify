"""Request/response models shared by the REST API and the portal."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SpeakerIn(BaseModel):
    phone: str
    languages: list[str] = Field(min_length=1, description="most preferred first")
    trusted_for: list[str] = []
    source: Literal["customer", "network"] = "customer"


class SpeakerOut(BaseModel):
    id: int
    languages: list[str]
    trusted_for: list[str]
    source: str
    opted_in: bool
    opted_out: bool


class CandidateBody(BaseModel):
    text: str | None
    origin: Literal["default_api", "customer_model", "uploaded"] = "uploaded"
    model_ref: str | None = None


class SourceBody(BaseModel):
    text: str = Field(min_length=1)
    swahili_text: str | None = None
    candidates: dict[str, CandidateBody] = {}


class CampaignIn(BaseModel):
    mode: Literal["bulk", "research"] = "research"
    source_lang: Literal["eng", "swh"] = "eng"
    fallback_lang: Literal["eng", "swh"] = "eng"
    review_deadline_min: int | None = Field(None, gt=0)
    list_id: str | None = None  # required in bulk mode
    sources: list[SourceBody] = Field(min_length=1)
