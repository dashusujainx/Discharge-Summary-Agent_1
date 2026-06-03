from __future__ import annotations
import json
import logging
import os
import re
from pathlib import Path
import time
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from models.schemas import (
    AgentAction, AgentState, DischargeSummary, ExtractionPage,
    Fact, Medication, MedicationChange, SafetyFlag, SourceRef,
)
from tools.pdf_reader import extract_pdf_pages
from tools.safety import detect_conflicting_facts, mock_drug_interaction_lookup
from tools.trace import TraceLogger

logger = logging.getLogger(__name__)

REQUIRED_FACTS = [
    "patient_demographics",
    "admission_date",
    "discharge_date",
    "principal_diagnosis",
    "hospital_course",
    "discharge_condition",
]

MISSING = "MISSING - clinician review required"


class DischargeSummaryAgent:
    def __init__(
        self,
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
        max_ocr_pages: Optional[int] = None,
    ):
        self.patient_id = patient_id
        self.input_paths = input_paths
        self.output_dir = output_dir
        self.max_steps = max_steps
        self.model = model
        self.use_llm = use_llm
        self.enable_ocr = enable_ocr
        self.ocr_provider = ocr_provider
        self.vision_model = vision_model
        self.ocr_dpi = ocr_dpi
        self.max_ocr_pages = max_ocr_pages

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.trace = TraceLogger(self.output_dir / "trace.jsonl")
        self.state = AgentState()

        # Init Groq client
        self._groq = None
        if use_llm:
            api_key = os.getenv("GROQ_API_KEY")
            if api_key:
                try:
                    from groq import Groq
                    self._groq = Groq(api_key=api_key)
                    logger.info("Groq client initialised.")
                except Exception as exc:
                    logger.warning(f"Groq init failed: {exc}")
            else:
                logger.warning("GROQ_API_KEY not set — LLM disabled.")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> AgentState:
        print(f"\n{'='*60}")
        print(f"  Discharge Summary Agent — Patient: {self.patient_id}")
        print(f"  PDFs: {[p.name for p in self.input_paths]}")
        print(f"{'='*60}\n")

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
                self.state.done = True
            else:
                self.state.done = True

        if self.state.steps_taken >= self.max_steps and not self.state.done:
            self._add_flag("missing_field", f"Agent hit max step limit ({self.max_steps}). Output may be incomplete.")
            self._write_outputs()
            self.state.done = True

        print(f"\n{'='*60}")
        print(f"  DONE — {self.state.steps_taken} steps, {len(self.state.flags)} flags")
        print(f"  Output: {self.output_dir}")
        print(f"{'='*60}\n")
        return self.state

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------

    def _decide_next_action(self) -> AgentAction:
        s = self.state
        if not s.pdfs_read:
            reason = "PDFs not yet read — must extract text before anything else."
            action = AgentAction.READ_PDFS
        elif not s.extraction_done:
            reason = "Text extracted but clinical facts not yet parsed."
            action = AgentAction.EXTRACT_FACTS
        elif not s.reconciliation_done:
            reason = "Facts extracted — now reconcile admission vs discharge medications."
            action = AgentAction.RECONCILE_MEDICATIONS
        elif not s.safety_checked:
            reason = "Medications reconciled — run safety/conflict checks."
            action = AgentAction.CHECK_SAFETY
        elif not s.validation_done:
            reason = "Safety checked — validate all required fields are present."
            action = AgentAction.VALIDATE
        else:
            reason = "All steps complete — write final outputs."
            action = AgentAction.WRITE_OUTPUTS

        self.trace.log(
            step=s.steps_taken + 1,
            reasoning=reason,
            action=action.value,
            inputs={"step": s.steps_taken + 1},
            next_decision=action.value,
        )
        return action

    # ------------------------------------------------------------------
    # Step 1: Read PDFs
    # ------------------------------------------------------------------

    def _read_pdfs(self):
        cache_dir = self.output_dir / "ocr_cache"
        all_pages = []
        for pdf_path in self.input_paths:
            print(f"  Reading: {pdf_path.name}")
            try:
                pages = extract_pdf_pages(
                    pdf_path=pdf_path,
                    enable_ocr=self.enable_ocr,
                    ocr_provider=self.ocr_provider,
                    vision_model=self.vision_model,
                    ocr_dpi=self.ocr_dpi,
                    max_ocr_pages=self.max_ocr_pages,
                    cache_dir=cache_dir,
                )
                for p in pages:
                    all_pages.append(ExtractionPage(**p))
                print(f"    → {len(pages)} pages extracted from {pdf_path.name}")
            except Exception as exc:
                logger.error(f"Failed to read {pdf_path}: {exc}")
                self._add_flag("unreadable", f"Could not read {pdf_path.name}: {exc}")

        self.state.pages = all_pages
        self.state.pdfs_read = True

        ok = sum(1 for p in all_pages if p.status == "ok")
        empty = sum(1 for p in all_pages if p.status == "empty")
        self.trace.log(
            step=self.state.steps_taken,
            reasoning="PDF reading complete.",
            action=AgentAction.READ_PDFS.value,
            inputs={"files": [p.name for p in self.input_paths]},
            result={"total_pages": len(all_pages), "ok": ok, "empty": empty},
            next_decision=AgentAction.EXTRACT_FACTS.value,
        )

    # ------------------------------------------------------------------
    # Step 2: Extract Facts
    # ------------------------------------------------------------------

    def _extract_facts(self):
        # Build combined text
        text_parts = []
        for p in self.state.pages:
            if p.text.strip():
                text_parts.append(f"[FILE: {p.file} | PAGE: {p.page}]\n{p.text}")
        combined_text = "\n\n---\n\n".join(text_parts)

        if not combined_text.strip():
            self._add_flag("missing_field", "No text could be extracted from any PDF page. All facts will be MISSING.")
            self.state.extraction_done = True
            return

        summary = None
        if self._groq:
            print("  Waiting 30s for rate limit cooldown before LLM extraction...")
            time.sleep(30)
            summary = self._extract_with_groq(combined_text)

        if summary is None:
            print("  [WARN] LLM extraction failed or disabled — using local fallback.")
            summary = self._extract_locally(combined_text)

        self.state.summary = summary
        self.state.extraction_done = True

        self.trace.log(
            step=self.state.steps_taken,
            reasoning="Clinical fact extraction complete.",
            action=AgentAction.EXTRACT_FACTS.value,
            inputs={"text_length": len(combined_text)},
            result={
                "principal_diagnosis": summary.principal_diagnosis.value,
                "admission_date": summary.admission_date.value,
                "discharge_date": summary.discharge_date.value,
                "patient_demographics": summary.patient_demographics.value,
                "discharge_medications_count": len(summary.discharge_medications),
                "pending_results_count": len(summary.pending_results),
            },
            next_decision=AgentAction.RECONCILE_MEDICATIONS.value,
        )

    def _extract_with_groq(self, text: str) -> Optional[DischargeSummary]:
        """Call Groq LLM to extract structured clinical facts."""
        # Truncate to avoid token limits — take first 12000 chars
        text_trimmed = text[:4000] if len(text) > 4000 else text

        prompt = f"""You are a clinical documentation AI. Extract structured discharge summary data from the following patient medical records.

STRICT RULES:
1. NEVER invent or guess any clinical fact.
2. If a field cannot be found in the text, set its value to exactly: "MISSING - clinician review required" and status to "missing".
3. If a field has conflicting values in different notes, set status to "conflicting" and describe the conflict in notes.
4. If a result is still pending (awaiting lab/culture), set status to "pending".
5. Extract ONLY what is explicitly stated in the documents.

Return a single valid JSON object matching this exact schema:
{{
  "patient_demographics": {{"value": "...", "status": "ok|missing|conflicting|pending", "notes": null}},
  "admission_date": {{"value": "...", "status": "ok|missing"}},
  "discharge_date": {{"value": "...", "status": "ok|missing"}},
  "principal_diagnosis": {{"value": "...", "status": "ok|missing|conflicting", "notes": null}},
  "secondary_diagnoses": ["...", "..."],
  "hospital_course": {{"value": "...", "status": "ok|missing"}},
  "discharge_condition": {{"value": "...", "status": "ok|missing"}},
  "procedures": ["...", "..."],
  "allergies": {{"value": "...", "status": "ok|missing"}},
  "follow_up_instructions": {{"value": "...", "status": "ok|missing"}},
  "pending_results": ["...", "..."],
  "admission_medications": [
    {{"name": "...", "dose": "...", "route": "...", "frequency": "...", "status": "active"}}
  ],
  "discharge_medications": [
    {{"name": "...", "dose": "...", "route": "...", "frequency": "...", "status": "active"}}
  ]
}}

PATIENT MEDICAL RECORDS:
{text_trimmed}

Return ONLY the JSON object. No explanation, no markdown, no extra text."""

        from tenacity import retry, stop_after_attempt, wait_fixed

        @retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
        def _call():
            resp = self._groq.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=3000,
            )
            return resp.choices[0].message.content or ""

        try:
            raw = _call()
            # Strip markdown code fences if present
            raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
            data = json.loads(raw)
            return self._dict_to_summary(data)
        except Exception as exc:
            logger.error(f"Groq extraction failed after retries: {exc}")
            self._add_flag("missing_field", f"LLM extraction failed: {exc}. Falling back to local parser.")
            return None

    def _dict_to_summary(self, data: dict) -> DischargeSummary:
        """Convert raw LLM JSON dict into DischargeSummary, safely."""

        def to_fact(raw) -> Fact:
            if isinstance(raw, dict):
                return Fact(
                    value=raw.get("value", MISSING) or MISSING,
                    status=raw.get("status", "missing"),
                    notes=raw.get("notes"),
                )
            if isinstance(raw, str):
                return Fact(value=raw, status="ok" if raw and raw != MISSING else "missing")
            return Fact()

        def to_meds(raw_list) -> list[Medication]:
            meds = []
            if not isinstance(raw_list, list):
                return meds
            for m in raw_list:
                if isinstance(m, dict):
                    meds.append(Medication(
                        name=m.get("name", ""),
                        dose=m.get("dose", ""),
                        route=m.get("route", ""),
                        frequency=m.get("frequency", ""),
                        status=m.get("status", "active"),
                        reason=m.get("reason", ""),
                    ))
            return meds

        return DischargeSummary(
            patient_demographics=to_fact(data.get("patient_demographics")),
            admission_date=to_fact(data.get("admission_date")),
            discharge_date=to_fact(data.get("discharge_date")),
            principal_diagnosis=to_fact(data.get("principal_diagnosis")),
            secondary_diagnoses=data.get("secondary_diagnoses") or [],
            hospital_course=to_fact(data.get("hospital_course")),
            discharge_condition=to_fact(data.get("discharge_condition")),
            procedures=data.get("procedures") or [],
            allergies=to_fact(data.get("allergies")),
            follow_up_instructions=to_fact(data.get("follow_up_instructions")),
            pending_results=data.get("pending_results") or [],
            admission_medications=to_meds(data.get("admission_medications")),
            discharge_medications=to_meds(data.get("discharge_medications")),
        )

    def _extract_locally(self, text: str) -> DischargeSummary:
        """Conservative local regex-based fallback. Flags everything it cannot find."""
        summary = DischargeSummary()

        # Try to find diagnosis
        diag_match = re.search(
            r"(?:diagnosis|diagnos[ei]s|impression)[:\s]+([^\n]{5,120})",
            text, re.IGNORECASE
        )
        if diag_match:
            summary.principal_diagnosis = Fact(value=diag_match.group(1).strip(), status="ok")

        # Try to find dates
        date_match = re.findall(r"\b(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\b", text)
        if date_match:
            summary.admission_date = Fact(
                value=date_match[0],
                status="ok",
                notes="First date found in document — verify against admission note.",
            )
            if len(date_match) > 1:
                summary.discharge_date = Fact(
                    value=date_match[-1],
                    status="ok",
                    notes="Last date found in document — verify against discharge note.",
                )

        # Try allergies
        allergy_match = re.search(r"allerg[yi][^\n]*:?\s*([^\n]{3,80})", text, re.IGNORECASE)
        if allergy_match:
            summary.allergies = Fact(value=allergy_match.group(1).strip(), status="ok")

        # Flag that this is a local fallback
        self._add_flag(
            "missing_field",
            "Used local fallback extraction (LLM unavailable). Many fields may be MISSING — full clinician review required.",
        )
        return summary

    # ------------------------------------------------------------------
    # Step 3: Reconcile Medications
    # ------------------------------------------------------------------

    def _reconcile_medications(self):
        adm = {m.name.lower(): m for m in self.state.summary.admission_medications}
        dis = {m.name.lower(): m for m in self.state.summary.discharge_medications}
        changes: list[MedicationChange] = []

        # Drugs added at discharge
        for name, med in dis.items():
            if name not in adm:
                changes.append(MedicationChange(
                    medication=med.name,
                    change_type="ADDED",
                    details=f"{med.dose} {med.route} {med.frequency}".strip(),
                    reason=med.reason or MISSING,
                    needs_reconciliation=(not med.reason),
                ))

        # Drugs discontinued at discharge
        for name, med in adm.items():
            if name not in dis:
                changes.append(MedicationChange(
                    medication=med.name,
                    change_type="DISCONTINUED",
                    details=f"Was: {med.dose} {med.route} {med.frequency}".strip(),
                    reason=MISSING,
                    needs_reconciliation=True,
                ))

        # Drugs with changed dose/route
        for name in adm:
            if name in dis:
                a = adm[name]
                d = dis[name]
                diffs = []
                if a.dose and d.dose and a.dose != d.dose:
                    diffs.append(f"dose {a.dose}→{d.dose}")
                if a.route and d.route and a.route != d.route:
                    diffs.append(f"route {a.route}→{d.route}")
                if a.frequency and d.frequency and a.frequency != d.frequency:
                    diffs.append(f"frequency {a.frequency}→{d.frequency}")
                if diffs:
                    changes.append(MedicationChange(
                        medication=d.name,
                        change_type="CHANGED",
                        details="; ".join(diffs),
                        reason=d.reason or MISSING,
                        needs_reconciliation=(not d.reason),
                    ))

        self.state.summary.medication_changes = changes
        self.state.reconciliation_done = True

        recon_needed = sum(1 for c in changes if c.needs_reconciliation)
        if recon_needed:
            self._add_flag(
                "missing_field",
                f"{recon_needed} medication change(s) have no documented reason — clinician reconciliation required.",
            )

        self.trace.log(
            step=self.state.steps_taken,
            reasoning="Medication reconciliation complete.",
            action=AgentAction.RECONCILE_MEDICATIONS.value,
            inputs={"admission_meds": len(adm), "discharge_meds": len(dis)},
            result={"changes": len(changes), "needs_reconciliation": recon_needed},
            next_decision=AgentAction.CHECK_SAFETY.value,
        )

    # ------------------------------------------------------------------
    # Step 4: Safety checks
    # ------------------------------------------------------------------

    def _check_safety(self):
        conflict_flags = detect_conflicting_facts(self.state.summary)
        self.state.flags.extend(conflict_flags)

        med_names = [m.name for m in self.state.summary.discharge_medications]
        interaction_flags = mock_drug_interaction_lookup(med_names)
        self.state.flags.extend(interaction_flags)

        self.state.safety_checked = True

        self.trace.log(
            step=self.state.steps_taken,
            reasoning="Safety and conflict checks complete.",
            action=AgentAction.CHECK_SAFETY.value,
            inputs={"medications_checked": len(med_names)},
            result={
                "conflict_flags": len(conflict_flags),
                "interaction_flags": len(interaction_flags),
                "total_flags": len(self.state.flags),
            },
            next_decision=AgentAction.VALIDATE.value,
        )

    # ------------------------------------------------------------------
    # Step 5: Validate required fields
    # ------------------------------------------------------------------

    def _validate(self):
        summary = self.state.summary
        for field in REQUIRED_FACTS:
            fact: Fact = getattr(summary, field)
            if fact.status in ("missing",) or fact.value in ("", MISSING):
                self._add_flag(
                    "missing_field",
                    f"Required field '{field}' is MISSING — clinician must supply this value before finalising.",
                )

        if not summary.discharge_medications:
            self._add_flag("missing_field", "No discharge medications found — verify medication list with prescriber.")

        if not summary.allergies.value or summary.allergies.status == "missing":
            self._add_flag("missing_field", "Allergy status not documented — clinician must confirm before discharge.")

        self.state.validation_done = True
        self.state.summary.safety_flags = self.state.flags

        self.trace.log(
            step=self.state.steps_taken,
            reasoning="Validation of required fields complete.",
            action=AgentAction.VALIDATE.value,
            inputs={"required_fields": REQUIRED_FACTS},
            result={"total_flags": len(self.state.flags)},
            next_decision=AgentAction.WRITE_OUTPUTS.value,
        )

    # ------------------------------------------------------------------
    # Step 6: Write outputs
    # ------------------------------------------------------------------

    def _write_outputs(self):
        s = self.state.summary

        # ---- draft.md ----
        critical = [f for f in self.state.flags if f.severity == "critical"]
        review = [f for f in self.state.flags if f.severity == "review"]

        banner = "🔴 CRITICAL FLAGS PRESENT" if critical else ("🟡 REVIEW FLAGS PRESENT" if review else "🟢 NO FLAGS")

        def fmt_fact(f: Fact) -> str:
            val = f.value or MISSING
            suffix = ""
            if f.status == "missing":
                suffix = " ⚠️ MISSING"
            elif f.status == "conflicting":
                suffix = f" ⚠️ CONFLICTING — {f.notes or ''}"
            elif f.status == "pending":
                suffix = " ⏳ PENDING"
            return f"{val}{suffix}"

        def fmt_meds(meds: list[Medication]) -> str:
            if not meds:
                return "  None documented"
            lines = []
            for m in meds:
                lines.append(f"  - {m.name} {m.dose} {m.route} {m.frequency}".strip())
            return "\n".join(lines)

        def fmt_changes(changes: list[MedicationChange]) -> str:
            if not changes:
                return "  No changes detected"
            lines = []
            for c in changes:
                recon = " ⚠️ RECONCILIATION REQUIRED" if c.needs_reconciliation else ""
                lines.append(f"  [{c.change_type}] {c.medication} — {c.details} | Reason: {c.reason}{recon}")
            return "\n".join(lines)

        def fmt_flags(flags: list[SafetyFlag]) -> str:
            if not flags:
                return "  None"
            lines = []
            for f in flags:
                icon = "🔴" if f.severity == "critical" else "🟡"
                lines.append(f"  {icon} [{f.category.upper()}] {f.message}")
            return "\n".join(lines)

        draft = f"""# DISCHARGE SUMMARY DRAFT
## ⚠️ FOR CLINICIAN REVIEW ONLY — NOT A FINAL CLINICAL DOCUMENT
### Status: {banner}

---

## PATIENT DEMOGRAPHICS
{fmt_fact(s.patient_demographics)}

## ADMISSION DATE
{fmt_fact(s.admission_date)}

## DISCHARGE DATE
{fmt_fact(s.discharge_date)}

## PRINCIPAL DIAGNOSIS
{fmt_fact(s.principal_diagnosis)}

## SECONDARY DIAGNOSES
{chr(10).join(f"  - {d}" for d in s.secondary_diagnoses) if s.secondary_diagnoses else "  None documented"}

## HOSPITAL COURSE
{fmt_fact(s.hospital_course)}

## PROCEDURES
{chr(10).join(f"  - {p}" for p in s.procedures) if s.procedures else "  None documented"}

## ALLERGIES
{fmt_fact(s.allergies)}

## DISCHARGE CONDITION
{fmt_fact(s.discharge_condition)}

## ADMISSION MEDICATIONS
{fmt_meds(s.admission_medications)}

## DISCHARGE MEDICATIONS
{fmt_meds(s.discharge_medications)}

## MEDICATION CHANGES (Admission → Discharge)
{fmt_changes(s.medication_changes)}

## FOLLOW-UP INSTRUCTIONS
{fmt_fact(s.follow_up_instructions)}

## PENDING RESULTS
{chr(10).join(f"  - {r}" for r in s.pending_results) if s.pending_results else "  None documented"}

---

## SAFETY FLAGS & REVIEW ITEMS
{fmt_flags(self.state.flags)}

---
*Generated by Discharge Summary Agent | Patient: {self.patient_id} | Steps: {self.state.steps_taken}*
*THIS IS A DRAFT — All MISSING fields must be completed by clinician before finalising.*
"""

        draft_path = self.output_dir / "draft.md"
        draft_path.write_text(draft, encoding="utf-8")
        print(f"  ✓ draft.md written → {draft_path}")

        # ---- summary.json ----
        json_path = self.output_dir / "summary.json"
        json_path.write_text(
            self.state.summary.model_dump_json(indent=2),
            encoding="utf-8",
        )
        print(f"  ✓ summary.json written → {json_path}")

        self.trace.log(
            step=self.state.steps_taken,
            reasoning="All outputs written successfully.",
            action=AgentAction.WRITE_OUTPUTS.value,
            inputs={},
            result={
                "draft_path": str(draft_path),
                "json_path": str(json_path),
                "flags_total": len(self.state.flags),
                "critical_flags": len(critical),
            },
            next_decision=AgentAction.STOP.value,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _add_flag(self, category: str, message: str, severity: str = "review"):
        self.state.flags.append(SafetyFlag(category=category, severity=severity, message=message))