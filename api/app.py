from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import FastAPI, File, Form, UploadFile

from agents.discharge_agent import DischargeSummaryAgent


app = FastAPI(title="Discharge Summary Agent")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/summaries")
async def create_summary(
    patient_id: str = Form(...),
    files: list[UploadFile] = File(...),
    max_steps: int = Form(8),
    ocr_provider: str = Form("auto"),
    max_ocr_pages: int | None = Form(None),
) -> dict[str, object]:
    upload_dir = Path("data/uploads") / patient_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    for upload in files:
        suffix = Path(upload.filename or "source.pdf").suffix or ".pdf"
        with NamedTemporaryFile(delete=False, suffix=suffix, dir=upload_dir) as tmp:
            shutil.copyfileobj(upload.file, tmp)
            paths.append(Path(tmp.name))

    output_dir = Path("outputs") / patient_id
    agent = DischargeSummaryAgent(
        patient_id=patient_id,
        input_paths=paths,
        output_dir=output_dir,
        max_steps=max_steps,
        ocr_provider=ocr_provider,
        max_ocr_pages=max_ocr_pages,
    )
    state = agent.run()

    return {
        "patient_id": patient_id,
        "draft_path": str(output_dir / "draft.md"),
        "summary_path": str(output_dir / "summary.json"),
        "trace_path": str(output_dir / "trace.jsonl"),
        "flags": [flag.model_dump() for flag in state.flags],
    }
