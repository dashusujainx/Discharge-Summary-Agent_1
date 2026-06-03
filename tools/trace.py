from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime


class TraceLogger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Clear file on init
        self.path.write_text("", encoding="utf-8")

    def log(
        self,
        step: int,
        reasoning: str,
        action: str,
        inputs: dict | None = None,
        result: dict | None = None,
        next_decision: str = "",
    ) -> None:
        event = {
            "timestamp": datetime.utcnow().isoformat(),
            "step": step,
            "reasoning": reasoning,
            "action": action,
            "inputs": inputs or {},
            "result": result or {},
            "next_decision": next_decision,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

        # Also print to console for visibility
        print(f"\n[TRACE Step {step}] {action}")
        print(f"  Reasoning : {reasoning}")
        if result:
            # Print a short summary of the result
            result_str = str(result)
            print(f"  Result    : {result_str[:200]}{'...' if len(result_str) > 200 else ''}")
        if next_decision:
            print(f"  Next      : {next_decision}")