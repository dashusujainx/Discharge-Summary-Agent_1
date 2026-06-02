from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    file: str
    page: int | None = None
    quote: str | None = None


class Fact(BaseModel):
    value: str = "MISSING"
    status: str = "missing"
    sources: list[SourceRef] = Field(default_factory=list)
    notes: str | None = None


class Medication(BaseModel):
    name: str
    dose: str | None = None
    route: str | None = None
    frequency: str | None = None
    status: str = "unknown"
    reason: str | None = None
    sources: list[SourceRef] = Field(default_factory=list)


class MedicationChange(BaseModel):
    medication: str
    change_type: str
    details: str
    reason: str | None = None
    needs_reconciliation: bool = False
    sources: list[SourceRef] = Field(default_factory=list)


class SafetyFlag(BaseModel):
    category: str
    severity: str = "review"
    message: str
    sources: list[SourceRef] = Field(default_factory=list)


class DischargeSummary(BaseModel):
    patient_demographics: Fact = Field(default_factory=Fact)
    admission_date: Fact = Field(default_factory=Fact)
    discharge_date: Fact = Field(default_factory=Fact)
    principal_diagnosis: Fact = Field(default_factory=Fact)
    secondary_diagnoses: list[Fact] = Field(default_factory=list)
    hospital_course: Fact = Field(default_factory=Fact)
    procedures: list[Fact] = Field(default_factory=list)
    admission_medications: list[Medication] = Field(default_factory=list)
    discharge_medications: list[Medication] = Field(default_factory=list)
    medication_changes: list[MedicationChange] = Field(default_factory=list)
    allergies: list[Fact] = Field(default_factory=list)
    follow_up_instructions: list[Fact] = Field(default_factory=list)
    pending_results: list[Fact] = Field(default_factory=list)
    discharge_condition: Fact = Field(default_factory=Fact)
    conflicts: list[SafetyFlag] = Field(default_factory=list)
    safety_flags: list[SafetyFlag] = Field(default_factory=list)


class ExtractionPage(BaseModel):
    file: str
    page: int
    text: str
    method: str
    status: str
    error: str | None = None


class AgentState(BaseModel):
    patient_id: str
    input_paths: list[Path]
    pages: list[ExtractionPage] = Field(default_factory=list)
    summary: DischargeSummary = Field(default_factory=DischargeSummary)
    flags: list[SafetyFlag] = Field(default_factory=list)
    steps_taken: int = 0
    done: bool = False
    extraction_failed: bool = False
    extraction_done: bool = False
    validation_done: bool = False
    safety_checked: bool = False
    safety_checked: bool = False


class TraceEvent(BaseModel):
    step: int
    reasoning: str
    action: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    next_decision: str


class AgentAction(str, Enum):
    READ_PDFS = "read_pdfs"
    EXTRACT_FACTS = "extract_facts"
    RECONCILE_MEDICATIONS = "reconcile_medications"
    CHECK_SAFETY = "check_safety"
    VALIDATE = "validate"
    WRITE_OUTPUTS = "write_outputs"
    STOP = "stop"
