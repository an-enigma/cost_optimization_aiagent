"""Human review screen. A person approves or rejects each email draft; nothing is released without it.

    python3 review_app.py            then open Cloud Shell > Web Preview > Preview on port 8080

Standard library only. Every decision is appended to out/audit_log.jsonl with the reviewer's name and
a fingerprint of the exact draft that was approved. Approving copies the draft to out/outbox/ (it is not
emailed; sending is a separate, optional step).
"""
import argparse
import datetime as dt
import html
import json
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pipeline

OUT = pipeline.OUT
AUDIT = OUT / "audit_log.jsonl"
OUTBOX = OUT / "outbox"
E = html.escape
TIER = {"SAFE_TO_ACT": ("Safe to act", "t-safe"), "NEEDS_APPROVAL": ("Review and approve", "t-appr"),
        "DO_NOT_ACT": ("Do not act", "t-no")}


def usd(x):
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def read_audit():
    if not AUDIT.exists():
        return []
    return [json.loads(line) for line in AUDIT.read_text().splitlines() if line.strip()]


def latest_decisions(run_id):
    d = {}
    for rec in read_audit():
        if rec["run_id"] == run_id:
            d[rec["owner"]] = rec
    return d


def record_decision(S, owner, decision, reviewer, note):
    """Validate and log a human decision. Returns an error string or None."""
    entry = next((q for q in S.get("queue", []) if q["owner"] == owner), None)
    if entry is None:
        return "Unknown draft"
    if decision not in ("approve", "reject"):
        return "Decision must be approve or reject"
    if not reviewer.strip():
        return "Reviewer name is required"
    rec = dict(ts=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), run_id=S["run_id"], owner=owner,
               decision=decision, reviewer=reviewer.strip(), note=note.strip(), draft_sha256=entry["draft_sha256"])
    OUT.mkdir(exist_ok=True)
    with AUDIT.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    if decision == "approve":
        OUTBOX.mkdir(exist_ok=True)
        shutil.copy(pipeline.EMAILS / f"{entry['stem']}.eml", OUTBOX / f"{S['run_id']}-{entry['stem']}.eml")
    return None


CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#202124;--mute:#5f6368;--line:#d9dee5;--blue:#1a56b8}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--card:#1d2026;--ink:#eceff3;--mute:#a4abb5;--line:#343a44;--blue:#8ab4f8}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,Arial,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:28px;margin:0 0 4px}h2{font-size:20px;margin:36px 0 12px}
.sub{color:var(--mute);margin:0 0 20px}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.kpi,.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.kpi b{display:block;font-size:26px}.kpi span{color:var(--mute);font-size:14px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;font-size:14px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mute);font-weight:600}
.r{text-align:right}.chip{display:inline-block;padding:1px 9px;border-radius:10px;font-size:12px;font-weight:600}
.t-safe{background:#e6f4ea;color:#1b6b38}.t-appr{background:#fdf3e0;color:#8a5a00}.t-no{background:#fcebeb;color:#a32d2d}
.ai{background:#e8f0fe;color:#1a56b8}.pend{background:#eee;color:#444}.ok{background:#e6f4ea;color:#1b6b38}.bad{background:#fcebeb;color:#a32d2d}
.queue{display:grid;gap:16px}iframe{width:100%;height:340px;border:1px solid var(--line);border-radius:8px;background:#fff}
form{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}input[type=text]{padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);flex:1;min-width:140px}
button{padding:8px 16px;border-radius:8px;border:1px solid var(--line);font-weight:600;cursor:pointer}
.approve{background:#1b6b38;color:#fff;border-color:#1b6b38}.reject{background:var(--card);color:#a32d2d;border-color:#a32d2d}
.note{color:var(--mute);font-size:13px}
"""


def page(S):
    if "queue" not in S:
        return ("<main><h1>No run yet</h1><p>Run <code>python3 pipeline.py</code> first, then reload this page.</p></main>")
    s, g, t = S["summary"], S["grounding"], S["summary"]["time"]
    dec = latest_decisions(S["run_id"])
    kp = [(usd(s["total_realisable_savings"]) + "/mo", "Realisable savings (synthetic estate)"),
          (usd(s["blocked_unsafe_savings"]) + "/mo", f"Blocked as unsafe ({s['blocked_count']} findings)"),
          (f"{g['passed_all_checks']}/{g['total']}", "AI notes that passed every check"),
          (f"{t['hours_saved_per_review_cycle']} h", f"Review time saved per cycle (assumption: {t['manual_minutes_per_finding']:.0f} vs {t['review_minutes_per_finding']:.0f} min per item)")]
    out = [f"<main><h1>Cloud cost and security review</h1><p class='sub'>Run {E(S['run_id'])}. Synthetic demo estate. "
           "Drafts below are recommendations only; nothing is sent and nothing in the cloud is changed.</p>",
           "<div class='kpis'>" + "".join(f"<div class='kpi'><b>{E(a)}</b><span>{E(b)}</span></div>" for a, b in kp) + "</div>"]

    out.append("<h2>Forecast against budget</h2><table><tr><th>Project</th><th class=r>Forecast</th><th class=r>Budget</th>"
               "<th class=r>Over / under</th><th class=r>Chance over</th></tr>")
    for p, v in sorted(S["forecast"].items(), key=lambda kv: -kv[1]["over_p50"]):
        out.append(f"<tr><td>{E(p)}</td><td class=r>{usd(v['p50'])}</td><td class=r>{usd(v['budget'])}</td>"
                   f"<td class=r>{'+' if v['over_p50'] > 0 else ''}{usd(v['over_p50'])}</td><td class=r>{v['prob_over']:.0%}</td></tr>")
    out.append("</table>")

    out.append("<h2>Email drafts waiting for a human decision</h2><div class='queue'>")
    for q in S["queue"]:
        d = dec.get(q["owner"])
        badge = ("<span class='chip pend'>Waiting</span>" if not d else
                 f"<span class='chip {'ok' if d['decision'] == 'approve' else 'bad'}'>"
                 f"{'Approved' if d['decision'] == 'approve' else 'Rejected'} by {E(d['reviewer'])}</span>")
        draft = (pipeline.EMAILS / f"{q['stem']}.html").read_text()
        out.append(f"<div class='card'><b>{E(q['owner'])}</b> {badge}<div class='note'>{E(q['subject'])}</div>"
                   f"<iframe sandbox srcdoc=\"{E(draft, quote=True)}\" title='Draft for {E(q['owner'])}'></iframe>"
                   f"<form method='post' action='/decide'><input type='hidden' name='owner' value='{E(q['owner'], quote=True)}'>"
                   "<input type='text' name='reviewer' placeholder='Your name (required)' required>"
                   "<input type='text' name='note' placeholder='Comment (optional)'>"
                   "<button class='approve' name='decision' value='approve'>Approve and release to outbox</button>"
                   "<button class='reject' name='decision' value='reject'>Reject</button></form></div>")
    out.append("</div>")

    out.append("<h2>All findings</h2><table><tr><th>ID</th><th>Resource</th><th>Tier</th><th class=r>Saves/mo</th>"
               "<th class=r>Blocked/mo</th><th>Why and AI note</th></tr>")
    for f in S["findings"]:
        label, cls = TIER[f["tier"]]
        n = S["notes"][f["id"]]
        src = "AI-written, checked" if n["source"] == "gemini" else "Template"
        ai = f.get("ai_review", {})
        aib = " <span class='chip ai'>AI review added caution</span>" if ai.get("applied") in ("raised_to_approval", "blocked", "caution_added") else ""
        out.append(f"<tr><td>{E(f['id'])}</td><td>{E(f['resource'])}<div class='note'>{E(f['project'])} / {E(f['env'])}</div></td>"
                   f"<td><span class='chip {cls}'>{label}</span>{aib}</td><td class=r>{usd(f['monthly_savings'])}</td>"
                   f"<td class=r>{usd(f['blocked_savings'])}</td><td>{E(n['note'])}<div class='note'>{src}</div></td></tr>")
    out.append("</table>")

    out.append("<h2>Audit log</h2>")
    log = read_audit()[-15:][::-1]
    if not log:
        out.append("<p class='note'>No decisions yet.</p>")
    else:
        out.append("<table><tr><th>Time (UTC)</th><th>Reviewer</th><th>Draft</th><th>Decision</th><th>Comment</th><th>Draft fingerprint</th></tr>")
        for r in log:
            out.append(f"<tr><td>{E(r['ts'])}</td><td>{E(r['reviewer'])}</td><td>{E(r['owner'])}</td><td>{E(r['decision'])}</td>"
                       f"<td>{E(r['note'])}</td><td><code>{E(r['draft_sha256'][:12])}</code></td></tr>")
        out.append("</table>")
    out.append("</main>")
    return "".join(out)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] != "/":
            return self._send(404, "Not found", "text/plain")
        body = f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Cost and security review</title><style>{CSS}</style>{page(pipeline.load_state())}"
        self._send(200, body)

    def do_POST(self):
        if self.path != "/decide":
            return self._send(404, "Not found", "text/plain")
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        get = lambda k: form.get(k, [""])[0]
        err = record_decision(pipeline.load_state(), get("owner"), get("decision"), get("reviewer"), get("note"))
        if err:
            return self._send(400, E(err), "text/plain")
        self._send(303, "", "text/plain", {"Location": "/"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    a = ap.parse_args()
    print(f"Review screen on port {a.port}. In Cloud Shell use Web Preview > Preview on port {a.port}.")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
