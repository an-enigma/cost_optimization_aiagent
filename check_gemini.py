"""Finds which region works for your Gemini model name.

    python3 check_gemini.py                       # uses GEMINI_MODEL, or gemini-2.5-flash
    python3 check_gemini.py <model-name>          # try a specific name
"""
import os
import sys

from google import genai

project = os.environ.get("GOOGLE_CLOUD_PROJECT")
model = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
if not project:
    sys.exit("Set your project first: export GOOGLE_CLOUD_PROJECT=<project-id>")

print(f"Project {project}, model {model}")
works = []
for loc in ("global", "us-central1", "us-east5", "europe-west4", "europe-west1", "asia-south1", "asia-southeast1"):
    try:
        client = genai.Client(vertexai=True, project=project, location=loc)
        r = client.models.generate_content(model=model, contents="Reply with the single word OK")
        print(f"  {loc:16} WORKS   -> {r.text.strip()[:20]!r}")
        works.append(loc)
    except Exception as e:
        msg = str(e).replace("\n", " ")
        print(f"  {loc:16} fails   -> {msg[:110]}")
if works:
    print(f"\nUse:  export GOOGLE_CLOUD_LOCATION={works[0]}   and set GOOGLE_CLOUD_LOCATION={works[0]} in the three .env files")
else:
    print("\nNo region worked. Check the model name in Vertex AI Studio > Model Garden, or ask the organisers which model and region the sandbox allows.")