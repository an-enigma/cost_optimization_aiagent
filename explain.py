"""Gemini explainer (drop-in). Does NOT change any existing file.

Run after the original prototype is in the same folder:
    pip install google-genai          # only needed once
    python3 explain.py                # uses Gemini on Vertex AI
    python3 explain.py --offline      # no Gemini: uses the plain template text (for a dry run)

What it does:
  1. Runs the existing engine (rules, guardrails, scoring, forecast) exactly as before.
  2. Asks Gemini to write a 2-sentence plain-language note per finding. Gemini only gets the facts
     the engine already produced and may not add numbers of its own.
  3. Rejects any note that mentions a dollar figure the engine did not produce, and falls back to
     the template text for that finding.
  4. Writes email drafts to out/emails_ai/ and the notes to out/explanations.json.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import engine
import notify

HERE = Path(__file__).parent
OUT = HERE / "out"

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
# Check the exact model name available in your sandbox (Vertex AI Studio > Model Garden) and set it:
#   export GEMINI_MODEL=<model name>
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SYSTEM = (
    "You write short notes for cloud resource owners who are not engineers. "
    "Use only the facts provided. Never invent numbers, names or dates. "
    "Write exactly two plain sentences: what was found and why it matters, then what the owner should do. "
    "If the tier is DO_NOT_ACT, explain why the saving was blocked and say no action is needed. "
    "Do not say anything has been changed; this is a recommendation only."
)


def usd_figures(text):
    return [float(x.replace(",", "")) for x in re.findall(r"\$([\d,]+(?:\.\d+)?)", text)]


def allowed_figures(f):
    base = [f["monthly_savings"], f["blocked_savings"]]
    vals = set()
    for b in base:
        if b:
            vals.update({round(b, 2), round(b), round(b * 12, 2), round(b * 12)})
    return vals


def valid(f, text):
    ok = allowed_figures(f)
    return all(round(v, 2) in ok or round(v) in ok for v in usd_figures(text))


def template_note(f):
    reasons = "; ".join(f["why"] + f["reasons"])
    step = "" if f["tier"] == "DO_NOT_ACT" else " " + notify._step(f)
    return f"{f['title']}. {reasons}.{step}".strip()


def facts(f):
    return json.dumps({k: f[k] for k in (
        "resource", "resource_type", "env", "title", "action", "tier",
        "monthly_savings", "blocked_savings", "risk_reduction", "why", "reasons", "dependents")}, indent=1)


def make_client():
    from google import genai
    if not PROJECT:
        sys.exit("Set your project first: export GOOGLE_CLOUD_PROJECT=<your-project-id>")
    return genai.Client(vertexai=True, project=PROJECT, location=LOCATION)


def explain_all(findings, offline):
    notes, source = {}, {}
    client = None if offline else make_client()
    types = None if offline else __import__("google.genai.types", fromlist=["x"])
    for f in findings:
        text, src = None, "template"
        if client:
            try:
                resp = client.models.generate_content(
                    model=MODEL, contents="Facts:\n" + facts(f),
                    config=types.GenerateContentConfig(system_instruction=SYSTEM, temperature=0.2))
                cand = (resp.text or "").strip()
                if cand and valid(f, cand):
                    text, src = cand, "gemini"
                else:
                    print(f"  {f['id']}: Gemini note rejected (empty or unsupported figure); using template")
            except Exception as e:  # keep the demo alive if the model call fails
                print(f"  {f['id']}: Gemini call failed ({type(e).__name__}: {e}); using template")
        notes[f["id"]], source[f["id"]] = text or template_note(f), src
    return notes, source


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip Gemini and use template text")
    args = ap.parse_args()

    if not (HERE / "data" / "resources.json").exists():
        import gen_data
        gen_data.main()
    res = engine.run()
    notes, source = explain_all(res["findings"], args.offline)

    OUT.mkdir(exist_ok=True)
    (OUT / "explanations.json").write_text(json.dumps(
        {i: {"note": notes[i], "source": source[i]} for i in notes}, indent=2))

    # Put the note into each email card without touching notify.py
    original_card = notify._card

    def card_with_note(f):
        html_card = original_card(f)
        note = notify.html.escape(notes[f["id"]])
        tag = "AI-written" if source[f["id"]] == "gemini" else "Template"
        extra = (f"<p style='margin:8px 0 0;color:#202124'>{note} "
                 f"<span style='color:#888;font-size:11px'>({tag})</span></p>")
        return html_card[:-len("</div>")] + extra + "</div>"

    notify._card = card_with_note
    notify.write_all(res, OUT / "emails_ai")
    n_ai = sum(1 for s in source.values() if s == "gemini")
    print(f"Done: {len(notes)} notes ({n_ai} from Gemini, {len(notes) - n_ai} template).")
    print("Open out/emails_ai/*.html and out/explanations.json")


if __name__ == "__main__":
    main()