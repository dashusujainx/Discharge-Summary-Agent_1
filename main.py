from __future__ import annotations

import argparse
from pathlib import Path

from agents.discharge_agent import DischargeSummaryAgent


def _collect_pdfs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    return sorted(input_path.glob("*.pdf"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the discharge-summary agent on patient PDFs.")
    parser.add_argument("--input", required=True, help="A patient PDF or a folder containing patient PDFs.")
    parser.add_argument("--patient-id", default=None, help="Identifier used in output paths.")
    parser.add_argument("--out", default="outputs", help="Output directory.")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--model", default="llama-3.1-8b-instant")
    parser.add_argument("--no-llm", action="store_true", help="Disable Groq even if GROQ_API_KEY is set.")
    parser.add_argument("--no-ocr", action="store_true", help="Disable OCR fallback for image-only PDFs.")
    parser.add_argument(
        "--ocr-provider",
        choices=["auto", "groq", "local", "none"],
        default="auto",
        help="OCR backend for scanned PDFs. auto uses Groq when GROQ_API_KEY is set, otherwise local Tesseract.",
    )
    parser.add_argument(
        "--vision-model",
        default="meta-llama/llama-4-scout-17b-16e-instruct",
        help="Groq vision model used for OCR when --ocr-provider groq/auto.",
    )
    parser.add_argument("--ocr-dpi", type=int, default=150, help="Render DPI for OCR images.")
    parser.add_argument(
        "--max-ocr-pages",
        type=int,
        default=None,
        help="Optional page cap for quick smoke tests. Omit for full PDFs.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    pdfs = _collect_pdfs(input_path)
    if not pdfs:
        raise SystemExit(f"No PDFs found at {input_path}")

    patient_id = args.patient_id or input_path.stem.replace(" ", "_")
    output_dir = Path(args.out) / patient_id
    agent = DischargeSummaryAgent(
        patient_id=patient_id,
        input_paths=pdfs,
        output_dir=output_dir,
        max_steps=args.max_steps,
        model=args.model,
        use_llm=not args.no_llm,
        enable_ocr=not args.no_ocr,
        ocr_provider=args.ocr_provider,
        vision_model=args.vision_model,
        ocr_dpi=args.ocr_dpi,
        max_ocr_pages=args.max_ocr_pages,
    )
    state = agent.run()
    print(f"Done: {patient_id}")
    print(f"Draft: {output_dir / 'draft.md'}")
    print(f"Trace: {output_dir / 'trace.jsonl'}")
    print(f"Flags: {len(state.flags)}")


if __name__ == "__main__":
    main()
