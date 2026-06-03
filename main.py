from __future__ import annotations
import argparse
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from agents.discharge_agent import DischargeSummaryAgent


def _collect_pdfs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    return sorted(input_path.glob("*.pdf"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the discharge-summary agent on patient PDFs."
    )
    parser.add_argument("--input", required=True,
                        help="A patient PDF or a folder containing patient PDFs.")
    parser.add_argument("--patient-id", default=None,
                        help="Identifier used in output paths.")
    parser.add_argument("--out", default="outputs",
                        help="Output directory.")
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--model", default="llama-3.1-8b-instant")
    parser.add_argument("--no-llm", action="store_true",
                        help="Disable Groq LLM (use local fallback).")
    parser.add_argument("--no-ocr", action="store_true",
                        help="Disable OCR fallback for image-only PDFs.")
    parser.add_argument("--ocr-provider",
                        choices=["auto", "groq", "local", "none"],
                        default="auto")
    parser.add_argument("--vision-model",
                        default="meta-llama/llama-4-scout-17b-16e-instruct")
    parser.add_argument("--ocr-dpi", type=int, default=150)
    parser.add_argument("--max-ocr-pages", type=int, default=None)

    # Part 2 options
    parser.add_argument("--run-part2", action="store_true",
                        help="Run Part 2 bandit learning after generating summary.")
    parser.add_argument("--part2-iterations", type=int, default=30,
                        help="Number of bandit iterations for Part 2.")

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

    print(f"\n✓ Done          : {patient_id}")
    print(f"  Draft         : {output_dir / 'draft.md'}")
    print(f"  Summary JSON  : {output_dir / 'summary.json'}")
    print(f"  Trace         : {output_dir / 'trace.jsonl'}")
    print(f"  Total flags   : {len(state.flags)}")
    critical = [f for f in state.flags if f.severity == "critical"]
    if critical:
        print(f"  🔴 CRITICAL    : {len(critical)} flags need immediate review!")

    # Part 2 — optional bandit learning
    if args.run_part2:
        from learning.simulated_learning import run_bandit
        summary_path = output_dir / "summary.json"
        if summary_path.exists():
            metrics = run_bandit(
                summary_paths=[summary_path],
                patient_ids=[patient_id],
                iterations=args.part2_iterations,
                output_dir=Path(args.out) / "part2_results",
            )
            print(f"\n  Part 2 best strategy : {metrics.get('best_strategy', 'N/A')}")
            print(f"  Improvement          : {metrics.get('improvement', 0):+.4f}")
        else:
            print("[Part2] summary.json not found — skipping bandit.")


if __name__ == "__main__":
    main()