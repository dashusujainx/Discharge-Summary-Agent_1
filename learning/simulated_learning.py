from __future__ import annotations
"""
Part 2 — Simulated Learning from Doctor Edits
Multi-armed bandit over prompt/template strategies,
rewarded by reduced edit distance between agent draft and "doctor-corrected" version.
"""
import csv
import json
import random
from pathlib import Path

# ------------------------------------------------------------------ #
# Strategies                                                           #
# ------------------------------------------------------------------ #

STRATEGIES = ["baseline", "explicit_missing", "safety_first"]


def render_strategy(summary_json_path: Path, patient_id: str, strategy: str) -> str:
    """Re-render a draft from summary.json using the chosen strategy."""
    data = json.loads(summary_json_path.read_text(encoding="utf-8"))

    def val(field: str) -> str:
        f = data.get(field, {})
        if isinstance(f, dict):
            v = f.get("value", "MISSING - clinician review required")
            return v or "MISSING - clinician review required"
        return str(f) if f else "MISSING - clinician review required"

    missing_label = {
        "baseline": "MISSING - clinician review required",
        "explicit_missing": "NOT DOCUMENTED in source notes — clinician must supply",
        "safety_first": "⛔ CRITICAL MISSING — do not finalise without this value",
    }[strategy]

    def safe_val(field: str) -> str:
        v = val(field)
        if v == "MISSING - clinician review required":
            return missing_label
        return v

    meds = data.get("discharge_medications", [])
    med_lines = "\n".join(
        f"  - {m.get('name','')} {m.get('dose','')} {m.get('route','')} {m.get('frequency','')}".strip()
        for m in meds
    ) or "  None documented"

    pending = data.get("pending_results", [])
    pending_lines = "\n".join(f"  - {r}" for r in pending) or "  None documented"

    header = {
        "baseline": "DISCHARGE SUMMARY DRAFT",
        "explicit_missing": "DISCHARGE SUMMARY DRAFT (Explicit Missing Fields)",
        "safety_first": "⚠️ DISCHARGE SUMMARY DRAFT — SAFETY FIRST REVIEW",
    }[strategy]

    return f"""# {header}
## Patient: {patient_id}

## PATIENT DEMOGRAPHICS
{safe_val('patient_demographics')}

## ADMISSION DATE
{safe_val('admission_date')}

## DISCHARGE DATE
{safe_val('discharge_date')}

## PRINCIPAL DIAGNOSIS
{safe_val('principal_diagnosis')}

## HOSPITAL COURSE
{safe_val('hospital_course')}

## DISCHARGE CONDITION
{safe_val('discharge_condition')}

## DISCHARGE MEDICATIONS
{med_lines}

## FOLLOW-UP
{safe_val('follow_up_instructions')}

## PENDING RESULTS
{pending_lines}
"""


# ------------------------------------------------------------------ #
# Simulated doctor editor                                              #
# ------------------------------------------------------------------ #

def simulated_doctor_edit(draft: str) -> str:
    """
    Apply a consistent (hidden-to-the-agent) editing policy.
    Represents what a real doctor would fix in the draft.
    """
    edited = draft

    # Replace vague MISSING markers with clearer clinical language
    replacements = [
        ("MISSING - clinician review required", "To be confirmed by treating physician"),
        ("NOT DOCUMENTED in source notes — clinician must supply", "Not found in records — verify with team"),
        ("⛔ CRITICAL MISSING — do not finalise without this value", "Pending — must be completed before discharge"),
        ("None documented", "Nil known"),
        ("No changes detected", "No medication changes identified"),
    ]
    for old, new in replacements:
        edited = edited.replace(old, new)

    # Doctor always adds a sign-off line
    if "Reviewed by:" not in edited:
        edited += "\n\n---\n*Reviewed by: [Clinician signature required]*\n"

    return edited


# ------------------------------------------------------------------ #
# Edit-distance metric                                                 #
# ------------------------------------------------------------------ #

def edit_distance_score(original: str, edited: str) -> float:
    """
    Normalised edit distance: 0.0 = identical (no edits), 1.0 = completely different.
    Lower = better (less editing needed = higher reward).
    Uses character-level Levenshtein approximation via difflib.
    """
    import difflib
    ratio = difflib.SequenceMatcher(None, original, edited).ratio()
    return 1.0 - ratio   # convert similarity to distance


# ------------------------------------------------------------------ #
# Multi-armed bandit                                                   #
# ------------------------------------------------------------------ #

class EpsilonGreedyBandit:
    """Simple epsilon-greedy bandit over strategies."""

    def __init__(self, strategies: list[str], epsilon: float = 0.2):
        self.strategies = strategies
        self.epsilon = epsilon
        self.counts = {s: 0 for s in strategies}
        self.rewards = {s: 0.0 for s in strategies}

    def select(self) -> str:
        if random.random() < self.epsilon:
            return random.choice(self.strategies)
        # Exploit: pick strategy with highest average reward
        avg = {s: (self.rewards[s] / self.counts[s] if self.counts[s] > 0 else 0.0)
               for s in self.strategies}
        return max(avg, key=avg.__getitem__)

    def update(self, strategy: str, reward: float):
        self.counts[strategy] += 1
        self.rewards[strategy] += reward

    def best_strategy(self) -> str:
        avg = {s: (self.rewards[s] / self.counts[s] if self.counts[s] > 0 else 0.0)
               for s in self.strategies}
        return max(avg, key=avg.__getitem__)

    def stats(self) -> dict:
        return {
            s: {
                "count": self.counts[s],
                "avg_reward": round(self.rewards[s] / self.counts[s], 4) if self.counts[s] > 0 else 0.0,
            }
            for s in self.strategies
        }


# ------------------------------------------------------------------ #
# Main runner                                                          #
# ------------------------------------------------------------------ #

def run_bandit(
    summary_paths: list[Path],
    iterations: int = 30,
    output_dir: Path = Path("outputs/part2"),
    patient_ids: list[str] | None = None,
) -> dict:
    """
    Run the multi-armed bandit learning loop.

    For each iteration:
      1. Pick a strategy (epsilon-greedy)
      2. Render a draft using that strategy
      3. Apply simulated doctor edits
      4. Compute edit distance → reward = 1 - edit_distance
      5. Update bandit
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    bandit = EpsilonGreedyBandit(STRATEGIES, epsilon=0.25)

    if not summary_paths:
        print("[Part2] No summary.json files provided — skipping bandit run.")
        return {}

    if patient_ids is None:
        patient_ids = [p.parent.name for p in summary_paths]

    curve_rows = []

    print(f"\n{'='*50}")
    print("  Part 2 — Bandit Learning Loop")
    print(f"  Iterations: {iterations} | Strategies: {STRATEGIES}")
    print(f"{'='*50}")

    for i in range(iterations):
        # Pick patient + strategy
        idx = i % len(summary_paths)
        summary_path = summary_paths[idx]
        patient_id = patient_ids[idx]
        strategy = bandit.select()

        # Render with chosen strategy
        try:
            draft = render_strategy(summary_path, patient_id, strategy)
        except Exception as exc:
            print(f"  [WARN] iter {i+1}: render failed ({exc}) — skipping")
            continue

        # Simulated doctor edits
        edited = simulated_doctor_edit(draft)

        # Score: edit distance (lower = better draft → higher reward)
        dist = edit_distance_score(draft, edited)
        reward = 1.0 - dist   # reward = similarity (higher = less editing needed)

        bandit.update(strategy, reward)

        curve_rows.append({
            "iteration": i + 1,
            "strategy": strategy,
            "edit_distance": round(dist, 4),
            "reward": round(reward, 4),
        })

        if (i + 1) % 5 == 0:
            print(f"  Iter {i+1:3d} | strategy={strategy:20s} | dist={dist:.4f} | reward={reward:.4f}")

    best = bandit.best_strategy()
    stats = bandit.stats()

    print(f"\n  Best strategy: {best}")
    for s, st in stats.items():
        print(f"    {s:20s} count={st['count']:3d}  avg_reward={st['avg_reward']:.4f}")

    # Save CSV
    csv_path = output_dir / "learning_curve.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["iteration", "strategy", "edit_distance", "reward"])
        writer.writeheader()
        writer.writerows(curve_rows)

    # Save metrics JSON
    metrics = {
        "iterations": iterations,
        "best_strategy": best,
        "strategy_stats": stats,
        "first_10_avg_dist": round(
            sum(r["edit_distance"] for r in curve_rows[:10]) / max(len(curve_rows[:10]), 1), 4
        ),
        "last_10_avg_dist": round(
            sum(r["edit_distance"] for r in curve_rows[-10:]) / max(len(curve_rows[-10:]), 1), 4
        ),
    }
    improvement = metrics["first_10_avg_dist"] - metrics["last_10_avg_dist"]
    metrics["improvement"] = round(improvement, 4)
    metrics["improved"] = improvement > 0

    metrics_path = output_dir / "part2_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    print(f"\n  Edit distance improvement: {improvement:+.4f}")
    print(f"  CSV  → {csv_path}")
    print(f"  JSON → {metrics_path}")

    return metrics