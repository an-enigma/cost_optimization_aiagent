"""Ask-the-agent for finance and engineers. Answers only from the latest pipeline run.

Run pipeline first (python3 pipeline.py or the finops_adk agent), then: adk web, pick "finops_analyst".
Try: "Why is ml-sandbox forecast to go over budget?"  or  "Which savings did you refuse to recommend, and why?"
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
for _line in (HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []:
    _line = _line.strip()
    if _line and not _line.startswith("#") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

from google.adk.agents import LlmAgent  # noqa: E402

import pipeline  # noqa: E402

FIELDS = ("id", "resource", "project", "env", "owner", "title", "tier", "action", "monthly_savings",
          "blocked_savings", "risk_reduction", "why", "reasons")


def get_overview() -> dict:
    """Headline numbers from the latest run: savings, blocked savings, tier counts and note-check results."""
    S = pipeline.load_state()
    if "summary" not in S:
        return {"error": "No completed run found. Run the pipeline first."}
    return {"run_id": S["run_id"], "summary": S["summary"], "grounding": S["grounding"]}


def list_findings(tier: str = "", project: str = "") -> dict:
    """List findings, optionally filtered by tier (SAFE_TO_ACT, NEEDS_APPROVAL, DO_NOT_ACT) and/or project name."""
    S = pipeline.load_state()
    rows = [{k: f[k] for k in FIELDS} for f in S.get("findings", [])
            if (not tier or f["tier"] == tier.upper()) and (not project or f["project"] == project)]
    return {"count": len(rows), "findings": rows}


def get_forecast(project: str = "") -> dict:
    """Month-end spend forecast against budget, for one project or all of them."""
    fc = pipeline.load_state().get("forecast", {})
    return {p: v for p, v in fc.items() if not project or p == project}


analyst = LlmAgent(
    name="finops_analyst",
    model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
    description="Answers finance and engineering questions about the latest cost and security review.",
    tools=[get_overview, list_findings, get_forecast],
    instruction=(
        "You answer questions about the latest cloud cost and security review. Use only data returned by your "
        "tools. Quote dollar figures exactly as returned and cite finding IDs (for example F017) for every "
        "claim about a specific resource. Explain in plain terms for a finance reader, without jargon. If the "
        "tools do not contain the answer, say so. You cannot approve, send, delete or change anything; if "
        "asked to, explain that a person must approve drafts on the review screen."))

root_agent = analyst
