// Plays the CLAUDE.md §14 demo against demo_server.py; saves screenshots and a video.
//   node record.js <out_dir>   (server on http://127.0.0.1:$DEMO_PORT, default 8765)
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const B = `http://127.0.0.1:${process.env.DEMO_PORT || 8765}`;
const OUT = process.argv[2] || 'demo_out';
const D = JSON.parse(fs.readFileSync(path.join(__dirname, 'demo.json'), 'utf8'));
const ADMIN = { Authorization: 'Bearer demo' };
fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 760 },
    recordVideo: { dir: OUT, size: { width: 1280, height: 760 } } });
  const page = await ctx.newPage();
  const api = ctx.request;
  let n = 0;
  const pause = ms => page.waitForTimeout(ms);
  async function caption(text) {
    await page.evaluate(t => {
      let el = document.getElementById('demo-cap');
      if (!el) { el = document.createElement('div'); el.id = 'demo-cap'; document.body.appendChild(el); }
      el.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:99;background:#111d;color:#fff;' +
        'font:600 18px system-ui,sans-serif;padding:12px 18px;text-align:center';
      el.textContent = t;
    }, text);
  }
  async function shot(name, text, ms = 2500) {
    await caption(text);
    await pause(ms);
    await page.screenshot({ path: path.join(OUT, `${String(++n).padStart(2, '0')}-${name}.png`), fullPage: false });
  }
  const sms = async (phone, text) => {
    await api.post(`${B}/__demo/log_reply?phone=${encodeURIComponent(phone)}&text=${encodeURIComponent(text)}`);
    return api.post(`${B}/at/sms/inbound`, { form: { from: phone, text, id: `${phone}-${Date.now()}-${Math.random()}` } });
  };
  const advance = m => api.post(`${B}/__demo/advance?minutes=${m}`);
  const phones = async (only) => { await page.goto(`${B}/__demo/phones${only ? '?only=' + only : ''}`); };

  // 0. login
  await page.goto(`${B}/portal/login`);
  await shot('login', 'Lugha Verify: a County Health Office logs in to its own instance', 1800);
  await page.fill('input[name=token]', 'demo');
  await page.click('button');

  // 1. recipients
  await page.goto(`${B}/portal/lists`);
  await page.fill('input[name=list_id]', 'health-notice');
  await page.fill('textarea[name=csv_text]', 'phone,preferred_lang\n' +
    D.recipients.map(r => `${r.phone},${r.lang}`).join('\n'));
  await shot('upload-list', 'Upload the contact list: each person with their preferred language');
  await page.click('form[action="/portal/lists"] button');
  await shot('list-uploaded', '4 recipients: English, Kiswahili and Kidaw\'ida');

  // 2. speakers (registered via API; they reply YES by SMS)
  for (const s of D.speakers) {
    await api.post(`${B}/api/speakers`, { headers: ADMIN,
      data: { phone: s.phone, languages: s.languages, trusted_for: s.trusted_for || [] } });
    await sms(s.phone, 'YES');
  }
  await page.goto(`${B}/portal/speakers`);
  await shot('speakers', 'Native Kidaw\'ida speakers opted in by SMS (reply YES); one is a trusted reviewer');

  // 3. campaign
  await page.goto(`${B}/portal/campaigns/new`);
  await page.fill('textarea[name=message]', D.message_eng);
  await page.fill('textarea[name=swahili]', D.message_swh);
  await shot('new-campaign', 'Write the notice once in English (and Kiswahili)');
  await page.click('form >> nth=1 >> button');
  await pause(500);
  const cid = page.url().split('/').pop();
  await page.click('text=Start review');
  await shot('started', 'Start: English and Kiswahili go out at once; Kidaw\'ida waits for a machine draft');

  // 4. machine draft -> trusted reviewer
  await api.post(`${B}/__demo/jobs`);
  await phones('recipient');
  await shot('recipients-at-once', 'English and Kiswahili recipients already have the notice; Kidaw\'ida recipients wait', 3500);
  await phones('speaker');
  await shot('trusted-task', 'The machine draft goes to the trusted reviewer first (15 minutes to answer)', 3500);

  // 5. trusted reviewer silent -> fan-out
  await advance(16);
  await phones('speaker');
  await shot('fan-out', 'No answer in 15 minutes: the task fans out to the other speakers', 3500);

  // 6. two reviewers answer with the same correction
  const code = async (phone) => {
    const html = await (await api.get(`${B}/__demo/phones?only=speaker`)).text();
    const name = D.speakers.find(s => s.phone === phone).name;
    const card = html.split('<div class="phone">').find(c => c.includes(`<b>${name}</b>`));
    return [...card.matchAll(/([A-Z2-9]{4}) Rate 1-5/g)].pop()[1];
  };
  const [, r1, r2] = D.speakers;
  await advance(3);
  await sms(r1.phone, `${await code(r1.phone)} 3 ${D.dav_speaker_version} SHARE`);
  await advance(2);
  await sms(r2.phone, `${await code(r2.phone)} 3 ${D.dav_speaker_version}`);
  await advance(0.5);
  await phones('speaker');
  await shot('replies', 'Two speakers send the same correction: verified. Each is paid KSh 50 airtime; the others are told it is filled', 4500);

  // 7. delivery
  await phones('recipient');
  await shot('delivered', 'Only the speaker-verified Kidaw\'ida is sent to Kidaw\'ida recipients', 3500);

  // 8. campaign page + report
  await page.goto(`${B}/portal/campaigns/${cid}`);
  await shot('campaign', 'Campaign: delivered, airtime spent, and the machine draft scored by the speakers', 4000);
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await shot('report', 'Every check becomes evaluation data (one speaker typed SHARE for research use)', 4000);

  await ctx.close();
  await browser.close();
  console.log(`done: ${n} screenshots in ${OUT}`);
})().catch(e => { console.error(e); process.exit(1); });
