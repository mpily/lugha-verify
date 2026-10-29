"""Demo server: the real app with fake AT/Space and a controllable clock,
plus /__demo routes the recorder uses (advance time, run jobs, show phones).
Never use outside demos: nothing here talks to Africa's Talking."""
from __future__ import annotations

import html
import json
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.payouts import FakeAirtimeClient  # noqa: E402
from app.phones import normalize_phone  # noqa: E402
from app.sms.at_client import FakeSmsClient  # noqa: E402
from app.translators.base import FakeTranslator  # noqa: E402

DEMO = json.load(open(os.path.join(os.path.dirname(__file__), "demo.json"), encoding="utf-8"))


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 29, 9, 0)

    def __call__(self):
        return self.now


LOG: list[tuple] = []  # (sim time, seq, phone, "in"|"out", text), one timeline


class TimedSms(FakeSmsClient):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock

    def send(self, phone, text):
        res = super().send(phone, text)
        LOG.append((self.clock(), len(LOG), phone, "in", text))
        return res


clock = Clock()
sms = TimedSms(clock)
settings = Settings(_env_file=None, at_username="sandbox", at_api_key="demo", at_shortcode="12345",
                    phone_enc_key=Fernet.generate_key().decode(), phone_hash_salt="demo",
                    admin_token="demo", database_url="sqlite://")
app = create_app(settings, sms_client=sms, clock=clock, start_scheduler=False,
                 translator=FakeTranslator({(DEMO["message_swh"], "dav"): DEMO["dav_machine_draft"]}),
                 airtime_client=FakeAirtimeClient(), shared_sink=type("S", (), {"write": lambda s, r: None})())


@app.post("/__demo/log_reply")
def log_reply(phone: str, text: str):
    """The recorder calls this just before posting a simulated reply to
    /at/sms/inbound, so replies sit on the same timeline as outgoing SMS."""
    LOG.append((clock(), len(LOG), normalize_phone(phone), "out", text))
    return {"ok": True}


@app.post("/__demo/advance")
def advance(minutes: float = 0):
    clock.now += timedelta(minutes=minutes)
    app.state.tick()
    return {"now": clock.now.isoformat()}


@app.post("/__demo/jobs")
def jobs():
    return {"drafts": app.state.draft(), "retranslations": app.state.retranslate()}


@app.get("/__demo/phones", response_class=HTMLResponse)
def phones(only: str = ""):
    people = [dict(p, kind="recipient") for p in DEMO["recipients"]] + \
             [dict(p, kind="speaker") for p in DEMO["speakers"]]
    if only:
        people = [p for p in people if p["kind"] == only]
    cards = []
    for p in people:
        msgs = sorted((t, i, d, x) for t, i, ph, d, x in LOG if ph == p["phone"])
        label = p.get("role") or {"eng": "English", "swh": "Kiswahili", "dav": "Kidaw'ida"}[p["lang"]]
        bubbles = "".join(
            f'<div class="b {d}"><div>{html.escape(x)}</div><small>{t:%H:%M}</small></div>'
            for t, _, d, x in msgs) or '<p class="none">no messages</p>'
        cards.append(f'<div class="phone"><div class="top"><b>{html.escape(p["name"])}</b>'
                     f'<span>{html.escape(label)}</span></div><div class="chat">{bubbles}</div></div>')
    return f"""<!doctype html><meta charset=utf-8><title>Phones</title><style>
body{{font:14px system-ui,sans-serif;background:#e9edf1;margin:0;padding:16px}}
h1{{font-size:18px;margin:0 0 10px}}.grid{{display:flex;gap:12px;flex-wrap:nowrap}}
.phone{{width:262px;height:600px;background:#fff;border:10px solid #222;border-radius:28px;display:flex;flex-direction:column;overflow:hidden}}
.top{{background:#075e54;color:#fff;padding:10px 12px;display:flex;justify-content:space-between;align-items:baseline}}
.top span{{font-size:12px;opacity:.85}}.chat{{flex:1;overflow:auto;padding:8px;background:#ece5dd}}
.b{{max-width:85%;padding:6px 8px;border-radius:8px;margin:6px 0;white-space:pre-wrap;word-wrap:break-word;font-size:13px}}
.b small{{display:block;text-align:right;color:#777;font-size:10px}}
.in{{background:#fff}}.out{{background:#dcf8c6;margin-left:auto}}.none{{color:#888;text-align:center}}
</style><h1>Phones (simulated) - {clock.now:%H:%M}</h1><div class=grid>{''.join(cards)}</div>
<script>document.querySelectorAll('.chat').forEach(c=>c.scrollTop=c.scrollHeight)</script>"""


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DEMO_PORT", "8765")), log_level="warning")
