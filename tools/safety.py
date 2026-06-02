from __future__ import annotations

from collections import defaultdict

from models.schemas import Medication, SafetyFlag


def mock_drug_interaction_lookup(medications: list[Medication]) -> list[SafetyFlag]:
    names = {med.name.lower(): med for med in medications}
    rules = [
        (("warfarin", "amiodarone"), "Potential increased anticoagulation effect."),
        (("warfarin", "aspirin"), "Potential increased bleeding risk."),
        (("lisinopril", "spironolactone"), "Potential hyperkalemia risk."),
        (("metformin", "contrast"), "Renal/metformin safety review may be needed."),
    ]
    flags: list[SafetyFlag] = []
    for pair, message in rules:
        if all(name in names for name in pair):
            flags.append(
                SafetyFlag(
                    category="drug_interaction",
                    severity="clinician_review",
                    message=f"{pair[0]} + {pair[1]}: {message}",
                )
            )
    return flags


def detect_conflicting_facts(section: str, values: list[str]) -> list[SafetyFlag]:
    normalized = defaultdict(list)
    for value in values:
        clean = " ".join(value.lower().split())
        if clean and clean != "missing":
            normalized[clean].append(value)

    if len(normalized) <= 1:
        return []

    return [
        SafetyFlag(
            category="conflict",
            severity="clinician_review",
            message=f"Conflicting {section}: " + " | ".join(sorted({v for vals in normalized.values() for v in vals})),
        )
    ]
