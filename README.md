# Lugha Verify

Send a message in English or Swahili; it reaches every recipient in their own
language, checked by paid native speakers over SMS first. Built on Africa's
Talking SMS and Airtime. Spec: `CLAUDE.md`. Log: `status.md`.

## Run locally

```bash
pip install -r requirements-dev.txt
cp .env.example .env   # fill in the values
uvicorn app.main:app --port 8000
```

AT callback URLs (sandbox dashboard → SMS → callbacks):
- Incoming messages: `https://<host>/at/sms/inbound?token=<AT_CALLBACK_TOKEN>`
- Delivery reports: `https://<host>/at/sms/delivery?token=<AT_CALLBACK_TOKEN>`

## Test

```bash
python -m pytest -q
python scripts/simulate.py          # offline end-to-end run
python scripts/simulate.py --live   # sends the SMS through the AT sandbox (.env)
```
