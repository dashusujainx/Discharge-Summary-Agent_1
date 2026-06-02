# Discharge Summary Agent

Agentic AI system for the Dscribe take-home assignment. It reads synthetic patient source-note PDFs and produces a structured discharge-summary draft for clinician review, with explicit flags for missing, pending, conflicting, unreadable, and medication-reconciliation items.

The system is intentionally conservative: it is a draft generator, not a final clinical documentation system.

## Setup

```powershell
uv sync
```

Create a `.env` file for Groq-backed extraction and scanned-PDF OCR:

```powershell
GROQ_API_KEY=your_key_here
```

Keep API keys out of git.

## Run the Agent

Run on one patient PDF:

```powershell
.venv\Scripts\python.exe main.py --input data\input\patient_2.pdf --patient-id patient_2 --ocr-provider groq
```

Run on a folder containing multiple source-note PDFs for the same patient:

```powershell
.venv\Scripts\python.exe main.py --input data\input\patient_2 --patient-id patient_2 --ocr-provider groq
```

Fast smoke test without LLM/OCR calls:

```powershell
.venv\Scripts\python.exe main.py --input data\input\patient_2.pdf --patient-id smoke_no_ocr --no-llm --no-ocr
```

Useful OCR controls:

```powershell
.venv\Scripts\python.exe main.py --input data\input\patient_2.pdf --patient-id patient_2 --ocr-provider groq --max-ocr-pages 5
```

Outputs are written to:

```text
outputs/<patient-id>/draft.md
outputs/<patient-id>/summary.json
outputs/<patient-id>/trace.jsonl
outputs/<patient-id>/ocr_cache/
```

`ocr_cache` avoids repeated Groq vision calls for pages that have already been extracted.

## Optional API

```powershell
.venv\Scripts\uvicorn.exe api.app:app --reload
```

Then open:

```text
http://127.0.0.1:8000/docs
```

## Agent Loop

The agent is a bounded from-scratch loop with a hard step cap:

1. Decide the next action from state.
2. Read PDFs with retry.
3. Use embedded text when available; for scanned PDFs, use OCR fallback.
4. Extract source-supported discharge-summary fields.
5. Reconcile admission vs discharge medications.
6. Call a mocked drug-interaction safety tool when discharge medications exist.
7. Validate missing required fields and conflicts.
8. Write `draft.md`, `summary.json`, and `trace.jsonl`.

Each trace event includes reasoning, action/tool chosen, inputs, result, and next decision.

## No-Fabrication Guardrail

Required sections are represented as structured Pydantic models. Any field that cannot be sourced from the documents remains `MISSING - clinician review required`.

The LLM prompt requires JSON output and source quotes. If Groq extraction fails or returns invalid content, the agent falls back to conservative local extraction and adds a review flag instead of silently continuing.

Image-only PDFs are handled safely. If embedded text and OCR both fail, the agent produces a mostly missing draft with explicit PDF-ingestion and required-field flags. Partial OCR failures are also flagged, so unread pages are not treated as evidence.

## Medication Reconciliation

Admission and discharge medications are normalized and compared. Added or stopped medications without a documented reason are flagged for reconciliation. The mocked interaction lookup demonstrates tool use and escalation; it should be replaced by a real medication-safety service in production.

## Part 2: Simulated Learning from Doctor Edits

Run the simulated reviewer and template-strategy learner:

```powershell
.venv\Scripts\python.exe learning\simulated_learning.py --summaries outputs\patient_2\summary.json --iterations 12 --out outputs\part2
```

The simulated doctor applies a hidden editing policy: it prefers explicit "not documented in source notes" wording and a stronger draft-only safety disclaimer. The learner treats lower normalized edit distance as better, runs a small bandit over draft-rendering strategies, and writes:

```text
outputs/part2/part2_metrics.json
outputs/part2/learning_curve.csv
```

This is intentionally lightweight but measurable. It demonstrates the feedback loop without using real clinician edits.

## Limitations

The local heuristic extractor is conservative and will miss information. Groq vision OCR is needed for the provided scanned patient PDF unless local Tesseract plus Python OCR libraries are installed. The drug-interaction tool is mocked. The Part 2 reviewer is synthetic, so its improvement metric proves only that the system can learn a consistent editing preference, not that clinical quality improved.

To avoid gaming the learning loop, the renderer only changes phrasing/style around missing-data and safety language. It does not remove required sections, weaken missing-data flags, or invent clinical facts to reduce edit distance.

With more time, I would add document type classification, table-aware medication extraction, stronger citation verification, multiple-patient evaluation, real OCR quality scoring, and integration with a verified clinical medication knowledge base.

## Video Demo Checklist

Record a 3-5 minute demo showing:

1. Run the agent on `patient_2` with Groq OCR.
2. Open `outputs/patient_2/draft.md`.
3. Open `outputs/patient_2/trace.jsonl` and point to a decision where the agent flagged missing/unreadable/pending data instead of guessing.
4. Run `learning\simulated_learning.py` and show `part2_metrics.json` if including Part 2.
5. Mention that the output is a clinician-review draft and not a finalized discharge summary.
