# AT Marketplace submission (fill in as we go)

| Field | Value |
|---|---|
| Name | Lugha Verify (working name) |
| Slug | lugha-verify |
| Short description | Send one message; every recipient gets it in their own language, checked by paid native speakers over SMS first. |
| Long description | Upload a contact list and write your message in English or Swahili. English and Swahili recipients get it at once. For other languages (e.g. Kidaw'ida) a machine draft is checked by native speakers by SMS; the first two to reply are paid airtime, and only translations they verify are ever sent. Anything not verified in time goes out in English/Swahili instead. Every check is scored (stars, chrF, agreement), so research teams can also use it to evaluate translation models. |
| Logo | TODO |
| Image URL | `registry.africastalking.dev/zej2a1/lugha-verify:latest` (pushed by `.github/workflows/docker.yml` on every push to `main`) |
| Image size | 301 MB (2026-10-29; limit 1 GB, checked in CI) |
| Port | 8000 (or `$PORT` if the platform sets it) |
| Health check | `GET /health` → `{"ok": true}` |
| Database needs | PostgreSQL (tested on 16) via `DATABASE_URL` (`postgres://…` accepted). Schema is migrated automatically on start. Small: a few MB per thousand messages. **Confirm with AT what they provision.** |
| Instances | Exactly one container per customer; do not scale to several replicas or workers (the review scheduler runs in-process). |
| Categories | TODO (suggest: Communication, Localization) |
| Pricing plan | TODO |

## Environment variables

Required:

| Var | Notes |
|---|---|
| `AT_USERNAME` | `sandbox` for the sandbox |
| `AT_API_KEY` | customer's AT key (SMS + airtime come from their wallet) |
| `AT_SHORTCODE` or `AT_SENDER_ID` | two-way SMS needs a shortcode |
| `DATABASE_URL` | Postgres URL |
| `PHONE_ENC_KEY` | Fernet key: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` |
| `PHONE_HASH_SALT` | random string; never change after first use |
| `ADMIN_TOKEN` | customer's login for the portal and API |

Recommended: `AT_CALLBACK_TOKEN` (random string; add `?token=…` to the AT
callback URLs). On a shared shortcode set `AT_KEYWORD` (e.g. `LUGHA`): AT
forwards only messages that start with it, and every SMS tells people to type
it first. Optional: `TRANSLATE_API_URL`, `REVIEW_REQUIRED_LANGS`,
`REVIEW_MACHINE_SWAHILI`, `SHARED_DATA_URL`, `PAYOUT_PER_ITEM_KES` and the
timing variables in CLAUDE.md §10.

## AT callbacks to configure (customer's AT dashboard)

- SMS inbound: `https://<instance>/at/sms/inbound?token=<AT_CALLBACK_TOKEN>`
- SMS delivery reports: `https://<instance>/at/sms/delivery?token=<AT_CALLBACK_TOKEN>`
- Airtime status: `https://<instance>/at/airtime/status?token=<AT_CALLBACK_TOKEN>`

`<instance>` = the public URL the Marketplace gives each deployment (format:
ask AT).

## After each deploy

`BASE_URL=https://<instance> ADMIN_TOKEN=… AT_CALLBACK_TOKEN=… sh scripts/smoke.sh`
