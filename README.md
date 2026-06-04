# Discharge Summary Agent

> **Live Demo:** [https://discharge-summary-agent-iqho.onrender.com](https://discharge-summary-agent-iqho.onrender.com)

An agentic AI system that reads patient medical PDFs (scanned or digital) and generates structured discharge summary drafts for clinician review. Built for the Dscribe take-home assignment.

**Core principle:** Conservative by design — any clinical data that cannot be reliably sourced from the documents is explicitly marked as `MISSING - clinician review required` rather than guessed or fabricated.

---

## Live Demo

Upload a patient PDF at the live URL and get a structured discharge summary in minutes:

👉 **[https://discharge-summary-agent-iqho.onrender.com](https://discharge-summary-agent-iqho.onrender.com)**

- Select OCR provider: **Groq** for scanned PDFs, **None** for digital PDFs (faster)
- Small PDFs (1–5 pages): ~30–60 seconds
- Large PDFs (10–70 pages): 5–15 minutes (OCR per page via Groq vision)
- Results show structured draft + review flags directly in the browser

> Note: The free Render instance may take ~30 seconds to wake up on first request.

---

## What It Does

- Extracts text from PDFs — embedded text first, Groq vision OCR as fallback
- Uses an LLM (Groq Llama 3.1) to extract structured clinical facts with source citations
- Reconciles admission vs. discharge medications and flags unexplained changes
- Runs drug interaction screening and conflict detection
- Validates all required fields and flags anything missing
- Outputs a human-readable `draft.md`, structured `summary.json`, and full `trace.jsonl` audit trail

---

## Agent Loop (8-step bounded state machine)

1. **READ_PDFS** — Extract text (embedded → Groq OCR → Tesseract → error flag)
2. **EXTRACT_FACTS** — LLM extracts all clinical fields with source quotes
3. **RECONCILE_MEDICATIONS** — Compare admission vs. discharge meds, flag changes
4. **CHECK_SAFETY** — Detect conflicts, run drug interaction screening
5. **VALIDATE** — Verify all required fields, generate missing-field flags
6. **WRITE_OUTPUTS** — Write `draft.md`, `summary.json`, `trace.jsonl`

Each step is logged with reasoning, inputs, result, and next decision.

---

## No-Fabrication Guardrail

All required fields are Pydantic models defaulting to `MISSING - clinician review required`. The LLM is required to return JSON with source quotes. If extraction fails, the agent falls back to conservative local extraction and adds a review flag — it never silently continues or invents data.

---

## Outputs

```
outputs/<patient-id>/
├── draft.md          # Human-readable discharge summary
├── summary.json      # Structured clinical data
├── trace.jsonl       # Full audit trail with reasoning
└── ocr_cache/        # Cached OCR results (avoids repeated API calls)
```

---

## Local Setup

**Requirements:** Python 3.11+

```bash
uv sync
```

Create a `.env` file:

```
GROQ_API_KEY=your_key_here
```

### Run the Agent

Single PDF:
```bash
python main.py --input data/input/patient_2.pdf --patient-id patient_2 --ocr-provider groq
```

Folder of PDFs (same patient):
```bash
python main.py --input data/input/patient_2 --patient-id patient_2 --ocr-provider groq
```

Fast smoke test (no API calls):
```bash
python main.py --input data/input/patient_2.pdf --patient-id smoke_test --no-llm --no-ocr
```

Limit OCR pages for quick testing:
```bash
python main.py --input data/input/patient_2.pdf --patient-id test --ocr-provider groq --max-ocr-pages 5
```

### Run the API locally

```bash
uvicorn api.app:app --reload
```

Then open: `http://127.0.0.1:8000/docs`

---

## Part 2: Simulated Learning from Doctor Edits

A multi-armed bandit that learns which discharge summary presentation strategy requires fewer clinician edits:

```bash
python learning/simulated_learning.py --summaries outputs/patient_2/summary.json --iterations 12 --out outputs/part2
```

**Strategies tested:**
- Baseline: `MISSING - clinician review required`
- Explicit: `NOT DOCUMENTED in source notes — clinician must supply`
- Safety First: `⛔ CRITICAL MISSING — do not finalise without this value`

Outputs: `outputs/part2/part2_metrics.json` and `outputs/part2/learning_curve.csv`

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| LLM | Groq (Llama 3.1 8B) |
| Vision OCR | Groq (Llama 4 Scout) |
| Framework | FastAPI + Uvicorn |
| Agent | Custom state machine (LangGraph-inspired) |
| Data models | Pydantic v2 |
| PDF parsing | PyMuPDF |
| Hosting | Render (free tier) |

---

## Limitations & Future Work

- Drug interaction screening is rule-based; production use requires a real pharmacological API (OpenFDA, DrugBank)
- Limited to English-language PDFs
- Part 2 reviewer is synthetic — proves the feedback loop works, not that clinical quality improved
- No multi-patient timeline tracking

With more time: document type classification, table-aware medication extraction, real clinician feedback loop, HIPAA-compliant audit logging, and integration with a verified clinical knowledge base.

---

## Project Structure

```
discharge-summary-agent/
├── main.py                      # CLI entry point
├── agents/discharge_agent.py    # Core agent loop
├── api/app.py                   # FastAPI REST endpoints
├── api/ui.html                  # Web UI
├── models/schemas.py            # Pydantic data models
├── tools/pdf_reader.py          # PDF extraction + OCR
├── tools/safety.py              # Conflict detection + drug interactions
├── tools/trace.py               # Audit trail logger
├── learning/simulated_learning.py  # Part 2 bandit learner
└── outputs/                     # Generated summaries
```