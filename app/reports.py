"""Research report (CLAUDE.md §6): per language × model, with n everywhere.

Units:
- reply = a speaker's answer to a task that showed a machine candidate
  (stored replies incl. late ones and bare ratings; 'invalid' too-fast
  replies excluded). Grouped by the candidate the speaker actually saw.
- stars: replies that gave stars. Mean with a t-based 95% CI.
- >=4-star rate: share of rated replies with stars >= 4, Wilson 95% CI.
- accept-unchanged rate: replies with stars >= 4 and no correction / replies.
- chrF: sentence chrF (chars 1-6, beta 2, spaces removed, as sacrebleu) of the
  candidate against the speaker's version (their correction, or the
  candidate itself when accepted, which scores 100); mean over replies that
  give a version.
- agreement: items whose first two valid replies saw the same candidate:
  share where the two versions match after normalization.
- low_n: n_items < 5.
"""
from __future__ import annotations

import csv
import io
import math
from collections import Counter, defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Candidate, ReviewItem, ReviewTask, SourceText
from .sms.parser import normalize_for_compare

T975 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
        2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
        2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]
LOW_N = 5


def t_crit(df: int) -> float:
    if df <= 30:
        return T975[df - 1]
    return 2.000 if df <= 60 else 1.980 if df <= 120 else 1.960


def mean_ci(xs: list[float]) -> dict:
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": None, "ci95": None}
    m = sum(xs) / n
    if n == 1:
        return {"n": 1, "mean": m, "ci95": None}
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    h = t_crit(n - 1) * sd / math.sqrt(n)
    return {"n": n, "mean": m, "ci95": [m - h, m + h]}


def wilson(k: int, n: int, z: float = 1.96) -> dict:
    if n == 0:
        return {"n": 0, "k": 0, "rate": None, "ci95": None}
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return {"n": n, "k": k, "rate": p, "ci95": [max(0.0, c - h), min(1.0, c + h)]}


def chrf(hyp: str, ref: str, order: int = 6, beta: float = 2.0) -> float:
    h, r = hyp.replace(" ", ""), ref.replace(" ", "")
    p_sum = r_sum = 0.0
    eff = 0
    for k in range(1, order + 1):
        hc = Counter(h[i:i + k] for i in range(len(h) - k + 1))
        rc = Counter(r[i:i + k] for i in range(len(r) - k + 1))
        hn, rn = sum(hc.values()), sum(rc.values())
        if hn == 0 or rn == 0:
            continue
        m = sum((hc & rc).values())
        p_sum += m / hn
        r_sum += m / rn
        eff += 1
    if eff == 0:
        return 0.0
    p, rr = p_sum / eff, r_sum / eff
    if p + rr == 0:
        return 0.0
    b2 = beta * beta
    return 100 * (1 + b2) * p * rr / (b2 * p + rr)


def _version(t: ReviewTask) -> str | None:
    if t.corrected_text:
        return t.corrected_text
    if t.shown_candidate_text is not None and t.stars is not None and t.stars >= 4:
        return t.shown_candidate_text
    return None


def campaign_report(db: Session, cid: int) -> dict:
    items = db.scalars(select(ReviewItem).join(Candidate).join(SourceText)
                       .where(SourceText.campaign_id == cid)).all()
    groups: dict[tuple, dict] = defaultdict(lambda: {
        "items": set(), "replies": 0, "stars": [], "accept": 0, "chrf": [], "agree": []})
    scratch: Counter = Counter()
    for item in items:
        replies = sorted((t for t in item.tasks
                          if t.replied_at is not None and t.status != "invalid"),
                         key=lambda t: (t.replied_at, t.id))
        for t in replies:
            if t.shown_candidate_text is None:
                if t.corrected_text:
                    scratch[item.lang] += 1
                continue
            cand = t.shown_candidate or item.candidate
            g = groups[(item.lang, cand.origin, cand.model_ref)]
            g["items"].add(item.id)
            g["replies"] += 1
            if t.stars is not None:
                g["stars"].append(t.stars)
            if t.stars is not None and t.stars >= 4 and not t.corrected_text:
                g["accept"] += 1
            v = _version(t)
            if v is not None:
                g["chrf"].append(chrf(t.shown_candidate_text, v))
        valid = [t for t in replies if t.status in ("replied", "accepted")][:2]
        if (len(valid) == 2 and valid[0].shown_candidate_id == valid[1].shown_candidate_id
                and valid[0].shown_candidate_text is not None):
            a, b = _version(valid[0]), _version(valid[1])
            cand = valid[0].shown_candidate or item.candidate
            groups[(item.lang, cand.origin, cand.model_ref)]["agree"].append(
                a is not None and b is not None
                and normalize_for_compare(a) == normalize_for_compare(b))
    rows = []
    for (lang, origin, model), g in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
        n_items = len(g["items"])
        rows.append({
            "lang": lang, "origin": origin, "model_ref": model,
            "n_items": n_items, "n_replies": g["replies"], "low_n": n_items < LOW_N,
            "stars": mean_ci([float(s) for s in g["stars"]]),
            "ge4_rate": wilson(sum(s >= 4 for s in g["stars"]), len(g["stars"])),
            "accept_unchanged": wilson(g["accept"], g["replies"]),
            "chrf": mean_ci(g["chrf"]),
            "agreement": wilson(sum(g["agree"]), len(g["agree"])),
        })
    return {"campaign_id": cid, "low_n_threshold": LOW_N, "rows": rows,
            "translated_from_scratch": dict(sorted(scratch.items())),
            "definitions": __doc__.split("Units:", 1)[1].strip()}


REPORT_FIELDS = ["lang", "origin", "model_ref", "n_items", "n_replies", "low_n",
                 "stars_n", "stars_mean", "stars_ci_lo", "stars_ci_hi",
                 "ge4_n", "ge4_rate", "ge4_ci_lo", "ge4_ci_hi",
                 "accept_n", "accept_rate", "accept_ci_lo", "accept_ci_hi",
                 "chrf_n", "chrf_mean", "chrf_ci_lo", "chrf_ci_hi",
                 "agree_n", "agree_rate", "agree_ci_lo", "agree_ci_hi"]


def report_csv(rep: dict) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=REPORT_FIELDS)
    w.writeheader()
    for r in rep["rows"]:
        out = {k: r[k] for k in ("lang", "origin", "model_ref", "n_items", "n_replies", "low_n")}
        for key, pre, val in (("stars", "stars", "mean"), ("ge4_rate", "ge4", "rate"),
                              ("accept_unchanged", "accept", "rate"), ("chrf", "chrf", "mean"),
                              ("agreement", "agree", "rate")):
            m = r[key]
            out[f"{pre}_n"] = m["n"]
            out[f"{pre}_{val}"] = None if m[val] is None else round(m[val], 4)
            lo, hi = m["ci95"] or (None, None)
            out[f"{pre}_ci_lo"] = None if lo is None else round(lo, 4)
            out[f"{pre}_ci_hi"] = None if hi is None else round(hi, 4)
        w.writerow(out)
    return buf.getvalue()
