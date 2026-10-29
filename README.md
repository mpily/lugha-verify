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

## Demo recording (screenshots + video)

```bash
sh scripts/demo/record.sh demo_out
```

Plays the demo story (CLAUDE.md §14) through the real app with simulated
phones and a fast-forward clock: portal login → contact list → speakers →
campaign → English/Swahili sent at once → trusted reviewer silent → fan-out →
two matching corrections → verified Kidaw'ida delivered → report. Writes 13
captioned PNGs plus `demo.webm`, `demo.mp4` and `demo.gif` (mp4/gif need
ffmpeg). Needs Node with the `playwright` package and Chromium.
Nothing is sent to Africa's Talking.

The texts and people are in `scripts/demo/demo.json`. **The Kidaw'ida lines
are placeholders**: put in a native speaker's translation before showing the
video publicly.

## Testing on the AT sandbox (real SMS)

1. Sandbox app at account.africastalking.com → API key; `AT_USERNAME=sandbox`.
2. Sandbox → SMS → Shortcodes: create one; put it in `AT_SHORTCODE`.
3. Expose the app (e.g. `cloudflared tunnel --url http://localhost:8000`) and set
   the callbacks (with `?token=<AT_CALLBACK_TOKEN>`): SMS inbound
   `/at/sms/inbound`, delivery reports `/at/sms/delivery`, airtime
   `/at/airtime/status`.
4. Open simulator.africastalking.com, add a few test phone numbers, register
   them as speakers/recipients in the portal, and reply from the simulator
   phones exactly as a speaker would.
