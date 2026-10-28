# status.md — Lugha Verify product log (newest last)

## 2026-10-28
- Repo bootstrapped with CLAUDE.md (spec).
- Build step 1 done: data model (`app/models.py`), settings with fail-fast
  validation (`app/config.py`), phone encryption/hashing, pure SMS parser,
  GSM-7/UCS-2 segment counter, SMS templates, review state machine
  (`app/review/engine.py`), `choose_delivery_text` (`app/delivery.py`).
  58 tests pass (parser table, 24 state-machine scenarios on a fake clock,
  payouts, Hypothesis delivery-safety property, config, phones, segments).
- Choices beyond the spec (change if wanted):
  - A fan-out task past its 30 min expiry still counts if the speaker replies
    while the item is open; expiry only triggers the next wave.
  - Trusted speaker replies in time with stars < 4 and no correction → item
    closes `unverified`, paid in full, recipients get the fallback.
  - Unknown code and someone else's code get the same answer (no leak).
  - Duplicate replies to the same task are ignored (first one stands).
  - Messages from numbers not in `speakers` are logged, not answered.
  - Review-item `outcome` uses the spec's `verified|unverified|no_reply`;
    the translation row uses `machine|speaker_corrected|verified`.
- Deferred: Alembic migrations (tables via `create_all` for now), gold items
  (`is_gold` column exists, mixing not built), accept/reject counters from
  trusted-review rejections, airtime sending (payouts recorded `pending`).
- Note for step 5: the Space API accepts `src=swh` only, so English sources
  need EN→SW first (customer Swahili text, or another translator).
- User decisions (2026-10-28, replaces the first two choices above):
  - Reply to an expired fan-out task (30 min): stored as `late`, not paid,
    not counted. Speakers get a "task expired, no reply needed" SMS.
  - Stars < 4 with no correction: stored as `no_correction`, not paid, not
    counted; a later correction on the same code is accepted and paid. From
    the trusted speaker it sends the item to fan-out at once.
  - Unknown/someone else's code same answer; unknown numbers logged only: kept.
  - Trusted speaker's late reply still counts as a stage-2 response (spec §5).
- Swahili pivot (user decision 2026-10-28): with no customer Swahili, the
  NLLB Swahili gets its own review item (same rules; `REVIEW_MACHINE_SWAHILI`,
  default on) and is delivered to Swahili recipients only once verified.
  `speakers.languages` = languages a speaker is comfortable with, most
  preferred first; a speaker gets a task only if they read an available source
  and is shown it in their preferred language (`review_tasks.shown_source_*`).
  Swahili becomes an available source at its first valid reply (accept or
  correction); Kitaita speakers who only read Swahili get a wave right away.
  One open task per speaker per sentence; target items start before the
  Swahili item so multilingual speakers go to the target language first.
  Open: if the Swahili reviewer corrects the Swahili, the Kitaita candidate was
  still made from the uncorrected machine Swahili (re-translate? step 5).
- Build step 2 done: SMS outbox table (`outbound_sms`; rows written inside the
  engine's transaction, sent after commit, marked `sending` before each AT call
  so a crash can't double-send; network errors retried up to 5 times), AT SMS
  client (`app/sms/at_client.py`), FastAPI app (`app/main.py`):
  `POST /at/sms/inbound` (idempotent on AT's message id), `POST /at/sms/delivery`,
  admin `POST /api/speakers` (sends the opt-in prompt), `POST /api/campaigns`,
  `POST /api/campaigns/{id}/start`, `GET /api/campaigns/{id}`, `GET /health`;
  in-process scheduler (tick + flush every 30 s).
- AT callbacks are checked with a shared `?token=` (`AT_CALLBACK_TOKEN`), since
  AT doesn't sign callbacks. New env var.
- `scripts/simulate.py` (offline): the A/B/C/D/E pivot scenario end to end
  through the HTTP callbacks — PASS (20/20 SMS, 28 segments). 81 tests pass.
- NOT yet run: `simulate.py --live` against the AT sandbox.
  developers.africastalking.com and the AT API are blocked by this build
  session's network policy, so the client follows AT's documented
  `version1/messaging` form API from memory; first live run must confirm it.
- Re-translation (user decision 2026-10-28): when the Swahili reviewer
  changes the machine Swahili, every open target item whose candidate came
  from the Space (`origin=default_api`) is re-translated from the reviewed
  Swahili (`review_items.retranslate_from`, background job every 10 s,
  outside the lock). Swahili-only readers are tasked once the new draft is in.
  If the translator returns nothing, the old candidate is kept (not invented,
  not dropped). Each task stores the candidate it showed
  (`shown_candidate_text`), so ratings are compared against what each speaker
  actually saw. `DefaultApiTranslator` (Space, swh→dav only) retries with
  backoff for up to 3 min, then gives no candidate; it shows reviewers
  `kitaita_marked` so untranslated words appear as `[?word]`.
- Trusted speaker's late reply counting as a paid stage-2 reply: confirmed.
- Build step 3a, airtime payouts (`app/payouts.py`): one row per task (unique),
  committed as `sending` before the AT call and updated with AT's requestId
  after. Retried only when AT certainly didn't pay (connection never opened,
  or AT's status callback says Failed; max 3 attempts). Ambiguous errors
  (timeout after sending, 5xx, crash) → `unknown`, never auto-retried:
  reconcile against the AT dashboard. A row stuck in `sending` means the
  process died mid-call: same treatment. `POST /at/airtime/status` callback;
  duplicates are no-ops; `airtime_paid_total` grows on confirmed Success only.
  Settings now require PAYOUT_PER_ITEM_KES/2 >= 10 (AT airtime minimum).
  Request format (`recipients` JSON list, "KES 50") written from memory —
  confirm on the first sandbox run.
- Build step 3b, bulk delivery (`app/bulk.py`, `compose_message` in
  `app/delivery.py`): `POST /api/lists/{id}/recipients` (CSV `phone,
  preferred_lang`; bad rows reported by line number only). Bulk campaigns need
  a `list_id`; review items are created for every review-required language in
  the list (plus the machine-Swahili pivot when the source is English and no
  customer Swahili is given); languages with no uploaded candidate get a
  no-candidate item for now (step 5: ask the translator first). When all items
  are closed, each recipient gets one SMS (unique per campaign+recipient).
  Message rule: every sentence passes `choose_delivery_text`; if any sentence
  would fall back, the WHOLE message goes in the fallback (Swahili only if
  every sentence has customer or verified Swahili, else the source language).
  Hypothesis test at message level. Recipients can STOP/YES. Delivery reports
  update `deliveries.delivery_status` (send result is stored as `sent`, AT's
  report as e.g. `Success`). Research campaigns end as `done`, no delivery.
- simulate.py offline: PASS, now including a 6-recipient list (one STOPs):
  2 Kidaw'ida recipients got the verified text, the Swahili recipient got the
  English fallback (Swahili had only one reply), nothing unverified sent.
  104 tests pass.
- Build step 4, customer portal (`app/web/portal.py`, Jinja2 templates):
  login with ADMIN_TOKEN (HttpOnly SameSite=Strict cookie holding an HMAC of
  the token, CSRF value on every form), recipient-list upload (file or
  paste), speakers (add → opt-in SMS; list with status, accept rate with n,
  airtime paid), new campaign (bulk: message split into sentences, optional
  Swahili paired sentence by sentence or kept whole if counts differ;
  research: CSV `source, lang, candidate` + model name), campaign page
  (start, per sentence × language state/result, deliveries, airtime total),
  export CSV/JSONL. No JavaScript; pages < 8 KB; checked at 375 px width
  (no horizontal scroll). Shared logic moved to `app/services.py` so the
  REST API and portal call the same code; `GET /api/campaigns/{id}/export`.
- Consent export (`app/exports.py`, never-cut item): only tasks with SHARE,
  on closed items, as anonymous records (no phone, speaker id, campaign or
  task id; stable HMAC record id for de-duplication). Goes to
  `SHARED_DATA_URL` if the customer set it, else local `shared_data.jsonl`.
  Runs every 5 min; Hypothesis test that non-consented tasks never appear.
  Open: shared records include the source sentence the speaker saw (the
  customer's text). The customer opts in by setting SHARED_DATA_URL; confirm
  that is enough or add an explicit per-campaign customer switch.
- 113 tests pass; simulate.py PASS. No login rate limiting yet.
