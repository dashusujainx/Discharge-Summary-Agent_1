from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from models.schemas import TraceEvent


class TraceLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def log(
        self,
        *,
        step: int,
        reasoning: str,
        action: str,
        inputs: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        next_decision: str,
    ) -> None:
        event = TraceEvent(
            step=step,
            reasoning=reasoning,
            action=action,
            inputs=inputs or {},
            result=result or {},
            next_decision=next_decision,
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.model_dump(), ensure_ascii=False) + "\n")
