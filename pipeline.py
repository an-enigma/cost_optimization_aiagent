"""Pipeline stages. Each stage is a plain function; the ADK agents call them as tools.

Run everything without ADK:   python3 pipeline.py            (uses Gemini)
                              python3 pipeline.py --offline  (no Gemini, template notes)
State passes between stages through out/state.json, so any stage can be re-run on its own.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path

import engine
import gemini_steps
import notify

HERE = Path(__file__).parent
DATA = HERE / "data"
OUT = HERE / "out"
STATE = OUT / "state.json"
EMAILS = OUT / "emails_ai"

# Assumptions for the time-saved figure. Replace with real Novartis numbers.
MANUAL_MIN = float(os.environ.get("MANUAL_MIN_PER_FINDING", "30"))   # manual cost + security review of one item
REVIEW_MIN = float(os.environ.get("REVIEW_MIN_PER_FINDING", "5"))    # time to read and decide on one agent note


def offline():
    return os.environ.get("FINOPS_OFFLINE", "") == "1"


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(S):
    OUT.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(S, indent=2))


def _resources():
    return json.loads((DATA / "resources.json").read_text())


def _need(S, key, stage):
    if key not in S:
        raise RuntimeError(f"Run {stage} first")


def _sort(findings):
    findings.sort(key=lambda x: (engine.TIER_ORDER[x["tier"]], -x["priority"], -x["blocked_savings"]))


def _tier_counts(findings):
    return {t: sum(1 for f in findings if f["tier"] == t) for t in engine.TIER_ORDER}


# ---- Stage 1: Scanner ---------------------------------------------------------------------------
def scan_estate() -> dict:
    """Scan the cloud estate for idle or oversized resources, wasteful settings and security risks."""
    if not (DATA / "resources.json").exists():
        import gen_data
        gen_data.main()
    resources = _resources()
    findings = engine.scan(resources)
    S = {"run_id": dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"), "findings": findings}
    save_state(S)
    by_code = {}
    for f in findings:
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1
    return {"resources_scanned": len(resources), "findings_proposed": len(findings), "by_type": by_code}


# ---- Stage 2: Dependency, guardrails, scoring ---------------------------------------------------
def apply_safety_checks() -> dict:
    """Check what depends on each proposed fix, apply legal, disaster-recovery and compliance guardrails, then score and tier every finding."""
    S = load_state()
    _need(S, "findings", "scan_estate")
    resources = _resources()
    by_id = {r["id"]: r for r in resources}
    engine.check_dependencies(S["findings"], resources)
    engine.apply_guardrails(S["findings"], by_id)
    engine.score_and_tier(S["findings"])
    save_state(S)
    return {"tiers": _tier_counts(S["findings"])}


# ---- Stage 3: Gemini case review ------------------------------------------------------------------
def review_cases_with_gemini() -> dict:
    """Ask Gemini to read the owner's notes on each resource and flag reasons a cut could be unsafe. Gemini can only add caution, never remove it."""
    S = load_state()
    _need(S, "findings", "scan_estate")
    annotations = json.loads((DATA / "annotations.json").read_text())
    annotations = {k: v for k, v in annotations.items() if not k.startswith("_")}
    S["ai_reviews"] = gemini_steps.review_cases(S["findings"], annotations, offline=offline())
    _sort(S["findings"])
    for f in S["findings"]:
        if f["id"] in S["ai_reviews"]:
            f["ai_review"] = S["ai_reviews"][f["id"]]
    save_state(S)
    applied = {}
    for r in S["ai_reviews"].values():
        applied[r["applied"]] = applied.get(r["applied"], 0) + 1
    return {"findings_reviewed": len(S["ai_reviews"]), "outcomes": applied, "tiers": _tier_counts(S["findings"])}


# ---- Stage 4: Forecast --------------------------------------------------------------------------------
def forecast_budgets() -> dict:
    """Forecast month-end spend for each project and compare it with budget."""
    S = load_state()
    budgets = json.loads((DATA / "budgets.json").read_text())
    S["forecast"] = engine.forecast(DATA / "billing.csv", budgets)
    save_state(S)
    return {p: {"forecast": round(v["p50"]), "budget": round(v["budget"]), "over_budget_by": round(v["over_p50"])}
            for p, v in S["forecast"].items()}


# ---- Stage 5: Notes (Gemini writes, code checks) ------------------------------------------------------
def write_checked_notes() -> dict:
    """Have Gemini write a plain-language note per finding, then check each note against the facts. Notes that fail a check are replaced by template text."""
    S = load_state()
    _need(S, "findings", "scan_estate")
    S["notes"], S["grounding"] = gemini_steps.explain_all(S["findings"], offline=offline())
    save_state(S)
    return S["grounding"]


# ---- Stage 6: Queue drafts for human review -----------------------------------------------------------
def _summary(S, resources):
    f = S["findings"]

    def tot(tier, key="monthly_savings"):
        return round(sum(x[key] for x in f if x["tier"] == tier), 2)

    s = dict(findings=len(f), safe_savings=tot("SAFE_TO_ACT"), approval_savings=tot("NEEDS_APPROVAL"),
             blocked_unsafe_savings=tot("DO_NOT_ACT", "blocked_savings"),
             blocked_count=sum(1 for x in f if x["tier"] == "DO_NOT_ACT"),
             risk_points_reducible=sum(x["risk_reduction"] for x in f),
             monthly_run_rate=round(sum(r["monthly_cost"] for r in resources), 2), tiers=_tier_counts(f))
    s["total_realisable_savings"] = round(s["safe_savings"] + s["approval_savings"], 2)
    s["annualised_savings"] = round(s["total_realisable_savings"] * 12, 2)
    manual = len(f) * MANUAL_MIN / 60
    assisted = len(f) * REVIEW_MIN / 60
    s["time"] = dict(assumption=True, manual_minutes_per_finding=MANUAL_MIN, review_minutes_per_finding=REVIEW_MIN,
                     manual_hours=round(manual, 1), assisted_hours=round(assisted, 1),
                     hours_saved_per_review_cycle=round(manual - assisted, 1))
    return s


def queue_for_human_review() -> dict:
    """Write one email draft per owner plus a finance forecast alert and put them in the human review queue. Nothing is sent."""
    S = load_state()
    for key, stage in (("findings", "scan_estate"), ("forecast", "forecast_budgets"), ("notes", "write_checked_notes")):
        _need(S, key, stage)
    resources = _resources()
    S["summary"] = _summary(S, resources)

    original = notify._card

    def card(f):
        html_card = original(f)
        n = S["notes"][f["id"]]
        tag = "AI-written, checked" if n["source"] == "gemini" else "Template text"
        extra = (f"<p style='margin:8px 0 0'>{notify.html.escape(n['note'])} "
                 f"<span style='color:#666;font-size:11px'>({tag})</span></p>")
        rv = f.get("ai_review", {})
        if rv.get("applied") in ("raised_to_approval", "blocked", "caution_added"):
            extra += "<p style='margin:4px 0 0;color:#8a5a00;font-size:12px'>AI review added caution to this item.</p>"
        return html_card[:-len("</div>")] + extra + "</div>"

    if EMAILS.exists():
        shutil.rmtree(EMAILS)
    notify._card = card
    try:
        written = notify.write_all({"findings": S["findings"], "forecast": S["forecast"]}, EMAILS)
    finally:
        notify._card = original

    queue = []
    for to, subject in written:
        stem = "finance-forecast" if to == notify.FINANCE else to.split("@")[0]
        mine = [f for f in S["findings"] if f["owner"] == to]
        queue.append(dict(
            owner=to, subject=subject, stem=stem, findings=len(mine),
            savings=round(sum(f["monthly_savings"] for f in mine), 2),
            draft_sha256=hashlib.sha256((EMAILS / f"{stem}.eml").read_bytes()).hexdigest()))
    S["queue"] = queue
    save_state(S)
    return {"drafts_waiting_for_human_approval": len(queue), "review_screen": "python3 review_app.py"}


STAGES = [scan_estate, apply_safety_checks, review_cases_with_gemini, forecast_budgets,
          write_checked_notes, queue_for_human_review]


def run_all():
    for stage in STAGES:
        print(f"[{stage.__name__}]", json.dumps(stage(), default=str))
    S = load_state()
    s = S["summary"]
    print(f"\nRealisable savings ${s['total_realisable_savings']:,.0f}/month; blocked as unsafe "
          f"${s['blocked_unsafe_savings']:,.0f}/month; tiers {s['tiers']}")
    g = S["grounding"]
    print(f"Notes passing every check: {g['passed_all_checks']}/{g['total']}  fallbacks: {g['fallback_reasons']}")
    print("Next: python3 review_app.py  (then Web Preview on port 8080)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip Gemini")
    if ap.parse_args().offline:
        os.environ["FINOPS_OFFLINE"] = "1"
    run_all()
