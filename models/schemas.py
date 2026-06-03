from __future__ import annotations
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    file: str = ""
    page: int = 0
    quote: str = ""


class Fact(BaseModel):
    value: str = "MISSING - clinician review required"
    status: str = "missing"   # missing | pending | conflicting | ok
    sources: List[SourceRef] = Field(default_factory=list)
    notes: Optional[str] = None


class Medication(BaseModel):
    name: str = ""
    dose: str = ""
    route: str = ""
    frequency: str = ""
    status: str = "unknown"   # unknown | active | discontinued
    reason: str = ""
    sources: List[SourceRef] = Field(default_factory=list)


class MedicationChange(BaseModel):
    medication: str = ""
    change_type: str = ""     # ADDED | DISCONTINUED | CHANGED
    details: str = ""
    reason: str = "Not documented - clinician review required"
    needs_reconciliation: bool = True


class SafetyFlag(BaseModel):
    category: str = ""        # conflict | missing_field | pending | unreadable | drug_interaction
    severity: str = "review"  # review | critical
    message: str = ""
    sources: List[SourceRef] = Field(default_factory=list)


class DischargeSummary(BaseModel):
    # Required facts
    patient_demographics: Fact = Field(default_factory=Fact)
    admission_date: Fact = Field(default_factory=Fact)
    discharge_date: Fact = Field(default_factory=Fact)
    principal_diagnosis: Fact = Field(default_factory=Fact)
    secondary_diagnoses: List[str] = Field(default_factory=list)
    hospital_course: Fact = Field(default_factory=Fact)
    discharge_condition: Fact = Field(default_factory=Fact)

    # Procedures
    procedures: List[str] = Field(default_factory=list)

    # Medications
    admission_medications: List[Medication] = Field(default_factory=list)
    discharge_medications: List[Medication] = Field(default_factory=list)
    medication_changes: List[MedicationChange] = Field(default_factory=list)

    # Allergies
    allergies: Fact = Field(default_factory=Fact)

    # Follow-up
    follow_up_instructions: Fact = Field(default_factory=Fact)
    pending_results: List[str] = Field(default_factory=list)

    # Safety
    safety_flags: List[SafetyFlag] = Field(default_factory=list)


class ExtractionPage(BaseModel):
    file: str = ""
    page: int = 0
    text: str = ""
    method: str = "embedded_text"   # embedded_text | ocr
    status: str = "ok"              # ok | empty | error
    error: Optional[str] = None


class AgentAction(str, Enum):
    READ_PDFS = "READ_PDFS"
    EXTRACT_FACTS = "EXTRACT_FACTS"
    RECONCILE_MEDICATIONS = "RECONCILE_MEDICATIONS"
    CHECK_SAFETY = "CHECK_SAFETY"
    VALIDATE = "VALIDATE"
    WRITE_OUTPUTS = "WRITE_OUTPUTS"
    STOP = "STOP"


class AgentState(BaseModel):
    pages: List[ExtractionPage] = Field(default_factory=list)
    summary: DischargeSummary = Field(default_factory=DischargeSummary)
    flags: List[SafetyFlag] = Field(default_factory=list)
    steps_taken: int = 0
    done: bool = False
    extraction_done: bool = False
    reconciliation_done: bool = False
    validation_done: bool = False
    safety_checked: bool = False
    pdfs_read: bool = False