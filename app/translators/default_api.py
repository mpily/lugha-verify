"""DefaultApiTranslator: the BantuGrammar HF Space (`TRANSLATE_API_URL`).
Contract: BantuGrammar `hf_space/api/CONTRACT.md` (api_version "1"):
src must be swh, tgt must be dav. The Space may be asleep, so retry with
backoff for up to `max_wait` seconds, then give up -> no candidate."""
from __future__ import annotations

import logging
import time
from typing import Callable

import httpx

from .base import MTResult

log = logging.getLogger(__name__)
SUPPORTED = {("swh", "dav")}


class DefaultApiTranslator:
    def __init__(self, url: str, max_wait: float = 180.0, timeout: float = 60.0,
                 sleep: Callable[[float], None] = time.sleep,
                 http: httpx.Client | None = None):
        self.url, self.max_wait, self.sleep = url, max_wait, sleep
        self.http = http or httpx.Client(timeout=timeout)

    def supports(self, src: str, tgt: str) -> bool:
        return (src, tgt) in SUPPORTED

    def translate(self, text: str, src: str, tgt: str) -> MTResult:
        if (src, tgt) not in SUPPORTED or not 1 <= len(text) <= 1000:
            return MTResult(None, None)
        waited, delay = 0.0, 5.0
        while True:
            try:
                r = self.http.post(self.url, json={"text": text, "src": src, "tgt": tgt})
                if r.status_code == 200:
                    return self._parse(r.json())
                if r.status_code not in (502, 503, 504, 429):
                    log.warning("translator HTTP %s", r.status_code)
                    return MTResult(None, None)
            except httpx.HTTPError as e:
                log.info("translator not reachable (%s), retrying", type(e).__name__)
            if waited >= self.max_wait:
                return MTResult(None, None)
            self.sleep(delay)
            waited += delay
            delay = min(delay * 2, 60.0)

    @staticmethod
    def _parse(body: dict) -> MTResult:
        ref = f"space:{body.get('pipeline_version', '?')}"
        if body.get("status") == "unknown" or not body.get("kitaita"):
            return MTResult(None, ref)
        # kitaita_marked wraps words with no known Kitaita as [?word]: show
        # reviewers exactly which words are still Swahili.
        return MTResult(body.get("kitaita_marked") or body["kitaita"], ref)
