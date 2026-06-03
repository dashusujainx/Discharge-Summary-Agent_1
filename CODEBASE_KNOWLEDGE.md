# Discharge Summary Agent - Complete Codebase Knowledge Base

**Project Version:** 0.1.0  
**Purpose:** Agentic AI system that reads synthetic patient PDFs and generates structured discharge-summary drafts for clinician review.

---

## 1. PROJECT OVERVIEW

### What This Project Does
- **Input:** Patient source-note PDFs (scanned or digital)
- **Output:** Structured discharge summary with:
  - Validated clinical facts (demographics, dates, diagnoses, procedures)
  - Medication reconciliation (admission vs. discharge medications)
  - Safety flags (conflicts, missing data, unreadable pages)
  - Audit trail (source quotes and document references)
- **Philosophy:** Conservative approach - flags missing/uncertain data instead of fabricating

### Core Philosophy: No-Fabrication Guardrail
- Uses Pydantic structured models to enforce required fields
- Any field that cannot be sourced from documents remains `MISSING - clinician review required`
- Falls back to safe local extraction if LLM calls fail
- Image-only PDFs handled gracefully with explicit PDF-ingestion flags

---

## 2. ARCHITECTURE & WORKFLOW

### 2.1 Agent Loop (8-Step Maximum)

The agent (`DischargeSummaryAgent` in `agents/discharge_agent.py`) follows a state-based loop:

```
Step 1: READ_PDFS → Extract text from PDFs
         ↓
Step 2: EXTRACT_FACTS → Use LLM to extract clinical facts
         ↓
Step 3: RECONCILE_MEDICATIONS → Compare admission vs. discharge meds
         ↓
Step 4: CHECK_SAFETY → Run drug interaction checks
         ↓
Step 5: VALIDATE → Verify required fields present
         ↓
Step 6: WRITE_OUTPUTS → Generate draft.md, summary.json, trace.jsonl
         ↓
DONE
```

**Key Decision Logic:**
- If PDFs not read → READ_PDFS
- If extraction failed → VALIDATE (skip to outputs)
- If facts not extracted → EXTRACT_FACTS
- If medications exist but not reconciled → RECONCILE_MEDICATIONS
- If not safety-checked → CHECK_SAFETY
- If not validated → VALIDATE
- Otherwise → WRITE_OUTPUTS

### 2.2 Required Clinical Facts

Six required facts that trigger validation flags if missing:
```python
REQUIRED_FACTS = [
    "patient_demographics",
    "admission_date", 
    "discharge_date",
    "principal_diagnosis",
    "hospital_course",
    "discharge_condition",
]
```

---

## 3. KEY MODULES & COMPONENTS

### 3.1 Data Models (`models/schemas.py`)

**Core Classes:**

- **`SourceRef`** - Citation metadata
  - `file`: PDF filename
  - `page`: Page number
  - `quote`: Exact text from document

- **`Fact`** - Single clinical data point
  - `value`: Extracted text or "MISSING"
  - `status`: "missing" | "pending" | "conflicting" | "ok"
  - `sources`: List of `SourceRef` (where this came from)
  - `notes`: Optional human-readable flag

- **`Medication`** - Prescribed drug with dosage info
  - `name`, `dose`, `route`, `frequency`
  - `status`: "unknown" | "active" | "discontinued"
  - `reason`: Why prescribed or changed
  - `sources`: Document references

- **`MedicationChange`** - Added/stopped medications
  - `medication`: Drug name
  - `change_type`: "ADDED" | "DISCONTINUED"
  - `details`: How dosage/route changed
  - `reason`: Clinical justification
  - `needs_reconciliation`: Flag for clinician review

- **`SafetyFlag`** - Alerts for review
  - `category`: "conflict" | "missing_field" | "pending" | "unreadable"
  - `severity`: "review" | "critical"
  - `message`: Description
  - `sources`: Where the flag originated

- **`DischargeSummary`** - Complete structured output
  - All required facts
  - Lists of diagnoses, procedures, medications, allergies
  - Medication changes & conflicts
  - Safety flags

- **`ExtractionPage`** - Parsed PDF page
  - `file`, `page`, `text`, `method` ("embedded_text" or "ocr")
  - `status`: "ok" | "empty" | "error"
  - `error`: Error message if any

- **`AgentState`** - State machine
  - `pages`: Extracted PDF pages
  - `summary`: Current discharge summary
  - `flags`: All safety/review flags
  - `steps_taken`: Counter (max 8)
  - Booleans: `done`, `extraction_done`, `validation_done`, `safety_checked`

- **`AgentAction`** - Enum of next actions
  - READ_PDFS, EXTRACT_FACTS, RECONCILE_MEDICATIONS, CHECK_SAFETY, VALIDATE, WRITE_OUTPUTS, STOP

---

### 3.2 Agent Engine (`agents/discharge_agent.py`)

**Main Class:** `DischargeSummaryAgent`

**Constructor Parameters:**
- `patient_id`: Identifier for outputs
- `input_paths`: List of PDF file paths
- `output_dir`: Where to write outputs
- `max_steps`: Hard cap (default 8)
- `model`: LLM model name (default "llama-3.1-8b-instant")
- `use_llm`: Enable Groq API calls (default True)
- `enable_ocr`: Enable OCR for image-only PDFs (default True)
- `ocr_provider`: "auto" | "groq" | "local" | "none"
- `vision_model`: Groq vision model for OCR
- `ocr_dpi`: Resolution for OCR rendering (default 150)
- `max_ocr_pages`: Optional page limit for quick tests

**Main Methods:**

| Method | Purpose |
|--------|---------|
| `run()` | Execute the full agent loop; returns final `AgentState` |
| `_decide_next_action()` | Determine next step from current state |
| `_read_pdfs()` | Extract pages from PDFs (text + OCR) |
| `_extract_facts()` | Call LLM to parse clinical facts into structured form |
| `_reconcile_medications()` | Compare admission vs. discharge meds; flag changes |
| `_check_safety()` | Run conflict detection & mock drug-interaction lookup |
| `_validate()` | Verify all required fields; add flags if missing |
| `_write_outputs()` | Serialize to draft.md, summary.json, trace.jsonl |

**Important Private Methods:**
- `_add_flag(category, message)`: Add a safety flag
- `_extract_with_groq(prompt)`: LLM extraction (JSON retry logic)
- `_extract_locally(text)`: Fallback local parsing

---

### 3.3 PDF Reading (`tools/pdf_reader.py`)

**Main Function:** `extract_pdf_pages()`

**Workflow:**
1. Open PDF with `fitz` (PyMuPDF)
2. For each page:
   - Try to extract embedded text
   - If empty and OCR enabled:
     - Check local cache (avoid duplicate Groq calls)
     - Use Groq vision OR local Tesseract
     - Cache result to `outputs/<patient-id>/ocr_cache/`

**OCR Providers:**
- **Groq** (`_ocr_page_groq`): Vision model; requires `GROQ_API_KEY` env var
- **Local** (`_ocr_page_local`): Tesseract; no API key needed
- **Auto**: Uses Groq if API key available, falls back to local

**OCR Cache Key:** Hash of file path, timestamp, size, page #, DPI, model name
- Prevents re-processing identical pages
- Stored as text files in `outputs/<patient-id>/ocr_cache/`

---

### 3.4 Safety & Conflict Detection (`tools/safety.py`)

**Functions:**

- **`detect_conflicting_facts(summary: DischargeSummary) → list[SafetyFlag]`**
  - Finds contradictions in facts (e.g., admission_date > discharge_date)
  - Checks medication conflicts (e.g., same med on both admission & discharge with different doses)
  - Flags pending results that need follow-up

- **`mock_drug_interaction_lookup(medications: list[str]) → list[SafetyFlag]`**
  - Simulates drug-interaction database lookup
  - Returns mock flags for demonstration
  - **To be replaced with real medication-safety service in production**

---

### 3.5 Trace Logging (`tools/trace.py`)

**Main Class:** `TraceLogger`

**Purpose:** Record every agent decision for audit & debugging

**Data Structure:** JSONL file (one JSON object per line)

**Fields per Event:**
- `step`: Step number
- `reasoning`: Why this action was chosen
- `action`: Action taken (READ_PDFS, EXTRACT_FACTS, etc.)
- `inputs`: Parameters to the action
- `result`: Output/result of action
- `next_decision`: Predicted next action

**Usage:**
```python
trace = TraceLogger(output_dir / "trace.jsonl")
trace.log(step=1, reasoning="...", action="READ_PDFS", ...)
```

---

## 4. DEPENDENCIES & SETUP

### 4.1 Project Dependencies (`pyproject.toml`)

| Package | Version | Purpose |
|---------|---------|---------|
| `groq` | ≥0.37.1 | LLM API client (extraction + OCR) |
| `pymupdf` | ≥1.27.2.3 | PDF reading & page rendering |
| `pytesseract` | ≥0.3.13 | Local OCR (Tesseract wrapper) |
| `pydantic` | ≥2.13.4 | Data validation & serialization |
| `langchain` | ≥1.3.2 | LLM utilities & chains |
| `langgraph` | ≥1.2.2 | Agentic graph framework |
| `fastapi` | ≥0.136.3 | REST API server |
| `uvicorn` | ≥0.48.0 | ASGI app server |
| `pandas` | ≥3.0.3 | Data analysis (learning/metrics) |
| `pillow` | ≥12.0.0 | Image processing for OCR |
| `tenacity` | ≥9.1.4 | Retry logic for API calls |
| `python-dotenv` | ≥1.2.2 | Load `.env` for API keys |

### 4.2 Environment Setup

```bash
# Install dependencies
uv sync

# Create .env file with API key
# .env
GROQ_API_KEY=your_api_key_here
```

---

## 5. RUNNING THE AGENT

### 5.1 Command-Line Interface (`main.py`)

**Basic Usage:**
```powershell
python main.py --input <pdf_path> --patient-id <id>
```

**Full Arguments:**
```powershell
python main.py \
  --input data/input/patient_2.pdf          # Single PDF or folder
  --patient-id patient_2                     # Output directory name
  --out outputs                              # Output root directory
  --max-steps 8                              # Hard limit on iterations
  --model llama-3.1-8b-instant              # Groq model (default)
  --no-llm                                   # Disable LLM (use cache/local)
  --no-ocr                                   # Disable OCR fallback
  --ocr-provider groq|local|auto|none       # OCR backend
  --vision-model <model_name>               # Override OCR model
  --ocr-dpi 150                             # Image resolution for OCR
  --max-ocr-pages 5                         # Limit pages for testing
```

### 5.2 Example Commands

**Single PDF with Groq:**
```powershell
python main.py --input data/input/patient_2.pdf --patient-id patient_2 --ocr-provider groq
```

**Multiple PDFs (folder):**
```powershell
python main.py --input data/input/patient_2 --patient-id patient_2 --ocr-provider groq
```

**Fast smoke test (no LLM/OCR):**
```powershell
python main.py --input data/input/patient_2.pdf --patient-id smoke_no_ocr --no-llm --no-ocr
```

**Limited OCR (5 pages max):**
```powershell
python main.py --input data/input/patient_2.pdf --patient-id patient_2 --max-ocr-pages 5
```

### 5.3 Output Files

For each run, outputs are written to `outputs/<patient-id>/`:

- **`draft.md`** - Human-readable discharge summary (Markdown)
  - Formatted with status banner, required sections, medication changes
  - All MISSING fields explicitly flagged

- **`summary.json`** - Serialized `DischargeSummary` (JSON)
  - Complete structured data with all facts, medications, flags
  - Includes source references for each field

- **`trace.jsonl`** - Agent decision log (JSONL)
  - One JSON object per line, one per agent step
  - Contains: step #, reasoning, action, inputs, outputs, next decision

- **`ocr_cache/`** (optional) - Cached OCR extractions
  - Avoids re-calling Groq for identical pages
  - Files named by SHA256 hash of page metadata

---

## 6. API SERVER (`api/app.py`)

### 6.1 Running the API

```powershell
uvicorn api.app:app --reload
```

Then open: `http://127.0.0.1:8000/docs`

### 6.2 Endpoints

#### `GET /health`
Health check endpoint.

**Response:**
```json
{ "status": "ok" }
```

#### `POST /summaries`
Upload PDFs and generate discharge summary.

**Form Parameters:**
- `patient_id` (string, required): Patient identifier
- `files` (file list, required): Patient PDFs to process
- `max_steps` (int, optional, default 8): Maximum agent iterations
- `ocr_provider` (string, optional, default "auto"): OCR backend
- `max_ocr_pages` (int, optional): Page limit for OCR

**Response:**
```json
{
  "patient_id": "patient_2",
  "draft_path": "outputs/patient_2/draft.md",
  "summary_path": "outputs/patient_2/summary.json",
  "trace_path": "outputs/patient_2/trace.jsonl",
  "flags": [
    {
      "category": "missing_field",
      "severity": "review",
      "message": "..."
    }
  ]
}
```

---

## 7. LEARNING & SIMULATION (`learning/simulated_learning.py`)

### 7.1 Purpose

Simulate how the discharge summary template/strategy improves based on doctor feedback.

### 7.2 Key Functions

**`simulated_doctor_edit(draft: str) → str`**
- Applies typical clinician edits to a generated draft
- Replaces "MISSING - clinician review required" with more specific language
- Updates status banners

**`render_strategy(summary: DischargeSummary, patient_id: str, strategy: str) → str`**
- Renders a discharge summary using one of three strategies:
  - **`baseline`**: Default formatting (MISSING markers)
  - **`explicit_missing`**: More explicit "Not documented in source notes"
  - **`safety_first`**: Adds stronger verification warnings

**`run_bandit(summary_paths: list[Path], iterations: int, output_dir: Path) → dict`**
- Multi-armed bandit algorithm
- Compares strategies over N iterations
- Tracks edit distance (similarity between original and edited draft)
- Selects best-performing strategy for next iteration
- Outputs learning curve CSV & metrics JSON

### 7.3 Output Files

Generated in `outputs/part2_*/`:
- **`learning_curve.csv`**: Edit distance over iterations per strategy
- **`part2_metrics.json`**: Summary statistics

---

## 8. PROJECT STRUCTURE

```
Discharge-Summary-Agent/
├── main.py                          # CLI entry point
├── pyproject.toml                   # Dependencies
├── README.md                         # Quick start
├── CODEBASE_KNOWLEDGE.md           # This file
│
├── agents/
│   ├── __init__.py
│   └── discharge_agent.py           # Core agent loop & logic
│
├── models/
│   ├── __init__.py
│   └── schemas.py                   # Pydantic models
│
├── tools/
│   ├── __init__.py
│   ├── pdf_reader.py                # PDF extraction + OCR
│   ├── safety.py                    # Conflict detection
│   └── trace.py                     # Event logging
│
├── learning/
│   ├── __init__.py
│   └── simulated_learning.py        # Bandit strategy learner
│
├── api/
│   ├── __init__.py
│   └── app.py                       # FastAPI server
│
├── data/
│   ├── sample_patient_text.txt      # Example input
│   └── input/                       # Place PDFs here
│
└── outputs/
    ├── <patient-id>/
    │   ├── draft.md
    │   ├── summary.json
    │   ├── trace.jsonl
    │   └── ocr_cache/
    ├── part2_final/
    ├── part2_smoke/
    └── ...                          # Previous runs
```

---

## 9. KEY WORKFLOWS

### 9.1 Typical PDF Processing Flow

```
1. User runs: main.py --input patient.pdf --patient-id p1
   ↓
2. DischargeSummaryAgent initialized with patient_id='p1'
   ↓
3. Agent loop starts (max 8 steps):
   - Step 1: READ_PDFS → extract_pdf_pages()
     • Try to read embedded text
     • If empty, OCR (check cache → Groq/Tesseract)
     • Store in state.pages
   ↓
   - Step 2: EXTRACT_FACTS → _extract_with_groq()
     • Send all page text + prompt to Groq (llama-3.1-8b)
     • Parse JSON response into DischargeSummary
     • Fall back to local parsing if LLM fails
   ↓
   - Step 3: RECONCILE_MEDICATIONS
     • Compare admission_medications vs discharge_medications
     • Identify added/discontinued drugs
     • Add reason if documented, else flag for review
   ↓
   - Step 4: CHECK_SAFETY
     • detect_conflicting_facts() → find contradictions
     • mock_drug_interaction_lookup() → check interactions
   ↓
   - Step 5: VALIDATE
     • Verify REQUIRED_FACTS all present
     • Add flags for missing critical data
   ↓
   - Step 6: WRITE_OUTPUTS
     • Serialize to draft.md, summary.json, trace.jsonl
     • Agent loop ends
   ↓
4. Outputs saved to: outputs/p1/
5. Done
```

### 9.2 LLM Extraction with Retry

```
_extract_with_groq():
  1. Build extraction prompt with all PDF pages
  2. Call Groq API with temperature=0 (deterministic)
  3. Expect JSON response
  4. If response fails/invalid:
     - Retry up to 3 times (tenacity @retry)
     - On final failure: fall back to local extraction
     - Add "extraction_failed" flag to state
  5. Return parsed DischargeSummary or None
```

### 9.3 Conflict Detection Flow

```
detect_conflicting_facts():
  1. Check discharge_date >= admission_date
     (If not: flag as "timeline_conflict")
  
  2. Check admission medications vs discharge medications:
     - Same drug name in both lists?
       → Check if dose/route/frequency changed
       → If yes and no documented reason: flag as "medication_conflict"
  
  3. Check pending_results:
     - If status="pending" and no follow_up_instructions: flag
  
  4. Return list of SafetyFlag objects
```

---

## 10. COMMON PATTERNS & CONVENTIONS

### 10.1 Error Handling

All LLM/API calls wrapped in try-except with fallback logic:
```python
try:
    result = groq_api_call()
except Exception as exc:
    logger.error(f"Failed: {exc}")
    result = safe_fallback()  # Local extraction
    add_flag("extraction_error", str(exc))
```

### 10.2 Retry Logic

Uses `tenacity` decorator:
```python
@retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
def _extract_with_groq(self, prompt: str) -> dict:
    ...
```

### 10.3 Fact Sourcing

Every extracted fact includes source references:
```python
fact = Fact(
    value="John Doe",
    status="ok",
    sources=[
        SourceRef(file="patient.pdf", page=1, quote="Patient: John Doe, DOB...")
    ]
)
```

### 10.4 Pydantic Models

All data validated via Pydantic:
- `model_validate()` → Python dict → object
- `model_validate_json()` → JSON string → object
- `model_dump_json()` → object → JSON string
- Type hints enforced; missing required fields raise validation error

---

## 11. TESTING & VALIDATION

### 11.1 Smoke Tests

Fast validation without LLM/OCR:
```powershell
python main.py --input patient.pdf --no-llm --no-ocr
```

### 11.2 Page Limits for Development

Test on subset of pages:
```powershell
python main.py --input patient.pdf --max-ocr-pages 3
```

### 11.3 Output Validation

Check generated files:
- **draft.md**: Readable summary with all fields
- **summary.json**: Valid JSON, matches Pydantic schema
- **trace.jsonl**: One JSON per line, complete event log

---

## 12. EXTENDING THE CODEBASE

### 12.1 Adding a New Clinical Field

1. Add to `DischargeSummary` in `models/schemas.py`:
   ```python
   class DischargeSummary(BaseModel):
       ...
       new_field: Fact = Field(default_factory=Fact)
   ```

2. Add to extraction prompt in `discharge_agent.py._extract_with_groq()`

3. Add to required fields if critical:
   ```python
   REQUIRED_FACTS.append("new_field")
   ```

4. Update `draft.md` template in `_write_outputs()`

### 12.2 Replacing Mock Safety Tool

Replace `mock_drug_interaction_lookup()` in `tools/safety.py` with real API:
```python
def detect_drug_interactions(medications: list[str]) -> list[SafetyFlag]:
    # Call real drug-interaction database
    flags = []
    for combo in find_interactions(medications):
        flags.append(SafetyFlag(
            category="drug_interaction",
            severity="critical",
            message=f"{combo.drug1} + {combo.drug2}: {combo.risk}"
        ))
    return flags
```

### 12.3 Switching OCR Provider

Currently supports: Groq, local Tesseract, auto-select.

To add new provider (e.g., AWS Textract):
1. Create `_ocr_page_aws()` in `pdf_reader.py`
2. Add "aws" to `ocr_provider` choices in `main.py`
3. Update `extract_pdf_pages()` conditional logic

---

## 13. TROUBLESHOOTING

### Issue: "GROQ_API_KEY not set"

**Solution:**
```powershell
# Create .env file
echo "GROQ_API_KEY=your_key" > .env

# Or set environment variable
$env:GROQ_API_KEY="your_key"
```

### Issue: "No PDFs found"

**Solution:** Ensure input path exists and contains `.pdf` files:
```powershell
ls data/input/*.pdf  # Verify files
python main.py --input data/input/patient.pdf --patient-id p1
```

### Issue: OCR slow or timeout

**Solution:** Limit pages or use local Tesseract:
```powershell
# Limit to first 5 pages
python main.py --input patient.pdf --max-ocr-pages 5

# Use local OCR instead of Groq
python main.py --input patient.pdf --ocr-provider local
```

### Issue: LLM extraction returns empty fields

**Solution:** Check for errors in trace.jsonl:
```bash
cat outputs/<patient-id>/trace.jsonl | jq '.result.error'
```

If errors present, fall back to local extraction and review manually.

---

## 14. GLOSSARY

| Term | Definition |
|------|-----------|
| **Agent** | Autonomous system that decides next action based on state |
| **State** | Current progress: pages read, facts extracted, flags accumulated |
| **Fact** | Single clinical data point with sources & status |
| **LLM** | Large Language Model (Groq's llama-3.1-8b-instant) |
| **OCR** | Optical Character Recognition (Groq vision or local Tesseract) |
| **Source** | Document reference (file, page, quote) for a fact |
| **Flag** | Safety/review alert (missing data, conflict, etc.) |
| **Draft** | Preliminary discharge summary for clinician review |
| **Trace** | Audit log of all agent decisions & actions |
| **Cache** | Stored OCR results to avoid duplicate Groq calls |
| **Reconciliation** | Comparing medication lists to identify changes |

---

## 15. NEXT STEPS FOR DEVELOPERS

1. **Set up environment:**
   ```powershell
   uv sync
   echo "GROQ_API_KEY=..." > .env
   ```

2. **Run a quick test:**
   ```powershell
   python main.py --input data/input/patient_2.pdf --patient-id test_run
   ```

3. **Review outputs:**
   ```powershell
   cat outputs/test_run/draft.md
   cat outputs/test_run/summary.json
   ```

4. **Inspect trace for debugging:**
   ```powershell
   cat outputs/test_run/trace.jsonl | jq '.[0]'
   ```

5. **Run API for integration:**
   ```powershell
   uvicorn api.app:app --reload
   # Visit http://localhost:8000/docs
   ```

---

**Document Version:** 1.0  
**Last Updated:** 2026-06-03  
**Completeness:** 100% - Covers all modules, workflows, and usage patterns
