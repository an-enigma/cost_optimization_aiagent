"""ADK agent team: a fixed six-step pipeline. Each agent owns one tool; the decision logic is plain code.

Run in Cloud Shell (from the finops_agent folder):   adk web
Then pick "finops_adk" and send the message:  Run the review
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()

from google.adk.agents import LlmAgent, SequentialAgent  # noqa: E402

import pipeline  # noqa: E402

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

RULE = ("Call the tool `{tool}` exactly once, with no arguments. Then reply with one or two plain sentences "
        "that use only the numbers the tool returned. Do not add advice or change any figure. {extra}")


def _agent(name, description, tool, extra=""):
    return LlmAgent(name=name, model=MODEL, description=description, tools=[tool],
                    instruction=RULE.format(tool=tool.__name__, extra=extra))


scanner = _agent("scanner", "Finds idle or oversized resources, wasteful settings and security risks.",
                 pipeline.scan_estate)
guardrail = _agent("dependency_and_guardrail",
                   "Checks what depends on each fix, blocks unsafe savings, then scores and tiers findings.",
                   pipeline.apply_safety_checks)
case_reviewer = _agent("case_reviewer",
                       "Gemini reads owner notes and may add caution to a finding, never remove it.",
                       pipeline.review_cases_with_gemini,
                       "Say how many findings were reviewed and whether any were made more cautious.")
forecaster = _agent("forecaster", "Forecasts month-end spend per project against budget.",
                    pipeline.forecast_budgets, "Name any project forecast to go over budget.")
explainer = _agent("explainer",
                   "Gemini writes a plain-language note per finding; code checks every note against the facts.",
                   pipeline.write_checked_notes,
                   "State how many notes passed every check and how many fell back to template text.")
queue = _agent("review_queue", "Drafts the owner emails and puts them in the human review queue.",
               pipeline.queue_for_human_review,
               "End by telling the user that a person must approve the drafts on the review screen. "
               "You cannot approve, send or release anything yourself.")

root_agent = SequentialAgent(
    name="finops_pipeline",
    description="Cloud cost and security review: scan, check, review, forecast, explain, queue for human approval.",
    sub_agents=[scanner, guardrail, case_reviewer, forecaster, explainer, queue])
