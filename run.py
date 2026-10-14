"""Run the whole pipeline: synthetic data -> agents -> findings.json, summary.md, email drafts."""
import json
from pathlib import Path

import engine
import gen_data
import notify

HERE = Path(__file__).parent
OUT = HERE / "out"


def usd(x):
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def main():
    gen_data.main()
    res = engine.run()
    OUT.mkdir(exist_ok=True)
    (OUT / "findings.json").write_text(json.dumps(
        {"summary": res["summary"], "forecast": res["forecast"], "findings": res["findings"]}, indent=2))
    emails = notify.write_all(res, OUT / "emails")

    s = res["summary"]
    lines = ["# Prototype run summary", "",
             f"Monthly run-rate (synthetic estate): {usd(s['monthly_run_rate'])}",
             f"Findings: {s['findings']}  |  risk points reducible: {s['risk_points_reducible']}", "",
             "| Tier | Findings | Monthly savings |", "| --- | ---: | ---: |"]
    for tier, key in (("SAFE_TO_ACT", "monthly_savings"), ("NEEDS_APPROVAL", "monthly_savings"),
                      ("DO_NOT_ACT", "blocked_savings")):
        fs = [f for f in res["findings"] if f["tier"] == tier]
        lines.append(f"| {tier} | {len(fs)} | {usd(sum(f[key] for f in fs))}"
                     f"{' (not counted; unsafe)' if tier == 'DO_NOT_ACT' else ''} |")
    lines += ["", f"Realisable savings: {usd(s['total_realisable_savings'])}/month, "
                  f"{usd(s['annualised_savings'])}/year", "",
              "## Forecast", "", "| Project | MTD | Forecast | Budget | Over/under | Chance over |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for p, v in sorted(res["forecast"].items(), key=lambda kv: -kv[1]["over_p50"]):
        lines.append(f"| {p} | {usd(v['mtd'])} | {usd(v['p50'])} | {usd(v['budget'])} | {usd(v['over_p50'])} | {v['prob_over']:.0%} |")
    lines += ["", "## Emails drafted (not sent)", ""] + [f"- {to}: {sub}" for to, sub in emails]
    (OUT / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
