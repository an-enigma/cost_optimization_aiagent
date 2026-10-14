"""The agent team: Scanner, Dependency, Scoring, Guardrail, Forecast, Orchestrator.

Pure Python, no cloud calls. In the hackathon build each function becomes an ADK agent (or a tool
an ADK agent calls) and Gemini writes the explanations; the decision logic stays deterministic so
every recommendation can be audited.
"""
import csv
import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).parent / "data"

# ---- Tunable policy (state these as assumptions in the pitch) -----------------------------
IDLE_CPU = 5.0              # % average CPU below which compute / database is "idle"
RIGHTSIZE_CPU = 30.0        # % average CPU below which a non-idle resource is oversized
DOWNSIZE_RATE = {"compute": 0.40, "cloudsql": 0.40, "gke": 0.35}  # share of cost saved
POLICY_DEFAULT_LOG_DAYS = 90  # naive cost rule: keep logs this long
RISK_USD_PER_POINT = 40     # monthly dollars one risk point is "worth" when ranking
SAFE_SAVINGS_CAP = 500      # above this a change always needs approval
FORECAST_WINDOW = 28        # days of history used to fit the trend

TYPE_LABEL = {
    "compute": "VM", "cloudsql": "Cloud SQL instance", "gke": "GKE node pool", "disk": "Disk",
    "snapshot": "Snapshot", "address": "Static IP", "lb": "Load balancer", "storage": "Storage bucket",
    "firewall": "Firewall rule", "service_account": "Service account key", "bq": "BigQuery reservation",
}
TIER_ORDER = {"SAFE_TO_ACT": 0, "NEEDS_APPROVAL": 1, "DO_NOT_ACT": 2}


# ---- Scanner agent -------------------------------------------------------------------------
def exposure_points(r):
    pts = 0
    if r["public"]:
        pts += 10 if r["data_class"] == "pii" else 7
    if not r["encrypted"] and r["type"] in ("disk", "storage", "snapshot"):
        pts += 4
    return pts


def _f(r, code, title, action, savings, risk, why, **extra):
    return dict(resource=r["id"], resource_type=r["type"], project=r["project"], env=r["env"],
                owner=r["owner"], code=code, title=title, action=action,
                monthly_savings=round(savings, 2), blocked_savings=0.0, risk_reduction=risk,
                why=why, dependents=[], status="PROPOSED", tier=None, reasons=[], **extra)


def scan(resources):
    out = []
    for r in resources:
        t, cost, cpu = r["type"], r["monthly_cost"], r["cpu_util"]
        label = TYPE_LABEL[t]
        cf = None
        if t in ("compute", "cloudsql") and cpu < IDLE_CPU:
            cf = _f(r, "IDLE_RESOURCE", f"Idle {label}: {cpu:.0f}% average CPU", "DELETE", cost,
                    exposure_points(r),
                    [f"Average CPU {cpu:.1f}% over the review window",
                     f"Last activity {r['last_used_days']} days ago"])
        elif t in DOWNSIZE_RATE and cpu < RIGHTSIZE_CPU:
            cf = _f(r, "OVERSIZED", f"Oversized {label}: {cpu:.0f}% average CPU", "DOWNSIZE",
                    cost * DOWNSIZE_RATE[t], 0,
                    [f"Average CPU {cpu:.1f}% leaves large headroom",
                     f"Assumes about {int(DOWNSIZE_RATE[t] * 100)}% saving from the next size down"])
        elif t == "disk" and not r["attached"]:
            cf = _f(r, "UNATTACHED_DISK", f"Unattached disk, unused {r['last_used_days']} days", "DELETE",
                    cost, exposure_points(r),
                    ["Not attached to any VM"] + ([] if r["encrypted"] else ["Not encrypted with a managed key"]))
        elif t == "snapshot" and r["last_used_days"] > 180:
            cf = _f(r, "OLD_SNAPSHOT", f"Old snapshot, unused {r['last_used_days']} days", "DELETE", cost,
                    exposure_points(r),
                    ["No restore or copy in over 180 days"] + (["Snapshot is publicly readable"] if r["public"] else []))
        elif t == "address" and not r["attached"]:
            cf = _f(r, "UNUSED_IP", "Reserved static IP not in use", "RELEASE", cost, 0,
                    ["Billed while not attached to a resource"])
        elif t == "lb" and r["last_used_days"] >= 60:
            cf = _f(r, "IDLE_LB", f"Load balancer with no traffic for {r['last_used_days']} days", "DELETE",
                    cost, 0, ["No requests in the review window"])
        elif t == "storage" and r["log_retention_days"] and r["log_retention_days"] > POLICY_DEFAULT_LOG_DAYS:
            cur = r["log_retention_days"]
            cf = _f(r, "LOG_RETENTION", f"Logs kept {cur} days; cost policy suggests {POLICY_DEFAULT_LOG_DAYS}",
                    "REDUCE_RETENTION", cost * (1 - POLICY_DEFAULT_LOG_DAYS / cur), 0,
                    [f"Retention is {cur} days, default cost policy is {POLICY_DEFAULT_LOG_DAYS}"],
                    target_days=POLICY_DEFAULT_LOG_DAYS)
        if cf:
            out.append(cf)
        if cf and cf["action"] == "DELETE":
            continue  # deletion already removes any exposure on this resource
        # security-only findings
        if r["public"] and t in ("storage", "firewall", "snapshot"):
            pii = r["data_class"] == "pii"
            out.append(_f(r, "PUBLIC_EXPOSURE",
                          f"{label} open to the internet" + (" and holds PII" if pii else ""),
                          "RESTRICT_PUBLIC_ACCESS", 0, 10 if pii else 7,
                          ["Publicly reachable"] + (["Labeled as containing personal data"] if pii else [])))
        if t == "service_account" and (r["sa_key_age_days"] or 0) > 365 and r["last_used_days"] > 90:
            owner_role = "roles/owner" in r["sa_roles"]
            out.append(_f(r, "STALE_KEY", f"Service account key {r['sa_key_age_days']} days old, unused {r['last_used_days']} days",
                          "ROTATE_KEY", 0, 10 if owner_role else 7,
                          ["Key not rotated in over a year"] + (["Holds the broad Owner role"] if owner_role else [])))
        if not r["encrypted"] and t in ("disk", "storage") and not r["public"]:
            out.append(_f(r, "UNENCRYPTED", f"{label} without managed encryption", "ENABLE_CMEK", 0, 4,
                          ["Customer-managed key not enabled"]))
    return out


# ---- Dependency agent ----------------------------------------------------------------------
def check_dependencies(findings, resources):
    dependents = defaultdict(list)
    for r in resources:
        for d in r["depends_on"]:
            dependents[d].append(r["id"])
    deleting = {f["resource"] for f in findings if f["action"] == "DELETE"}
    for f in findings:
        deps = dependents.get(f["resource"], [])
        f["dependents"] = deps
        live = [d for d in deps if d not in deleting]
        f["live_dependents"] = live
        if f["action"] == "DELETE" and live:
            _block(f, f"Still used by {', '.join(live)}; deleting it would break them")
        elif f["action"] == "DOWNSIZE" and live:
            f["reasons"].append(f"Used by {', '.join(live)}; check them after resizing")


def _block(f, reason):
    if f["status"] != "BLOCKED":
        f["blocked_savings"] = f["monthly_savings"]
        f["monthly_savings"] = 0.0
        f["risk_reduction"] = 0
    f["status"] = "BLOCKED"
    f["reasons"].append(reason)


# ---- Guardrail agent -----------------------------------------------------------------------
def apply_guardrails(findings, by_id):
    for f in findings:
        r = by_id[f["resource"]]
        if f["action"] == "DELETE":
            if r["labels"].get("dr") == "true":
                _block(f, "Labeled as a disaster-recovery replica; low CPU is expected, deleting removes failover")
            if r["labels"].get("legal_hold") == "true":
                _block(f, "Under legal hold; deletion is not permitted")
        if f["code"] == "LOG_RETENTION":
            floor, cur = r["compliance_retention_days"] or 0, r["log_retention_days"]
            naive = f["monthly_savings"]
            if floor >= cur:
                _block(f, f"Already at the {floor}-day compliance minimum; cutting to {f['target_days']} days "
                          f"would breach it (naive saving ${naive:,.0f}/month)")
            elif f["target_days"] < floor:
                f["reasons"].append(f"Cost policy of {f['target_days']} days breaches the {floor}-day compliance "
                                    f"minimum; adjusted to {floor} days (naive saving was ${naive:,.0f}/month)")
                f["target_days"] = floor
                f["monthly_savings"] = round(r["monthly_cost"] * (1 - floor / cur), 2)
                f["title"] = f"Logs kept {cur} days; compliance only needs {floor}"
                f["adjusted"] = True
        if f["status"] != "BLOCKED" and r["env"] == "prod" and f["action"] in ("DELETE", "DOWNSIZE", "REDUCE_RETENTION"):
            f["reasons"].append("Production resource: owner approval required")


# ---- Scoring agent -------------------------------------------------------------------------
def score_and_tier(findings):
    for f in findings:
        if f["status"] == "BLOCKED":
            f["tier"], f["priority"] = "DO_NOT_ACT", 0.0
            continue
        f["priority"] = round(f["monthly_savings"] + f["risk_reduction"] * RISK_USD_PER_POINT, 2)
        safe = (f["env"] in ("dev", "staging") and not f.get("live_dependents")
                and f["monthly_savings"] <= SAFE_SAVINGS_CAP)
        f["tier"] = "SAFE_TO_ACT" if safe else "NEEDS_APPROVAL"
        f["status"] = "RECOMMENDED"
    findings.sort(key=lambda x: (TIER_ORDER[x["tier"]], -x["priority"], -x["blocked_savings"]))
    for n, f in enumerate(findings, 1):
        f["id"] = f"F{n:03d}"


# ---- Forecast agent ------------------------------------------------------------------------
def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def forecast(billing_path, budgets):
    daily = defaultdict(lambda: defaultdict(float))
    with open(billing_path) as fh:
        for row in csv.DictReader(fh):
            daily[row["project"]][dt.date.fromisoformat(row["date"])] += float(row["cost_usd"])
    today = max(d for s in daily.values() for d in s)
    month_start = today.replace(day=1)
    month_end = (month_start + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    remaining = [today + dt.timedelta(days=k) for k in range(1, (month_end - today).days + 1)]
    out = {}
    for p, series in daily.items():
        dates = sorted(series)
        mtd = sum(v for d, v in series.items() if d >= month_start)
        hist = dates[-56:]
        overall = sum(series[d] for d in hist) / len(hist)
        by_wd = defaultdict(list)
        for d in hist:
            by_wd[d.weekday()].append(series[d])
        wf = {wd: (sum(v) / len(v)) / overall for wd, v in by_wd.items()}
        window = dates[-FORECAST_WINDOW:]
        adj = [series[d] / wf[d.weekday()] for d in window]
        n = len(adj)
        xm, ym = (n - 1) / 2, sum(adj) / n
        slope = sum((x - xm) * (a - ym) for x, a in enumerate(adj)) / sum((x - xm) ** 2 for x in range(n))
        icpt = ym - slope * xm
        resid = [a - (icpt + slope * x) for x, a in enumerate(adj)]
        sigma = math.sqrt(sum(e * e for e in resid) / (n - 2))
        fc = sum((icpt + slope * (n - 1 + k)) * wf[d.weekday()] for k, d in enumerate(remaining, 1))
        p50 = mtd + fc
        sd = sigma * math.sqrt(len(remaining))
        budget = budgets[p]
        out[p] = dict(month_end=month_end.isoformat(), as_of=today.isoformat(), mtd=round(mtd, 2),
                      p50=round(p50, 2), p10=round(p50 - 1.2816 * sd, 2), p90=round(p50 + 1.2816 * sd, 2),
                      budget=budget, over_p50=round(p50 - budget, 2),
                      prob_over=round(1 - _phi((budget - p50) / sd), 3) if sd > 0 else float(p50 > budget),
                      trend_per_day=round(slope, 2))
    return out


# ---- Orchestrator --------------------------------------------------------------------------
def run():
    resources = json.loads((DATA / "resources.json").read_text())
    budgets = json.loads((DATA / "budgets.json").read_text())
    by_id = {r["id"]: r for r in resources}

    findings = scan(resources)
    check_dependencies(findings, resources)
    apply_guardrails(findings, by_id)
    score_and_tier(findings)
    fc = forecast(DATA / "billing.csv", budgets)

    def tot(tier, key="monthly_savings"):
        return round(sum(f[key] for f in findings if f["tier"] == tier), 2)

    summary = dict(
        findings=len(findings),
        safe_savings=tot("SAFE_TO_ACT"), approval_savings=tot("NEEDS_APPROVAL"),
        blocked_unsafe_savings=tot("DO_NOT_ACT", "blocked_savings"),
        blocked_count=sum(1 for f in findings if f["tier"] == "DO_NOT_ACT"),
        risk_points_reducible=sum(f["risk_reduction"] for f in findings),
    )
    summary["total_realisable_savings"] = round(summary["safe_savings"] + summary["approval_savings"], 2)
    summary["annualised_savings"] = round(summary["total_realisable_savings"] * 12, 2)
    summary["monthly_run_rate"] = round(sum(r["monthly_cost"] for r in resources), 2)
    return dict(findings=findings, forecast=fc, summary=summary, resources=resources)


if __name__ == "__main__":
    res = run()
    print(json.dumps(res["summary"], indent=2))
