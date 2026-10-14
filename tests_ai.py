"""Checks for the Gemini safeguards. No Gemini needed: model replies are faked.   python3 tests_ai.py"""
import copy
import json

import engine
import gemini_steps as g

res = engine.run()
F = {f["resource"] + ":" + f["code"]: f for f in res["findings"]}
NOTE = {k: v for k, v in json.load(open("data/annotations.json")).items() if not k.startswith("_")}


def fresh(key):
    return copy.deepcopy(F[key])


def review(**kw):
    base = dict(concern=True, category="SCHEDULED_WORKLOAD", confidence="MEDIUM", evidence_quote="", reasoning="Used rarely on purpose.")
    base.update(kw)
    return base


# 1. A quote that is not in the owner's note is ignored (no invented evidence)
f = fresh("dev-sandbox-vm-11:IDLE_RESOURCE")
out = g.apply_review(f, review(evidence_quote="This VM runs payroll every week"), NOTE["dev-sandbox-vm-11"])
assert out["applied"] == "ignored_quote_not_in_source" and f["tier"] == "SAFE_TO_ACT"

# 2. A verbatim quote raises a SAFE item to NEEDS_APPROVAL and records why
q = "Powered on only during close weeks"
out = g.apply_review(f, review(evidence_quote=q), NOTE["dev-sandbox-vm-11"])
assert out["applied"] == "raised_to_approval" and f["tier"] == "NEEDS_APPROVAL" and any(q in r for r in f["reasons"])
assert f["monthly_savings"] == 210  # Gemini never touches the numbers

# 3. Production node pool: medium confidence only adds a warning, tier unchanged
f = fresh("prod-gke-nodepool:OVERSIZED")
before = f["monthly_savings"]
out = g.apply_review(f, review(evidence_quote="Sized on purpose for the quarter-end peak"), NOTE["prod-gke-nodepool"])
assert out["applied"] == "caution_added" and f["tier"] == "NEEDS_APPROVAL" and f["monthly_savings"] == before

# 4. High-confidence regulated-data concern can block, and savings move to "blocked"
f = fresh("disk-orphan-b:UNATTACHED_DISK")
text = "Holds the only copy of the raw trial export."
out = g.apply_review(f, review(category="REGULATED_DATA", confidence="HIGH", evidence_quote="only copy of the raw trial export"), text)
assert out["applied"] == "blocked" and f["tier"] == "DO_NOT_ACT" and f["monthly_savings"] == 0 and f["blocked_savings"] == 96

# 5. Gemini can never lower caution: a "no concern" answer changes nothing, tiers never move towards SAFE
for key in ("dev-sandbox-vm-11:IDLE_RESOURCE", "prod-gke-nodepool:OVERSIZED", "orders-db-dr-replica:IDLE_RESOURCE"):
    f = fresh(key)
    t0 = engine.TIER_ORDER[f["tier"]]
    for rv in (review(concern=False, category="NONE"), review(evidence_quote="whatever text here ok")):
        g.apply_review(f, rv, "x")
        assert engine.TIER_ORDER[f["tier"]] >= t0

# 6. review_cases uses the fake model end to end and survives a model error
findings = copy.deepcopy(res["findings"])
for i, x in enumerate(findings):
    x["id"] = x.get("id", f"F{i}")
def fake_gen(system, prompt, schema):
    if schema is g.CaseReview:
        if "half-year restatement" in prompt:
            return review(evidence_quote="Powered on only during close weeks")
        if "quarter-end peak" in prompt:
            raise RuntimeError("boom")
        return review(concern=False, category="NONE")
    raise AssertionError
r = g.review_cases(findings, NOTE, gen=fake_gen)
outcomes = sorted(v["applied"] for v in r.values())
assert outcomes.count("raised_to_approval") == 1 and outcomes.count("error") == 1, outcomes

# 7. Note checks: invented dollar figure, invented number, uncited fact, failed verifier each fall back to template
f = fresh("snap-archive-2023:OLD_SNAPSHOT")
facts = g.fact_list(f)
good = {"note": "This old snapshot costs $130 a month and is publicly readable. Confirm nothing needs it, then delete it.",
        "cited_facts": [facts[1], facts[2]]}
assert g.numbers_ok(f, facts, good["note"]) and g.cited_ok(facts, good["cited_facts"])
assert not g.numbers_ok(f, facts, "It costs $900 a month.")
assert not g.numbers_ok(f, facts, "It has not been used for 90 days.")
assert not g.cited_ok(facts, ["Contains payroll data"]) and not g.cited_ok(facts, [])
def gen_with(note, supported=True):
    def gen(system, prompt, schema):
        return note if schema is g.Note else {"supported": supported, "unsupported_claims": []}
    return gen
assert g.explain_one(f, gen_with(good))["source"] == "gemini"
bad = dict(good, note="It costs $900 a month.")
r = g.explain_one(f, gen_with(bad)); assert r["source"] == "template" and "numbers" in r["fallback_reason"]
r = g.explain_one(f, gen_with(good, supported=False)); assert r["source"] == "template" and "verifier" in r["fallback_reason"]
def gen_err(system, prompt, schema): raise TimeoutError()
assert g.explain_one(f, gen_err)["fallback_reason"] == "error:TimeoutError"

# 8. Grounding report counts
notes, grounding = g.explain_all(findings, gen=gen_with(good))
assert grounding["total"] == len(findings) and grounding["passed_all_checks"] + grounding["fallback_to_template"] == grounding["total"]

print("All AI safeguard checks passed")
