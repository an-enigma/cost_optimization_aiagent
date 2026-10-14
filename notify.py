"""Email notifier: replaces the executor. Drafts one digest per resource owner plus a finance
overspend alert. Nothing is sent; drafts are written as .eml (open in any mail client) and .html."""
import html
from collections import defaultdict
from email.message import EmailMessage
from pathlib import Path

SENDER = "finops-agent@example.com"
FINANCE = "finance-ops@example.com"
TIER_STYLE = {
    "SAFE_TO_ACT": ("Safe to act", "#1b6e3c", "#e6f4ea"),
    "NEEDS_APPROVAL": ("Review and approve", "#8a5a00", "#fdf3e0"),
    "DO_NOT_ACT": ("Do not act", "#a32d2d", "#fcebeb"),
}
STEPS = {
    "DELETE": "Confirm nothing needs it, take a backup if it holds data, then delete it.",
    "DOWNSIZE": "Move to the next smaller size; watch CPU and latency for a week.",
    "RELEASE": "Release the unused address.",
    "REDUCE_RETENTION": "Lower retention to {target} days after compliance confirms.",
    "RESTRICT_PUBLIC_ACCESS": "Remove public access; grant only the users or services that need it.",
    "ROTATE_KEY": "Disable the old key, issue a scoped replacement, drop the Owner role.",
    "ENABLE_CMEK": "Turn on customer-managed encryption.",
}


def usd(x):
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def _step(f):
    return STEPS.get(f["action"], "").format(target=f.get("target_days", ""))


def _card(f):
    label, fg, bg = TIER_STYLE[f["tier"]]
    saving = (f"Save {usd(f['monthly_savings'])}/month" if f["monthly_savings"] else
              (f"Unsafe saving of {usd(f['blocked_savings'])}/month blocked" if f["blocked_savings"] else "Risk fix, no cost change"))
    risk = f" &middot; removes {f['risk_reduction']} risk points" if f["risk_reduction"] else ""
    notes = "".join(f"<li>{html.escape(x)}</li>" for x in f["why"] + f["reasons"])
    step = "" if f["tier"] == "DO_NOT_ACT" else f"<p style='margin:6px 0 0'><b>Suggested step:</b> {html.escape(_step(f))}</p>"
    return (f"<div style='border:1px solid #ddd;border-radius:8px;padding:12px 14px;margin:10px 0'>"
            f"<span style='background:{bg};color:{fg};padding:2px 8px;border-radius:10px;font-size:12px'>{label}</span> "
            f"<span style='color:#666;font-size:12px'>{f['id']} &middot; {html.escape(f['resource'])} &middot; {f['env']}</span>"
            f"<div style='font-size:15px;margin:6px 0 2px'><b>{html.escape(f['title'])}</b></div>"
            f"<div style='color:#333'>{saving}{risk}</div><ul style='margin:6px 0 0 18px;color:#444'>{notes}</ul>{step}</div>")


def owner_email(owner, findings):
    mine = [f for f in findings if f["owner"] == owner]
    save = sum(f["monthly_savings"] for f in mine)
    blocked = sum(f["blocked_savings"] for f in mine)
    n_act = sum(1 for f in mine if f["tier"] != "DO_NOT_ACT")
    subject = f"[FinOps] {n_act} action{'s' if n_act != 1 else ''}, {usd(save)}/month to save on your resources"
    head = (f"<p>You own {len(mine)} flagged resources. Realisable savings: <b>{usd(save)}/month</b> "
            f"({usd(save * 12)}/year)." + (f" We also ruled out $420/month of apparent savings because acting on them would be unsafe. These are not in your total and need no action." if blocked else "")
            + "</p><p style='color:#666'>This is a recommendation only. Nothing has been changed. "
              "Reply or use your normal change process to act.</p>")
    body = "".join(_card(f) for f in mine)
    html_doc = f"<html><body style='font-family:Arial,sans-serif;max-width:640px'><h2>Cloud cost and risk digest</h2>{head}{body}</body></html>"
    text = subject + "\n\n" + "\n".join(f"[{f['tier']}] {f['id']} {f['resource']}: {f['title']}" for f in mine)
    return subject, text, html_doc


def finance_email(forecast, findings):
    rows, over = "", []
    for p, v in sorted(forecast.items(), key=lambda kv: -kv[1]["over_p50"]):
        avail = sum(f["monthly_savings"] for f in findings if f["project"] == p and f["tier"] != "DO_NOT_ACT")
        flag = v["over_p50"] > 0
        if flag:
            over.append((p, v))
        color = "#a32d2d" if flag else "#1b6e3c"
        rows += (f"<tr><td>{p}</td><td align=right>{usd(v['mtd'])}</td><td align=right>{usd(v['p50'])}</td>"
                 f"<td align=right>{usd(v['p10'])} to {usd(v['p90'])}</td><td align=right>{usd(v['budget'])}</td>"
                 f"<td align=right style='color:{color}'>{'+' if flag else ''}{usd(v['over_p50'])}</td>"
                 f"<td align=right>{v['prob_over']:.0%}</td><td align=right>{usd(avail)}</td></tr>")
    first = next(iter(forecast.values()))
    subject = (f"[FinOps] {len(over)} project(s) forecast over budget for {first['month_end'][:7]}" if over
               else f"[FinOps] All projects forecast within budget for {first['month_end'][:7]}")
    th = "style='text-align:right;border-bottom:1px solid #ccc;padding:4px 8px'"
    html_doc = (f"<html><body style='font-family:Arial,sans-serif;max-width:760px'><h2>Month-end spend forecast</h2>"
                f"<p>As of {first['as_of']}, month ends {first['month_end']}. Range is the 10th to 90th percentile "
                f"of daily noise around the fitted trend.</p>"
                f"<table cellpadding=4 style='border-collapse:collapse;font-size:13px'><tr><th align=left>Project</th>"
                f"<th {th}>Month to date</th><th {th}>Forecast</th><th {th}>Range</th><th {th}>Budget</th>"
                f"<th {th}>Over/under</th><th {th}>Chance over</th><th {th}>Savings available/mo</th></tr>{rows}</table>"
                f"<p style='color:#666'>Recommendation only; owners received their own action lists.</p></body></html>")
    text = subject + "\n" + "\n".join(f"{p}: forecast {usd(v['p50'])} vs budget {usd(v['budget'])}" for p, v in forecast.items())
    return subject, text, html_doc


def _write(out_dir, name, to, subject, text, html_doc):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SENDER, to, subject
    msg["X-Draft"] = "not-sent"
    msg.set_content(text)
    msg.add_alternative(html_doc, subtype="html")
    (out_dir / f"{name}.eml").write_bytes(bytes(msg))
    (out_dir / f"{name}.html").write_text(html_doc)


def write_all(result, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    findings = result["findings"]
    written = []
    for owner in sorted({f["owner"] for f in findings}):
        subject, text, html_doc = owner_email(owner, findings)
        name = owner.split("@")[0]
        _write(out_dir, name, owner, subject, text, html_doc)
        written.append((owner, subject))
    subject, text, html_doc = finance_email(result["forecast"], findings)
    _write(out_dir, "finance-forecast", FINANCE, subject, text, html_doc)
    written.append((FINANCE, subject))
    return written
