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
