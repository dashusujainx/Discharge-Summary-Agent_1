from __future__ import annotations

import argparse
import csv
import json
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from models.schemas import DischargeSummary


STRATEGIES = ["baseline", "explicit_missing", "safety_first"]


def normalized_edit_distance(a: str, b: str) -> float:
    if not a and not b:
        return 0.0
    return 1.0 - SequenceMatcher(None, a, b).ratio()


def simulated_doctor_edit(draft: str) -> str:
    edited = draft.replace(
        "MISSING - clinician review required.",
        "Not documented in source notes - clinician review required.",
    )
    edited = edited.replace(
        "**Status:** Draft for clinician review. Do not use as a finalized clinical document.",
        "**Status:** Draft only; clinician must verify all source-supported content before finalization.",
    )
    edited = edited.replace(
        "- MISSING or no source-supported changes found.",
        "- Not documented in source notes - medication reconciliation required.",
    )
    return edited


def render_strategy(summary: DischargeSummary, patient_id: str, strategy: str) -> str:
    missing = "MISSING - clinician review required."
    status = "**Status:** Draft for clinician review. Do not use as a finalized clinical document."
    no_changes = "- MISSING or no source-supported changes found."

    if strategy in {"explicit_missing", "safety_first"}:
        missing = "Not documented in source notes - clinician review required."
        no_changes = "- Not documented in source notes - medication reconciliation required."
    if strategy == "safety_first":
        status = "**Status:** Draft only; clinician must verify all source-supported content before finalization."

    def fact(value: str, status_value: str) -> str:
        return missing if status_value == "missing" or value == "MISSING" else value

    lines = [
        f"# Discharge Summary Draft: {patient_id}",
        "",
        status,
        "",
        "## Required Sections",
        f"- Patient demographics: {fact(summary.patient_demographics.value, summary.patient_demographics.status)}",
        f"- Admission date: {fact(summary.admission_date.value, summary.admission_date.status)}",
        f"- Discharge date: {fact(summary.discharge_date.value, summary.discharge_date.status)}",
        f"- Principal diagnosis: {fact(summary.principal_diagnosis.value, summary.principal_diagnosis.status)}",
        f"- Hospital course: {fact(summary.hospital_course.value, summary.hospital_course.status)}",
        f"- Discharge condition: {fact(summary.discharge_condition.value, summary.discharge_condition.status)}",
        "",
        "## Medication Changes",
    ]
    if summary.medication_changes:
        lines.extend(
            f"- {change.change_type.upper()}: {change.medication}. {change.details} Reason: {change.reason or missing}"
            for change in summary.medication_changes
        )
    else:
        lines.append(no_changes)
    return "\n".join(lines) + "\n"


def run_bandit(summary_paths: list[Path], iterations: int, output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    totals = {strategy: 0.0 for strategy in STRATEGIES}
    counts = {strategy: 0 for strategy in STRATEGIES}

    summaries = [
        (path.parent.name, DischargeSummary.model_validate_json(path.read_text(encoding="utf-8")))
        for path in summary_paths
    ]

    for i in range(iterations):
        patient_id, summary = summaries[i % len(summaries)]
        if i < len(STRATEGIES):
            strategy = STRATEGIES[i]
        else:
            strategy = min(STRATEGIES, key=lambda item: totals[item] / max(counts[item], 1))

        draft = render_strategy(summary, patient_id, strategy)
        edited = simulated_doctor_edit(draft)
        distance = normalized_edit_distance(draft, edited)
        totals[strategy] += distance
        counts[strategy] += 1
        rows.append({"iteration": i + 1, "patient_id": patient_id, "strategy": strategy, "edit_distance": distance})

    averages = {
        strategy: (totals[strategy] / counts[strategy] if counts[strategy] else None)
        for strategy in STRATEGIES
    }
    best_strategy = min(
        (k for k, v in averages.items() if v is not None),
        key=lambda k: averages[k] if averages[k] is not None else 1.0,
    )
    baseline = averages["baseline"] or 0.0
    best = averages[best_strategy] or 0.0
    result = {
        "reward": "1 - normalized edit distance",
        "baseline_average_edit_distance": baseline,
        "best_strategy": best_strategy,
        "best_average_edit_distance": best,
        "relative_improvement": ((baseline - best) / baseline) if baseline else 0.0,
        "strategy_averages": averages,
    }

    with (output_dir / "learning_curve.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["iteration", "patient_id", "strategy", "edit_distance"])
        writer.writeheader()
        writer.writerows(rows)
    (output_dir / "part2_metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run simulated reviewer learning for Part 2.")
    parser.add_argument("--summaries", nargs="+", default=["outputs/patient_2/summary.json"])
    parser.add_argument("--iterations", type=int, default=12)
    parser.add_argument("--out", default="outputs/part2")
    args = parser.parse_args()

    paths = [Path(path) for path in args.summaries if Path(path).exists()]
    if not paths:
        raise SystemExit("No summary.json files found. Run the discharge agent first.")
    result = run_bandit(paths, args.iterations, Path(args.out))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
