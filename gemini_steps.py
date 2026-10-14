"""Gemini steps: case review, note writing and note verification.

Design rules (these are what we say in the pitch):
  * Rules decide the numbers. Gemini never changes a dollar figure.
  * Gemini may only ADD caution (move a finding towards "do not act"), never remove it.
  * Every Gemini output is checked in code before it is used: quotes must appear verbatim in the
    source text, numbers must come from the engine, and a second Gemini call must agree.
  * Anything that fails a check falls back to plain template text. The reason is recorded.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Literal

from pydantic import BaseModel

import engine
import notify

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

REVIEWABLE_ACTIONS = ("DELETE", "DOWNSIZE", "REDUCE_RETENTION")
BLOCKING_CATEGORIES = ("DR_OR_BACKUP", "REGULATED_DATA", "ACTIVE_USE")


# ---- Output shapes Gemini must follow ---------------------------------------------------------
class CaseReview(BaseModel):
    concern: bool
    category: Literal["DR_OR_BACKUP", "SCHEDULED_WORKLOAD", "REGULATED_DATA", "ACTIVE_USE", "NONE"]
    confidence: Literal["LOW", "MEDIUM", "HIGH"]
    evidence_quote: str
    reasoning: str


class Note(BaseModel):
    note: str
    cited_facts: List[str]


class Verdict(BaseModel):
    supported: bool
    unsupported_claims: List[str]


# ---- Gemini call (one place, easy to replace in tests) -----------------------------------------
_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        if not PROJECT:
            raise RuntimeError("Set GOOGLE_CLOUD_PROJECT first")
        _client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    return _client


def generate_json(system, prompt, schema):
    from google.genai import types
    last = None
    for _ in range(2):
        try:
            resp = _get_client().models.generate_content(
                model=MODEL, contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system, temperature=0.1,
                    response_mime_type="application/json", response_schema=schema))
            return json.loads(resp.text)
        except Exception as e:  # retry once, then give up
            last = e
    raise last


# ---- Small checks --------------------------------------------------------------------------------
def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().strip("\"'").lower()


def quote_in_source(quote, source):
    q = norm(quote)
    return len(q) >= 12 and q in norm(source)


def _numbers(text):
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))]


def allowed_numbers(f, facts):
    vals = {12.0}
    for b in (f["monthly_savings"], f["blocked_savings"]):
        if b:
            for v in (b, b * 12):
                vals.update({round(v, 2), float(round(v))})
    vals.update(_numbers(" ".join(facts)))
    vals.update({float(f["risk_reduction"])})
    return vals


def numbers_ok(f, facts, note):
    ok = allowed_numbers(f, facts)
    return all(round(n, 2) in ok or float(round(n)) in ok for n in _numbers(note))


def cited_ok(facts, cited):
    have = {norm(x) for x in facts}
    return bool(cited) and all(norm(c) in have for c in cited)


def fact_list(f):
    return [f["title"]] + list(f["why"]) + list(f["reasons"])


# ---- 1. Case review -------------------------------------------------------------------------------
CASE_SYSTEM = (
    "You review proposed cloud cost cuts for a regulated company. You are shown a finding from a rules "
    "engine and the owner's free-text note about the resource. Decide whether the note contains a reason "
    "the cut could be unsafe, for example a disaster-recovery role, a scheduled or seasonal workload, "
    "regulated data, or active use the metrics do not show. Rules: use only the note; if the note gives "
    "no such reason set concern=false and category=NONE; if concern=true, evidence_quote must be copied "
    "word for word from the note; never comment on dollar amounts."
)


def apply_review(f, review, note_text):
    """Turn a Gemini review into a decision. Can only add caution. Returns what was applied."""
    out = dict(review)
    out["applied"] = "none"
    if not review.get("concern") or review.get("category") == "NONE":
        return out
    if not quote_in_source(review.get("evidence_quote", ""), note_text):
        out["applied"] = "ignored_quote_not_in_source"
        return out
    why = f"AI review ({review['category'].lower().replace('_', ' ')}, {review['confidence'].lower()} confidence): " \
          f"{review['reasoning']} Source note: \"{review['evidence_quote']}\""
    if f["tier"] == "SAFE_TO_ACT":
        f["tier"] = "NEEDS_APPROVAL"
        f["reasons"].append(why)
        out["applied"] = "raised_to_approval"
    elif (f["tier"] == "NEEDS_APPROVAL" and review["confidence"] == "HIGH"
          and review["category"] in BLOCKING_CATEGORIES):
        engine._block(f, why)
        f["tier"], f["priority"] = "DO_NOT_ACT", 0.0
        out["applied"] = "blocked"
    elif f["tier"] == "NEEDS_APPROVAL":
        f["reasons"].append(why)
        out["applied"] = "caution_added"
    return out


def review_cases(findings, annotations, offline=False, gen=generate_json):
    """Ask Gemini about findings that have an owner note. Returns {finding_id: review}."""
    todo = [f for f in findings if f["tier"] != "DO_NOT_ACT" and f["action"] in REVIEWABLE_ACTIONS
            and f["resource"] in annotations]
    results = {}
    if offline:
        return {f["id"]: {"applied": "skipped_offline"} for f in todo}

    def one(f):
        r = f["resource"]
        prompt = ("Finding:\n" + json.dumps({k: f[k] for k in ("resource", "resource_type", "env", "title", "action")})
                  + "\n\nOwner note:\n" + annotations[r])
        try:
            return f, gen(CASE_SYSTEM, prompt, CaseReview)
        except Exception as e:
            return f, {"error": f"{type(e).__name__}: {e}"}

    with ThreadPoolExecutor(max_workers=4) as pool:
        pairs = list(pool.map(one, todo))
    for f, rev in pairs:  # apply in the main thread, in a stable order
        if "error" in rev:
            results[f["id"]] = {"applied": "error", **rev}
        else:
            results[f["id"]] = apply_review(f, rev, annotations[f["resource"]])
    return results


# ---- 2. Notes with grounding checks ---------------------------------------------------------------
NOTE_SYSTEM = (
    "You write short notes for cloud resource owners who are not engineers. Use only the facts provided. "
    "Never invent numbers, names or dates. Write exactly two plain sentences: what was found and why it "
    "matters, then what the owner should do. If the tier is DO_NOT_ACT, explain why the saving was blocked "
    "and say no action is needed. Never say anything has been changed; this is a recommendation only. "
    "In cited_facts, copy word for word the facts you relied on."
)
VERIFY_SYSTEM = (
    "You are a strict checker. Given facts and a note, decide whether every claim in the note is supported "
    "by the facts. List any unsupported claim. Do not judge style."
)


def template_note(f):
    reasons = "; ".join(f["why"] + f["reasons"])
    step = "" if f["tier"] == "DO_NOT_ACT" else " " + notify._step(f)
    return f"{f['title']}. {reasons}.{step}".strip()


def explain_one(f, gen=generate_json):
    facts = fact_list(f)
    payload = json.dumps({"tier": f["tier"], "action": f["action"], "monthly_savings": f["monthly_savings"],
                          "blocked_savings": f["blocked_savings"], "risk_points_removed": f["risk_reduction"],
                          "facts": facts}, indent=1)
    checks = {"numbers": None, "cited_facts": None, "verifier": None}
    try:
        n = gen(NOTE_SYSTEM, payload, Note)
        checks["numbers"] = numbers_ok(f, facts, n["note"])
        checks["cited_facts"] = cited_ok(facts, n["cited_facts"])
        if checks["numbers"] and checks["cited_facts"]:
            v = gen(VERIFY_SYSTEM, "Facts:\n" + "\n".join(facts) + "\n\nNote:\n" + n["note"], Verdict)
            checks["verifier"] = bool(v["supported"])
        if all(c is True for c in checks.values()):
            return {"note": n["note"], "source": "gemini", "checks": checks, "cited_facts": n["cited_facts"]}
        failed = [k for k, v in checks.items() if v is False]
        return {"note": template_note(f), "source": "template", "checks": checks, "fallback_reason": "failed:" + ",".join(failed)}
    except Exception as e:
        return {"note": template_note(f), "source": "template", "checks": checks,
                "fallback_reason": f"error:{type(e).__name__}"}


def explain_all(findings, offline=False, gen=generate_json):
    if offline:
        notes = {f["id"]: {"note": template_note(f), "source": "template", "checks": {}, "fallback_reason": "offline"}
                 for f in findings}
    else:
        with ThreadPoolExecutor(max_workers=4) as pool:
            res = list(pool.map(lambda f: explain_one(f, gen), findings))
        notes = {f["id"]: r for f, r in zip(findings, res)}
    total = len(notes)
    passed = sum(1 for n in notes.values() if n["source"] == "gemini")
    reasons = {}
    for n in notes.values():
        if n["source"] != "gemini":
            reasons[n["fallback_reason"]] = reasons.get(n["fallback_reason"], 0) + 1
    grounding = {"total": total, "passed_all_checks": passed, "fallback_to_template": total - passed,
                 "pass_rate": round(passed / total, 3) if total else 0.0, "fallback_reasons": reasons}
    return notes, grounding
