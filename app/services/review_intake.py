"""Pending review routing for authorised collectors and explicit B-source pilots."""

from datetime import date, datetime
import re

from app.services.intake_screening import REVIEW_NOTE, screen_intake
from app.services.source_candidate_policy import (
    CandidateDecision, SourceCandidate, evaluate_candidate, policy_evidence_text,
    STUDENT_UNSUITABLE_EXPERIENCE,
)


_EXPERIENCE = STUDENT_UNSUITABLE_EXPERIENCE
_SCHOOL_ONLY = re.compile(
    r"(?<!不)(?:仅限|只限|限|仅面向|只面向|只接受|仅接受|只招|仅招|仅接收|只接收)\s*"
    r"(?:本校|上海商学院|上海工程技术大学|上海应用技术大学)|(?:不对|不向)\s*外校(?:学生)?(?:开放|报名|申请|投递)|"
    r"(?:不接受|不接收)\s*外校(?:学生)?"
)


def evaluate_review_intake(candidate: SourceCandidate, now: datetime | date | None = None) -> CandidateDecision:
    """Retain only audience-unclear leads; all other missing evidence still blocks.

    Called by authorised collectors and the explicit, selected-sample B pilot.
    `review_only` is not `qualified` and must remain a pending human-review lead.
    """
    decision = evaluate_candidate(candidate, now=now)
    scoped_text = policy_evidence_text(candidate.evidence_text)
    combined = f"{candidate.title}\n{scoped_text}"
    if screen_intake(candidate.title, scoped_text).grade == "D" or _EXPERIENCE.search(combined):
        return CandidateDecision("excluded", ("UNSUITABLE_FOR_STUDENT_REVIEW",))
    if _SCHOOL_ONLY.search(f"{candidate.title}\n{candidate.evidence_text}"):
        return CandidateDecision("excluded", ("SCHOOL_EXCLUSIVE",))
    if decision.verdict == "needs_evidence" and set(decision.reason_codes) == {"TARGET_AUDIENCE_UNCLEAR"}:
        return CandidateDecision("review_only", decision.reason_codes)
    return decision
