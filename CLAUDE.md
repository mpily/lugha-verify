# CLAUDE.md — Lugha Verify (Africa's Talking Marketplace plugin)

Working name: **Lugha Verify** (rename freely). Repo: `mpily/lugha-verify`.
Built for the Africa's Talking Open Hackathon (Nairobi, 29 Oct 2026) and
submitted to the AT Marketplace (marketplace.africastalking.dev).

One sentence: **send a message in English or Swahili; it reaches every
recipient in their own language, checked by paid native speakers over SMS
first; every check becomes evaluation/training data.**

Related repos (do not copy code from them):
- `mpily/BantuGrammar` — research pipeline + HF Space with the translation API
  (default machine translator for this plugin, called over HTTP).
- `mpily/bantugrammar-app` — static learner/Scrabble site. Unrelated to this plugin.

---

## 1. Marketplace constraints (hard limits)

- **Docker image ≤ 1 GB.** No `torch`, `transformers`, model weights or
  anything from the research repo in the image. All machine translation is
  an HTTP call. Use a multi-stage build on `python:3.12-slim`, a
  `.dockerignore`, and check `docker images` before every push. Record the
  image size in `status.md`.
- **One instance per customer.** Each customer deploys their own container
  with their own database and their own AT credentials. SMS and airtime costs
  come from the *customer's* AT wallet. Nothing is shared between instances
  except through the optional central services in §7.
- Built on AT products: **Bulk SMS** (two-way, via shortcode/sender ID),
  **Airtime**. Optional: **USSD** (§9). No WhatsApp (not eligible, paid).
- **Assume a 2G user.** The customer portal is server-rendered HTML with
  minimal JS and no heavy assets; must work on a phone.
- Configurable through env vars only (§10). The Marketplace form needs
  name, slug, descriptions, logo, image URL, database needs, env vars,
  categories, pricing plan — keep `docs/marketplace_submission.md` filled in
  as you go.
- **Unknowns — ask the user, don't guess:** which database the Marketplace
  provisions (assume Postgres via `DATABASE_URL`; SQLite for local dev),
  whether the 1 GB also caps the database, registry push instructions,
  the public URL format for AT callbacks.

## 2. Non-negotiable rules

- **Never deliver unverified text in a review-required language.** A
  recipient gets either a `verified` translation or the fallback
  (English/Swahili) version. Machine output in a review-required language is
  never sent to recipients, under any timeout or error path. Enforce this in
  one function (`choose_delivery_text`) and test it exhaustively.
- **Don't invent translations.** If no translator is reachable, the result
  is "no candidate", not a guess.
- **Phone numbers**: stored encrypted (Fernet, key in env) for sending, plus a
  salted hash for lookups. Never log them in plain text. Never send them to
  any translation model.
- **Consent**: speakers opt in by SMS (`YES`) before receiving any task;
  `STOP` opts out of everything immediately. Per-task data-sharing consent is
  separate (§6) and defaults to **no**.
- **Money**: every airtime payout is recorded with AT's transaction ID before
  and after the call; payouts are idempotent (never pay the same task twice,
  even on retry or a duplicate callback).
- Secrets only in env vars; nothing secret in git.
- One change per commit; run tests before every push and every image build.
- Free tiers only for anything you host yourself. If something needs payment,
  stop and ask.

## 3. Modes

Both modes use the same review pipeline (§5).

**Bulk mode** (customer: government office, NGO, business, individual)
1. Customer uploads a recipient list (CSV: `phone, preferred_lang`) and a
   message, written in English and optionally also in Swahili.
2. Languages not in `REVIEW_REQUIRED_LANGS` (default: everything except
   `eng`, `swh`) are sent directly:
   - recipient's language = the source language → send as written;
   - English↔Swahili → use the customer-supplied version if given, else
     machine-translate (NLLB is good enough for these; configurable).
3. For each review-required language that has recipients: get a machine
   candidate (§8), run the review pipeline, then deliver to that language's
   recipients the verified text or, on deadline, the fallback.
4. Everything is stored (translations, scores, corrections) per §6.

**Research mode** (customer: researcher, lab, NGO evaluating a model)
1. Researcher uploads source sentences (optionally with their own candidate
   translations, or a model endpoint, §8) and a translator list.
2. Same review pipeline; no delivery to recipients.
3. Output: per-model, per-language metrics and a dataset export (§6).

## 4. Data model

SQLAlchemy 2 + Alembic. Postgres in production, SQLite in dev/tests.

```
speakers        id, phone_hash, phone_enc, languages[], source
                (customer|network), trusted_for[] (languages),
                opted_in_at, opted_out_at, accept_count, reject_count,
                airtime_paid_total
recipients      id, phone_hash, phone_enc, preferred_lang, list_id
campaigns       id, mode (bulk|research), source_lang, fallback_lang,
                created_at, review_deadline, status
source_texts    id, campaign_id, text, swahili_text (optional, customer-written)
candidates      id, source_id, lang, text,
                origin (default_api|customer_model|uploaded), model_ref
review_items    id, candidate_id, lang, state, started_at, closed_at,
                budget_kes, outcome (verified|unverified|no_reply)
review_tasks    id, review_item_id, speaker_id, code, wave, sent_at,
                expires_at, replied_at, stars, corrected_text,
                share_consent (bool), status
                (sent|replied|accepted|late|expired|invalid), is_gold
translations    source_id, lang, final_text,
                status (machine|speaker_corrected|verified), n_reviews
payouts         id, review_task_id (unique), speaker_id, amount_kes,
                at_transaction_id, status
deliveries      id, campaign_id, recipient_id, lang_sent, text_sent,
                was_fallback, at_message_id, segments, cost, delivery_status
inbound_sms     id, from_hash, text, received_at, parsed_ok, error
```

`translations` doubles as a translation memory: an identical source text in
the same language reuses a verified translation without a new review.

## 5. Review pipeline (the core: build and test this first)

One `review_item` per (source text, language). A state machine driven by a
scheduler tick (every 30 s, in-process; APScheduler or an asyncio loop) and
by inbound SMS. All timings and amounts come from config, defaults below.

**Budget**: `PAYOUT_PER_ITEM_KES = 100` per review item.

**Stage 1: trusted speaker.**
- If a speaker is `trusted_for` this language, send them the task first.
  Customer-provided trusted speakers take priority over network ones.
- Trusted reply within `TRUSTED_TIMEOUT_MIN = 15` → translation is
  `verified` (their corrected text, or the candidate if accepted unchanged).
  They are paid the full 100.
- No reply in 15 min → stage 2. If the trusted speaker replies later while
  stage 2 is still open, their reply counts as one of the two stage-2
  responses (paid 50), not as a trusted override.

**Stage 2: fan-out, first two to respond.**
- Send the task to a wave of `FANOUT_WAVE_SIZE = 5` eligible speakers
  (language matches, opted in, not opted out, no open task on this item),
  ordered by priority:
  1. customer-provided speakers, then
  2. network speakers (§7);
  within each group, by accept rate, then least-recently tasked.
- Each task expires after `SPEAKER_TIMEOUT_MIN = 30`. If fewer than 2 valid
  replies when a wave expires, send the next wave, until
  `campaign.review_deadline` (default 2 h after the campaign starts).
- **The first 2 valid replies win and split the budget: 50 each.** As soon as
  the 2nd arrives, close the item, mark all other open tasks `late`/closed, and
  send those speakers a short "Task K7P2 is filled, thank you. No reply
  needed." so nobody works unpaid. Late replies are stored (useful data) but
  not paid. Tell speakers this rule in the opt-in message.
- **Outcome** (*default, flagged as a choice the user can change*):
  - both accept the candidate (stars ≥ 4, no correction), or both corrections
    are identical after normalization → `verified`;
  - otherwise → `speaker_corrected`, **not delivered**; recipients get the
    fallback; the item goes to a trusted-review queue for later. Both
    responders are still paid.
- Only 1 valid reply by the deadline → that speaker is paid 50; status
  `speaker_corrected`; recipients get the fallback.
- No replies by the deadline → `no_reply`; fallback; nobody paid.

**Stage 3: delivery (bulk mode).** When every review item for a campaign is
closed or past its deadline, deliver each recipient's text via
`choose_delivery_text(recipient, item)`:
`verified` translation in their language → else the fallback
(customer-written Swahili/English if available, else source language).
Record `was_fallback`.

**Fraud and quality** (keep simple for the hackathon):
- Mix gold items (known-good translations) into network speakers' tasks at
  `GOLD_RATE = 0.1`; gold tasks are paid like normal ones.
- Speakers who fail gold items or whose replies a trusted reviewer later
  rejects sink in priority; at `MIN_ACCEPT_RATE` they stop receiving tasks.
- Reject replies that arrive in under `MIN_REPLY_SECONDS = 10` for sentences
  over 5 words (status `invalid`, unpaid, the speaker is told).

## 6. SMS formats, consent, data

**Task SMS** (fit in 160 GSM-7 chars where possible; split long sources into
sentences before review):
```
K7P2 Rate 1-5 & correct (Kidaw'ida):
"Choka yacha"
Reply: K7P2 5  or  K7P2 3 <your version>. Add SHARE to allow research use.
```
For items with no candidate: `Reply: K7P2 <your translation>`.

**Reply format**: `<CODE> <stars?> <text?> [SHARE]`
- `CODE`: 4 chars from an unambiguous alphabet (no 0/O, 1/I/L), unique among
  open tasks; case-insensitive.
- `K7P2 5` = accept unchanged with 5 stars. `K7P2 3 Choka iyacha` = 3 stars
  plus correction. `K7P2 Choka yacha` = translation (no-candidate items, or
  when stars are omitted: then stars = null).
- `SHARE` anywhere at the end (case-insensitive) sets `share_consent = true`
  for that task.
- Normalize: trim, collapse spaces, curly ’ → ', case-fold the code.
- The parser is a pure function with a table of tests (good, malformed,
  unknown code, expired code, someone else's code, `STOP`, `YES`, `HELP`).
- Unparseable replies get one helpful reply SMS, then are ignored.

**Data-sharing consent (per task)**: SMS has no buttons, so the per-task
"button" is the `SHARE` keyword, opt-in, default no. Where a real button
exists (USSD menu, customer portal) show it as a button.
- The customer always sees and exports all of their own instance's data.
- Only tasks with `share_consent = true` may leave the instance for other
  teams (§7 shared dataset). Enforce this in the export/upload function and
  test it.

**Research outputs** (`GET /api/campaigns/{id}/report`, CSV/JSONL export):
per language × model (`model_ref`): n items, mean stars with 95% CI,
≥4-star rate with Wilson CI, accept-unchanged rate, chrF of candidate vs
corrected, agreement between the two responders. **Always report n**; flag
n < 5 as low n.

## 7. Speaker network and shared data (central, optional)

Instances are isolated, so the inherited native-speaker network and the
shared dataset must be **central services the user runs**, reached by HTTP:

- `NETWORK_API_URL`: returns eligible network speakers for a language (and a
  way to send them tasks without exposing their numbers to customers:
  customers never see network speakers' phone numbers).
- `SHARED_DATA_URL`: receives only `share_consent = true` records,
  stripped of phone numbers and speaker identity.

**For the hackathon**: implement both behind interfaces (`SpeakerSource`,
`SharedDataSink`) with local implementations: the network is a seeded table
with `source = network`; the shared sink writes a local JSONL. If the env var
is set, use the HTTP implementation. Don't build the central service in this
repo; stop and ask when it's time.

## 8. Translators (machine candidates)

`Translator` interface with:
- `DefaultApiTranslator`: the BantuGrammar HF Space (`TRANSLATE_API_URL`).
  The Space may be asleep: retry with backoff for up to 3 min, then mark the
  item "no candidate" and continue (speakers translate from scratch).
- `CustomerModelTranslator`: the customer's own model, configured by
  `CUSTOMER_MODEL_URL`, `CUSTOMER_MODEL_KEY` (sent as a bearer header),
  `CUSTOMER_MODEL_REQUEST_TEMPLATE` (JSON with `{text}`, `{src}`, `{tgt}`),
  `CUSTOMER_MODEL_RESPONSE_PATH` (e.g. `$.translation`). If set, it is used
  instead of the default.
  - HTTPS only; refuse private, loopback and link-local addresses after DNS
    resolution (SSRF guard); 20 s timeout; rate limit.
  - Send only the text and language codes.
- `UploadedTranslator`: candidates uploaded with the source CSV.
Every candidate records `origin` and `model_ref`, so research reports can be
split by model.

## 9. Interfaces

- **AT callbacks**: `POST /at/sms/inbound`, `POST /at/sms/delivery`,
  `POST /at/airtime/status`. Verify they come from AT as far as AT allows;
  make all handlers idempotent.
- **Customer REST API** (token in `ADMIN_TOKEN`): upload lists, create
  campaigns, start, status, report, export.
- **Customer portal**: server-rendered (Jinja2), the same actions, works on
  a phone over 2G.
- **USSD (optional, cut first)**: speaker menu (1 my open tasks, 2 my
  earnings, 3 data-sharing preference, a real button).
- Develop and demo on the **AT sandbox**.

## 10. Environment variables

```
AT_USERNAME, AT_API_KEY, AT_SHORTCODE (or AT_SENDER_ID)
DATABASE_URL
PHONE_ENC_KEY, PHONE_HASH_SALT, ADMIN_TOKEN
TRANSLATE_API_URL            default: BantuGrammar Space
CUSTOMER_MODEL_URL, CUSTOMER_MODEL_KEY,
CUSTOMER_MODEL_REQUEST_TEMPLATE, CUSTOMER_MODEL_RESPONSE_PATH   (optional)
NETWORK_API_URL, SHARED_DATA_URL                                (optional)
REVIEW_REQUIRED_LANGS        default: all except eng,swh
PAYOUT_PER_ITEM_KES=100  TRUSTED_TIMEOUT_MIN=15  SPEAKER_TIMEOUT_MIN=30
FANOUT_WAVE_SIZE=5  REVIEW_DEADLINE_MIN=120  GOLD_RATE=0.1
MIN_ACCEPT_RATE=0.6  MIN_REPLY_SECONDS=10
```
Validate all of them at startup with Pydantic Settings; fail fast with a
clear message.

## 11. Repo layout

```
lugha-verify/
  CLAUDE.md  status.md  README.md
  docs/marketplace_submission.md   form answers, logo, pricing plan text
  app/main.py                FastAPI
  app/config.py              Pydantic Settings
  app/models.py  app/db.py   SQLAlchemy + Alembic migrations/
  app/review/                state machine, scheduler, payouts, fraud
  app/sms/                   parser, templates, segment counter, AT client
  app/translators/           interface + 3 implementations
  app/speakers/              SpeakerSource (local, http)
  app/delivery.py            choose_delivery_text + bulk send
  app/reports.py             metrics, export, SharedDataSink
  app/web/                   portal templates
  tests/
  scripts/simulate.py        fake speakers replying through the sandbox
  Dockerfile  .dockerignore
```

## 12. Testing

- **State machine** with a fake clock, covering at least:
  trusted replies in time; trusted timeout → fan-out; first two agree;
  first two disagree; one reply only; none; trusted replies late during
  stage 2; third reply after close (late, unpaid, notified); wave
  exhaustion; speaker opts out mid-task.
- **Delivery safety**: a test (property-based with Hypothesis) that no path
  ever sends a non-`verified` translation in a review-required language.
- **Payouts**: split math, idempotency under duplicate callbacks, total per
  item never exceeds the budget.
- **Parser**: the table in §6, including curly apostrophes and `SHARE`.
- **Consent**: shared export contains only `share_consent = true` records and
  no phone or speaker identity.
- **SSRF guard** for customer model URLs.
- SMS segment counter (GSM-7 vs UCS-2).
- `scripts/simulate.py`: an end-to-end run on the AT sandbox with simulated
  speakers. Run it before every image push; log the result in `status.md`.

## 13. Build order (deadline 29 Oct)

1. Data model, SMS parser, review state machine with fake clock and tests.
2. AT sandbox: inbound SMS, task sending, opt-in/STOP; simulate script.
3. Airtime payouts (idempotent) and bulk delivery with the fallback rule.
4. Minimal portal: upload CSV, create campaign, status, export.
5. Default translator (Space) with retry; research report.
6. Dockerfile ≤ 1 GB, push to the AT registry, fill in the Marketplace form.
7. If time allows: customer model endpoint, USSD, network HTTP adapters.

Cut from the bottom if late. Never cut: the delivery-safety rule, consent,
idempotent payouts.

## 14. Demo narrative

A county office sends one English health notice to a mixed contact list.
English and Swahili recipients get it at once. For Kidaw'ida, the machine
draft goes to a trusted speaker, who doesn't answer in 15 minutes. It fans
out, and two speakers reply by SMS within minutes and are paid KSh 50 airtime
each. They agree, so the verified Kidaw'ida version goes out. The report shows
the machine draft's score, and one speaker typed SHARE, so that correction
joins the shared dataset for the next model.
