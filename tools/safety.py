from __future__ import annotations
from models.schemas import DischargeSummary, SafetyFlag, SourceRef


def detect_conflicting_facts(summary: DischargeSummary) -> list[SafetyFlag]:
    """Scan summary for logical contradictions and missing critical data."""
    flags: list[SafetyFlag] = []

    # 1. Timeline check: admission_date must be before discharge_date
    adm = summary.admission_date.value
    dis = summary.discharge_date.value
    if (
        adm not in ("", "MISSING - clinician review required")
        and dis not in ("", "MISSING - clinician review required")
    ):
        try:
            from datetime import datetime
            # Try common date formats
            for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
                try:
                    adm_dt = datetime.strptime(adm.strip(), fmt)
                    dis_dt = datetime.strptime(dis.strip(), fmt)
                    if adm_dt > dis_dt:
                        flags.append(SafetyFlag(
                            category="conflict",
                            severity="critical",
                            message=f"Timeline conflict: admission_date ({adm}) is after discharge_date ({dis}). Clinician review required.",
                        ))
                    break
                except ValueError:
                    continue
        except Exception:
            pass

    # 2. Medication conflict: same drug in both admission & discharge with different dose
    adm_meds = {m.name.lower(): m for m in summary.admission_medications}
    dis_meds = {m.name.lower(): m for m in summary.discharge_medications}
    for drug_name in adm_meds:
        if drug_name in dis_meds:
            adm_m = adm_meds[drug_name]
            dis_m = dis_meds[drug_name]
            if adm_m.dose and dis_m.dose and adm_m.dose != dis_m.dose:
                flags.append(SafetyFlag(
                    category="conflict",
                    severity="review",
                    message=(
                        f"Medication dose change not explained: {drug_name} "
                        f"admission={adm_m.dose} → discharge={dis_m.dose}. "
                        "Reason not documented."
                    ),
                ))

    # 3. Pending results without follow-up instructions
    if summary.pending_results:
        if summary.follow_up_instructions.value in ("", "MISSING - clinician review required"):
            flags.append(SafetyFlag(
                category="pending",
                severity="review",
                message=f"Pending results present ({len(summary.pending_results)} item(s)) but no follow-up instructions documented.",
            ))

    # 4. Conflicting principal diagnosis flag (if status is conflicting)
    if summary.principal_diagnosis.status == "conflicting":
        flags.append(SafetyFlag(
            category="conflict",
            severity="critical",
            message=f"Conflicting principal diagnosis found: {summary.principal_diagnosis.notes}. Clinician must resolve.",
        ))

    return flags


def mock_drug_interaction_lookup(medications: list[str]) -> list[SafetyFlag]:
    """
    Mock drug-interaction database lookup.
    In production replace with a real pharmacological API (e.g. OpenFDA, DrugBank).
    """
    flags: list[SafetyFlag] = []

    # Known high-risk combinations for demonstration
    HIGH_RISK_PAIRS = [
        ({"warfarin", "aspirin"}, "Warfarin + Aspirin: increased bleeding risk. Monitor INR closely."),
        ({"metformin", "contrast"}, "Metformin + IV Contrast: risk of lactic acidosis. Hold metformin 48h."),
        ({"ssri", "tramadol"}, "SSRI + Tramadol: serotonin syndrome risk. Clinician review required."),
        ({"meropenem", "valproate"}, "Meropenem + Valproate: carbapenems reduce valproate levels significantly."),
        ({"insulin", "metformin"}, "Insulin + Metformin: monitor blood glucose closely for hypoglycaemia."),
    ]

    med_lower = {m.lower() for m in medications}

    for pair_set, message in HIGH_RISK_PAIRS:
        # Check if any keyword from the pair appears in med list
        matched = [kw for kw in pair_set if any(kw in m for m in med_lower)]
        if len(matched) >= 2:
            flags.append(SafetyFlag(
                category="drug_interaction",
                severity="critical",
                message=f"⚠️ Potential drug interaction detected: {message} — Clinician review required.",
            ))

    if not flags:
        # Always emit one flag to show the tool ran
        flags.append(SafetyFlag(
            category="drug_interaction",
            severity="review",
            message="No high-risk drug interactions detected in current medication list. Note: interaction screening is rule-based; clinical pharmacist review recommended for complex regimens.",
        ))

    return flags