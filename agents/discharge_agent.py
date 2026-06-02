from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq
from tenacity import retry, stop_after_attempt, wait_fixed

from models.schemas import (
    AgentAction,
    AgentState,
    DischargeSummary,
    Fact,
    Medication,
    MedicationChange,
    SafetyFlag,
    SourceRef,
)
from tools.pdf_reader import extract_pdf_pages
from tools.safety import detect_conflicting_facts, mock_drug_interaction_lookup
from tools.trace import TraceLogger


REQUIRED_FACTS = [
    "patient_demographics",
    "admission_date",
    "discharge_date",
    "principal_diagnosis",
    "hospital_course",
    "discharge_condition",
]


class DischargeSummaryAgent:
    def __init__(
        self,
        *,
        patient_id: str,
        input_paths: list[Path],
        output_dir: Path,
        max_steps: int = 8,
        model: str = "llama-3.1-8b-instant",
        use_llm: bool = True,
        enable_ocr: bool = True,
        ocr_provider: str = "auto",
        vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct",
        ocr_dpi: int = 150,
        max_ocr_pages: int | None = None,
    ) -> None:
        load_dotenv()
        self.state = AgentState(patient_id=patient_id, input_paths=input_paths)
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_steps = max_steps
        self.model = model
        self.enable_ocr = enable_ocr
        self.ocr_provider = ocr_provider
        self.vision_model = os.getenv("GROQ_VISION_MODEL", vision_model)
        self.ocr_dpi = ocr_dpi
        self.max_ocr_pages = max_ocr_pages
        self.trace = TraceLogger(output_dir / "trace.jsonl")
        self.groq = Groq(api_key=os.getenv("GROQ_API_KEY")) if use_llm and os.getenv("GROQ_API_KEY") else None

    def run(self) -> AgentState:
        while not self.state.done and self.state.steps_taken < self.max_steps:
            action = self._decide_next_action()
            self.state.steps_taken += 1
            if action == AgentAction.READ_PDFS:
                self._read_pdfs()
            elif action == AgentAction.EXTRACT_FACTS:
                self._extract_facts()
            elif action == AgentAction.RECONCILE_MEDICATIONS:
                self._reconcile_medications()
            elif action == AgentAction.CHECK_SAFETY:
                self._check_safety()
            elif action == AgentAction.VALIDATE:
                self._validate()
            elif action == AgentAction.WRITE_OUTPUTS:
                self._write_outputs()
            else:
                self.state.done = True

        if not self.state.done:
            self._add_flag("control", "Agent stopped at hard step cap before completion.")
            self._write_outputs()
        return self.state

    def _decide_next_action(self) -> AgentAction:
        if not self.state.pages:
            return AgentAction.READ_PDFS
        if self.state.extraction_failed:
            return AgentAction.WRITE_OUTPUTS if self.state.validation_done else AgentAction.VALIDATE
        if not self.state.extraction_done:
            return AgentAction.EXTRACT_FACTS
        if not self.state.summary.medication_changes and (
            self.state.summary.admission_medications or self.state.summary.discharge_medications
        ):
            return AgentAction.RECONCILE_MEDICATIONS
        if not self.state.safety_checked:
            return AgentAction.CHECK_SAFETY
        if not self.state.validation_done:
            return AgentAction.VALIDATE
        return AgentAction.WRITE_OUTPUTS

    def _read_pdfs(self) -> None:
        all_pages = []
        errors = []
        for path in self.state.input_paths:
            try:
                pages = self._read_pdf_with_retry(path)
                all_pages.extend(pages)
            except Exception as exc:
                errors.append({"file": str(path), "error": str(exc)})

        self.state.pages = all_pages
        readable = [page for page in all_pages if page.text.strip()]
        unreadable = [page for page in all_pages if not page.text.strip()]
        self.state.extraction_failed = not readable
        if self.state.extraction_failed:
            self._add_flag(
                "pdf_ingestion",
                "No readable text could be extracted. The draft must remain mostly missing until OCR/text extraction succeeds.",
            )
        elif unreadable:
            self._add_flag(
                "partial_pdf_ingestion",
                f"{len(unreadable)} page(s) could not be read and were excluded from extraction.",
            )

        self.trace.log(
            step=self.state.steps_taken,
            reasoning="The agent needs source evidence before drafting any clinical content.",
            action=AgentAction.READ_PDFS.value,
            inputs={
                "files": [str(path) for path in self.state.input_paths],
                "enable_ocr": self.enable_ocr,
                "ocr_provider": self.ocr_provider,
                "vision_model": self.vision_model if self.ocr_provider in {"auto", "groq"} else None,
                "max_ocr_pages": self.max_ocr_pages,
            },
            result={
                "pages": len(all_pages),
                "readable_pages": len(readable),
                "unreadable_pages": len(unreadable),
                "errors": errors,
            },
            next_decision="Extract facts if text exists; otherwise validate and flag missing fields.",
        )

    @retry(stop=stop_after_attempt(2), wait=wait_fixed(1))
    def _read_pdf_with_retry(self, path: Path):
        return extract_pdf_pages(
            path,
            enable_ocr=self.enable_ocr,
            ocr_provider=self.ocr_provider,
            groq_vision_model=self.vision_model,
            dpi=self.ocr_dpi,
            max_ocr_pages=self.max_ocr_pages,
            cache_dir=self.output_dir / "ocr_cache",
        )

    def _extract_facts(self) -> None:
        text = self._source_text(limit_chars=28000)
        if self.groq:
            try:
                self.state.summary = self._llm_extract(text)
                mode = "groq"
            except Exception as exc:
                self._add_flag("llm_failure", f"Groq extraction failed, used conservative local extraction instead: {exc}")
                self.state.summary = self._heuristic_extract(text)
                mode = "heuristic_after_llm_failure"
        else:
            self.state.summary = self._heuristic_extract(text)
            mode = "heuristic_no_api_key"

        self.state.extraction_done = True
        self.trace.log(
            step=self.state.steps_taken,
            reasoning="The agent extracts only source-supported fields and leaves unsupported fields missing.",
            action=AgentAction.EXTRACT_FACTS.value,
            inputs={"mode": mode, "characters": len(text)},
            result={"principal_diagnosis": self.state.summary.principal_diagnosis.value},
            next_decision="Reconcile medications if medication lists were found; otherwise safety check and validation.",
        )

    def _source_text(self, limit_chars: int) -> str:
        chunks = []
        for page in self.state.pages:
            if page.text.strip():
                chunks.append(f"[{Path(page.file).name} p.{page.page}]\n{page.text.strip()}")
        return "\n\n".join(chunks)[:limit_chars]

    def _llm_extract(self, text: str) -> DischargeSummary:
        if self.groq is None:
            raise RuntimeError("Groq client is not configured.")
        client = self.groq

        prompt = (
            "Extract a discharge-summary draft from the source notes. Never invent facts. "
            "Every non-missing fact must include a short source quote and page. Mark unknown fields as MISSING. "
            "Return JSON only matching this shape: "
            + json.dumps(DischargeSummary().model_dump(), ensure_ascii=False)
            + "\n\nSOURCE NOTES:\n"
            + text
        )
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a cautious clinical documentation extraction agent."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        return DischargeSummary.model_validate_json(content)

    def _heuristic_extract(self, text: str) -> DischargeSummary:
        summary = DischargeSummary()
        summary.patient_demographics = self._demographics_fact(text)
        summary.admission_date = self._value_after_label(text, "Admission Date")
        summary.discharge_date = self._value_after_label(text, "Discharge Date")
        summary.principal_diagnosis = self._value_after_label(text, "Principal Diagnosis")
        summary.secondary_diagnoses = self._split_fact_list(self._value_after_label(text, "Secondary Diagnoses"))
        summary.hospital_course = self._section_fact(text, ["hospital course", "brief hospital course"])
        summary.procedures = self._procedure_facts(text)
        summary.discharge_condition = self._value_after_label(text, "Discharge Condition")
        summary.allergies = self._split_fact_list(self._value_after_label(text, "Allergies"))
        summary.follow_up_instructions = self._split_fact_list(self._section_by_header(text, "Follow-up Instructions"))
        summary.pending_results = self._split_fact_list(self._section_by_header(text, "Pending Results"))
        summary.admission_medications = self._extract_medications(text, "admission")
        summary.discharge_medications = self._extract_medications(text, "discharge")
        return summary

    def _demographics_fact(self, text: str) -> Fact:
        pieces = []
        for label in ["Patient Name", "DOB", "MRN"]:
            fact = self._value_after_label(text, label)
            if fact.status != "missing":
                pieces.append(f"{label}: {fact.value}")
        if pieces:
            value = "; ".join(pieces)
            return Fact(value=value, status="sourced", sources=[self._source_for_quote(pieces[0])])
        return Fact()

    def _value_after_label(self, text: str, label: str) -> Fact:
        pattern = rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$"
        match = re.search(pattern, text)
        if not match:
            return Fact()
        value = match.group(1).strip()
        return Fact(value=value, status="sourced", sources=[self._source_for_quote(value)])

    def _split_fact_list(self, fact: Fact) -> list[Fact]:
        if fact.status == "missing":
            return []
        parts = [part.strip(" .") for part in re.split(r";|\n|\u2022", fact.value) if part.strip(" .")]
        return [Fact(value=part, status="sourced", sources=fact.sources) for part in parts]

    def _section_fact(self, text: str, labels: list[str]) -> Fact:
        for label in labels:
            fact = self._section_by_header(text, label)
            if fact.status != "missing":
                value = " ".join(fact.value.split())[:700]
                return Fact(value=value, status="sourced", sources=[self._source_for_quote(value[:80])])
        return Fact()

    def _section_by_header(self, text: str, label: str) -> Fact:
        headers = [
            "Patient Name",
            "DOB",
            "MRN",
            "Admission Date",
            "Discharge Date",
            "Principal Diagnosis",
            "Secondary Diagnoses",
            "Allergies",
            "Admission Medications",
            "Hospital Course",
            "Discharge Medications",
            "Follow-up Instructions",
            "Pending Results",
            "Discharge Condition",
        ]
        header_pattern = "|".join(re.escape(h) for h in headers if h.lower() != label.lower())
        pattern = rf"(?is){re.escape(label)}\s*:\s*(.*?)(?=\n\s*(?:{header_pattern})\s*:|\Z)"
        match = re.search(pattern, text)
        if not match:
            return Fact()
        value = match.group(1).strip()
        return Fact(value=value, status="sourced", sources=[self._source_for_quote(value[:80])])

    def _procedure_facts(self, text: str) -> list[Fact]:
        if re.search(r"no procedures? (were )?performed", text, re.IGNORECASE):
            value = "No procedures were performed."
            return [Fact(value=value, status="sourced", sources=[self._source_for_quote(value)])]
        fact = self._section_by_header(text, "Procedures")
        return [] if fact.status == "missing" else [fact]

    def _extract_medications(self, text: str, context: str) -> list[Medication]:
        meds: list[Medication] = []
        block_fact = self._section_by_header(text, f"{context.title()} Medications")
        if block_fact.status == "missing":
            return meds
        block = block_fact.value
        for raw in re.split(r"\n|;|•|- ", block):
            line = raw.strip(" .:-")
            if not line or len(line) < 3:
                continue
            if re.search(r"\b(mg|mcg|unit|tab|tablet|cap|daily|bid|tid|qhs|po|iv)\b", line, re.IGNORECASE):
                meds.append(Medication(name=line[:120], status=context, sources=[self._source_for_quote(line[:80])]))
        return meds[:25]

    def _source_for_quote(self, quote: str) -> SourceRef:
        for page in self.state.pages:
            if quote and quote[:30].lower() in page.text.lower():
                return SourceRef(file=Path(page.file).name, page=page.page, quote=quote[:180])
        return SourceRef(file="unknown", page=None, quote=quote[:180] if quote else None)

    def _reconcile_medications(self) -> None:
        admission = {self._med_key(med): med for med in self.state.summary.admission_medications}
        discharge = {self._med_key(med): med for med in self.state.summary.discharge_medications}
        changes: list[MedicationChange] = []

        for key, med in discharge.items():
            if key not in admission:
                changes.append(
                    MedicationChange(
                        medication=med.name,
                        change_type="added",
                        details="Present on discharge list but not admission list.",
                        reason=med.reason,
                        needs_reconciliation=not med.reason,
                        sources=med.sources,
                    )
                )
        for key, med in admission.items():
            if key not in discharge:
                changes.append(
                    MedicationChange(
                        medication=med.name,
                        change_type="stopped",
                        details="Present on admission list but not discharge list.",
                        reason=med.reason,
                        needs_reconciliation=not med.reason,
                        sources=med.sources,
                    )
                )

        self.state.summary.medication_changes = changes
        for change in changes:
            if change.needs_reconciliation:
                self._add_flag("medication_reconciliation", f"{change.medication}: {change.change_type} without documented reason.")

        self.trace.log(
            step=self.state.steps_taken,
            reasoning="Medication changes need explicit review when the source notes do not document a reason.",
            action=AgentAction.RECONCILE_MEDICATIONS.value,
            inputs={"admission_count": len(admission), "discharge_count": len(discharge)},
            result={"changes": [change.model_dump() for change in changes]},
            next_decision="Run mocked safety tools for discharge medications.",
        )

    def _med_key(self, med: Medication) -> str:
        return re.sub(r"[^a-z0-9]+", " ", med.name.lower()).strip().split(" ")[0]

    def _check_safety(self) -> None:
        flags = mock_drug_interaction_lookup(self.state.summary.discharge_medications)
        self.state.flags.extend(flags)
        self.state.summary.safety_flags = self.state.flags
        self.state.safety_checked = True
        self.trace.log(
            step=self.state.steps_taken,
            reasoning="The agent calls an external-style safety tool only after it has a discharge medication list.",
            action=AgentAction.CHECK_SAFETY.value,
            inputs={"medications": [med.name for med in self.state.summary.discharge_medications]},
            result={"flags": [flag.model_dump() for flag in flags]},
            next_decision="Validate required fields and conflicts.",
        )

    def _validate(self) -> None:
        for field in REQUIRED_FACTS:
            fact = getattr(self.state.summary, field)
            if fact.status == "missing" or fact.value == "MISSING":
                self._add_flag("missing_required", f"{field.replace('_', ' ')} is missing and must be completed by clinician review.")

        diagnosis_values = self._source_diagnosis_values()
        conflicts = detect_conflicting_facts("principal/discharge diagnoses", diagnosis_values)
        self.state.flags.extend(conflicts)
        self.state.summary.conflicts = conflicts
        self.state.summary.safety_flags = self.state.flags
        self.state.validation_done = True

        self.trace.log(
            step=self.state.steps_taken,
            reasoning="Before writing the draft, the agent checks for missing required fields and contradictions.",
            action=AgentAction.VALIDATE.value,
            result={"flag_count": len(self.state.flags), "conflict_count": len(conflicts)},
            next_decision="Write draft and trace artifacts.",
        )

    def _source_diagnosis_values(self) -> list[str]:
        text = self._source_text(limit_chars=40000)
        values = []
        for match in re.finditer(
            r"(?im)^\s*(?:principal diagnosis|primary diagnosis|discharge diagnosis)\s*:\s*(.+?)\s*$",
            text,
        ):
            value = match.group(1).strip()
            if value:
                values.append(value)
        return values

    def _write_outputs(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "summary.json").write_text(
            self.state.summary.model_dump_json(indent=2),
            encoding="utf-8",
        )
        (self.output_dir / "draft.md").write_text(self._render_markdown(), encoding="utf-8")
        self.trace.log(
            step=self.state.steps_taken,
            reasoning="The agent writes a draft for clinician review, not a final clinical document.",
            action=AgentAction.WRITE_OUTPUTS.value,
            result={"draft": str(self.output_dir / "draft.md"), "summary_json": str(self.output_dir / "summary.json")},
            next_decision="Stop.",
        )
        self.state.done = True

    def _render_markdown(self) -> str:
        s = self.state.summary
        lines = [
            f"# Discharge Summary Draft: {self.state.patient_id}",
            "",
            "**Status:** Draft for clinician review. Do not use as a finalized clinical document.",
            "",
            "## Required Sections",
            f"- Patient demographics: {self._fmt_fact(s.patient_demographics)}",
            f"- Admission date: {self._fmt_fact(s.admission_date)}",
            f"- Discharge date: {self._fmt_fact(s.discharge_date)}",
            f"- Principal diagnosis: {self._fmt_fact(s.principal_diagnosis)}",
            f"- Secondary diagnoses: {self._fmt_facts(s.secondary_diagnoses)}",
            f"- Hospital course: {self._fmt_fact(s.hospital_course)}",
            f"- Procedures: {self._fmt_facts(s.procedures)}",
            f"- Allergies: {self._fmt_facts(s.allergies)}",
            f"- Follow-up instructions: {self._fmt_facts(s.follow_up_instructions)}",
            f"- Pending results: {self._fmt_facts(s.pending_results)}",
            f"- Discharge condition: {self._fmt_fact(s.discharge_condition)}",
            "",
            "## Discharge Medications",
        ]
        lines.extend(self._fmt_meds(s.discharge_medications))
        lines.extend(["", "## Medication Changes"])
        if s.medication_changes:
            lines.extend(f"- {c.change_type.upper()}: {c.medication}. {c.details} Reason: {c.reason or 'MISSING - reconcile.'}" for c in s.medication_changes)
        else:
            lines.append("- MISSING or no source-supported changes found.")
        lines.extend(["", "## Safety / Review Flags"])
        if self.state.flags:
            lines.extend(f"- [{flag.category}] {flag.message}" for flag in self.state.flags)
        else:
            lines.append("- No flags generated by available tools.")
        return "\n".join(lines) + "\n"

    def _fmt_fact(self, fact: Fact) -> str:
        if fact.status == "missing" or fact.value == "MISSING":
            return "MISSING - clinician review required."
        source = fact.sources[0] if fact.sources else None
        suffix = f" (source: {source.file} p.{source.page})" if source else ""
        return f"{fact.value}{suffix}"

    def _fmt_facts(self, facts: list[Fact]) -> str:
        if not facts:
            return "MISSING - clinician review required."
        return "; ".join(self._fmt_fact(fact) for fact in facts)

    def _fmt_meds(self, meds: list[Medication]) -> list[str]:
        if not meds:
            return ["- MISSING - clinician review required."]
        return [f"- {med.name}" for med in meds]

    def _add_flag(self, category: str, message: str) -> None:
        if any(flag.category == category and flag.message == message for flag in self.state.flags):
            return
        flag = SafetyFlag(category=category, severity="clinician_review", message=message)
        self.state.flags.append(flag)
        self.state.summary.safety_flags = self.state.flags
