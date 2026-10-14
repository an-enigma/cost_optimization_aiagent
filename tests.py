"""Checks that the demo behaves the way the pitch says it does. Run after run.py."""
import json
from pathlib import Path

import engine

res = engine.run()
F = {f["resource"] + ":" + f["code"]: f for f in res["findings"]}
by_res = {}
for f in res["findings"]:
    by_res.setdefault(f["resource"], []).append(f)
s = res["summary"]

# 1. DR replica looks idle but must be blocked by the guardrail
f = F["orders-db-dr-replica:IDLE_RESOURCE"]
assert f["tier"] == "DO_NOT_ACT" and f["monthly_savings"] == 0 and f["blocked_savings"] == 880, f

# 2. A "idle" VM that prod depends on is blocked by the dependency check
f = F["batch-vm-legacy:IDLE_RESOURCE"]
assert f["tier"] == "DO_NOT_ACT" and "prod-api-vm-01" in f["live_dependents"], f

# 3. Security log bucket: cost cut would breach compliance, so blocked
f = F["logs-bucket-security:LOG_RETENTION"]
assert f["tier"] == "DO_NOT_ACT" and f["blocked_savings"] > 0, f

# 4. Audit log bucket: naive 90d proposal is adjusted up to the 365d compliance floor
f = F["logs-bucket-audit:LOG_RETENTION"]
assert f["target_days"] == 365 and f.get("adjusted") and round(f["monthly_savings"], 2) == round(520 * (1 - 365 / 730), 2), f

# 5. Legal hold disk blocked
assert F["disk-orphan-c:UNATTACHED_DISK"]["tier"] == "DO_NOT_ACT"

# 6. Double value: public snapshot deletion saves money AND removes exposure
f = F["snap-archive-2023:OLD_SNAPSHOT"]
assert f["monthly_savings"] == 130 and f["risk_reduction"] == 7, f
assert "snap-archive-2023" not in {x["resource"] for x in res["findings"] if x["code"] == "PUBLIC_EXPOSURE"}

# 7. Risk-only findings exist and carry no savings
assert F["pii-export-bucket:PUBLIC_EXPOSURE"]["risk_reduction"] == 10
assert F["sa-ci-legacy:STALE_KEY"]["risk_reduction"] == 10

# 8. Dev idle VMs are safe to act; prod resize needs approval
assert F["dev-sandbox-vm-07:IDLE_RESOURCE"]["tier"] == "SAFE_TO_ACT"
assert F["prod-gke-nodepool:OVERSIZED"]["tier"] == "NEEDS_APPROVAL"

# 9. Totals reconcile: blocked savings never leak into realisable savings
realisable = sum(f["monthly_savings"] for f in res["findings"] if f["tier"] != "DO_NOT_ACT")
assert abs(realisable - s["total_realisable_savings"]) < 0.01
assert all(f["monthly_savings"] == 0 for f in res["findings"] if f["tier"] == "DO_NOT_ACT")
assert abs(s["safe_savings"] + s["approval_savings"] - s["total_realisable_savings"]) < 0.01

# 10. Forecast: ml-sandbox projected over budget, core-prod and data-platform within
fc = res["forecast"]
assert fc["ml-sandbox"]["over_p50"] > 0 and fc["ml-sandbox"]["prob_over"] > 0.9, fc["ml-sandbox"]
assert fc["core-prod"]["over_p50"] < 0 and fc["data-platform"]["over_p50"] < 0, fc
for v in fc.values():
    assert v["p10"] < v["p50"] < v["p90"]

# 11. Every owner has a drafted email
emails = {p.stem for p in Path(__file__).parent.joinpath("out/emails").glob("*.eml")}
owners = {f["owner"].split("@")[0] for f in res["findings"]}
assert owners <= emails and "finance-forecast" in emails, (owners, emails)

print("All checks passed")
