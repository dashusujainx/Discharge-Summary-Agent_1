from __future__ import annotations
import shutil, tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from agents.discharge_agent import DischargeSummaryAgent

app = FastAPI(
    title="Discharge Summary Agent API",
    description="Upload patient PDFs → get a structured discharge summary draft.",
    version="0.1.0",
)

_UI_FILE = Path(__file__).parent / "ui.html"

@app.get("/", response_class=HTMLResponse)
def ui():
    return _UI_FILE.read_text()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/outputs/{patient_id}/draft")
def get_draft(patient_id: str):
    p = Path("outputs") / patient_id / "draft.md"
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"draft": p.read_text()}

@app.post("/summaries")
async def create_summary(
    patient_id: str = Form(...),
    files: list[UploadFile] = File(...),
    max_steps: int = Form(8),
    ocr_provider: str = Form("auto"),
    max_ocr_pages: int | None = Form(None),
):
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_paths = []
        for upload in files:
            dest = Path(tmpdir) / upload.filename
            with dest.open("wb") as f:
                shutil.copyfileobj(upload.file, f)
            pdf_paths.append(dest)

        output_dir = Path("outputs") / patient_id
        agent = DischargeSummaryAgent(
            patient_id=patient_id,
            input_paths=pdf_paths,
            output_dir=output_dir,
            max_steps=max_steps,
            ocr_provider=ocr_provider,
            max_ocr_pages=max_ocr_pages,
        )
        state = agent.run()

    return JSONResponse({
        "patient_id": patient_id,
        "draft_path": str(output_dir / "draft.md"),
        "summary_path": str(output_dir / "summary.json"),
        "trace_path": str(output_dir / "trace.jsonl"),
        "flags": [
            {"category": f.category, "severity": f.severity, "message": f.message}
            for f in state.flags
        ],
    })