"""Versioned, server-generated human-review receipt and pure integrity checks.

This receipt is an integrity/version marker, not a public cryptographic signature.
Only authorized server review actions may write it to confirmed_content_json.
"""
from datetime import datetime, date
import hashlib
import hmac
import json

from app.services.screenshot_contracts import EvidenceRef
from app.services.screenshot_review import UNSPECIFIED_DEADLINE, known_deadline


REVIEW_POLICY_VERSION = "screenshot-guided-review-v1"
_CHECKS = frozenset({"source_checked", "scope_checked", "audience_checked", "location_checked", "application_checked", "timeliness_checked"})
_CERTIFICATE_KEYS = frozenset({"policy", "job_id", "job_version", "import_id", "content_revision", "actor_id", "confirmed_at", "facts_sha256"})
_CORE_FIELDS = ("employer_name", "announcement_title", "job_title", "job_family", "recruitment_type", "location_category",
    "location_detail", "target_audience", "direction_tags", "deadline", "official_url", "source_url", "posting_scope",
    "attachment_status", "application_method", "application_contact", "student_fit_level", "distribution_recommendation", "notice_type")


def _positive(value):
    return type(value) is int and value > 0


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 100 or "T" not in value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _snapshot_valid(snapshot):
    required = {"schema_version", "import_id", "content_revision", "fields", "roles", "common_requirements", "evidence_refs"}
    if not isinstance(snapshot, dict) or not required <= snapshot.keys() or snapshot.keys() - required - {"student_roles_excerpt", "review_confirmation"}:
        return False
    if type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != 1 or not all(_positive(snapshot[name]) for name in ("import_id", "content_revision")):
        return False
    def fields(value):
        return isinstance(value, dict) and len(value) <= 100 and all(isinstance(name, str) and len(name) <= 100 and isinstance(item, str) and len(item) <= 24000 for name, item in value.items())
    if not fields(snapshot["fields"]) or not isinstance(snapshot["roles"], list) or len(snapshot["roles"]) > 100:
        return False
    if any(not fields(role) or not role.get("job_title", "").strip() for role in snapshot["roles"]):
        return False
    common = snapshot["common_requirements"]
    refs = snapshot["evidence_refs"]
    if not isinstance(common, list) or len(common) > 100 or any(not isinstance(item, str) or len(item) > 24000 for item in common):
        return False
    if not isinstance(refs, list) or len(refs) > 5000:
        return False
    for ref in refs:
        EvidenceRef.from_dict(ref)
    return type(snapshot.get("student_roles_excerpt", False)) is bool


def _hash(snapshot):
    payload = {key: value for key, value in snapshot.items() if key != "review_confirmation"}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def build_review_confirmation(snapshot: dict, *, job_id: int, job_version: int,
                              actor_id: int | None, confirmed_at: str) -> dict:
    try:
        valid = _snapshot_valid(snapshot)
    except (ValueError, TypeError, KeyError, AttributeError):
        valid = False
    if not valid or not _positive(job_id) or not _positive(job_version) or (actor_id is not None and not _positive(actor_id)) or not _timestamp(confirmed_at):
        raise ValueError("一次终审证明的事实结构、身份或版本无效")
    return {"policy": REVIEW_POLICY_VERSION, "job_id": job_id, "job_version": job_version,
        "import_id": snapshot["import_id"], "content_revision": snapshot["content_revision"],
        "actor_id": actor_id, "confirmed_at": confirmed_at, "facts_sha256": _hash(snapshot)}


def validate_review_confirmation(snapshot: dict, *, job_id: int, job_version: int) -> bool:
    try:
        if not _snapshot_valid(snapshot) or not _positive(job_id) or not _positive(job_version):
            return False
        cert = snapshot.get("review_confirmation")
        if not isinstance(cert, dict) or cert.keys() != _CERTIFICATE_KEYS:
            return False
        if cert["policy"] != REVIEW_POLICY_VERSION or not all(_positive(cert[name]) for name in ("job_id", "job_version", "import_id", "content_revision")):
            return False
        if cert["actor_id"] is not None and not _positive(cert["actor_id"]):
            return False
        if not _timestamp(cert["confirmed_at"]) or cert["job_id"] != job_id or cert["job_version"] != job_version:
            return False
        if cert["import_id"] != snapshot["import_id"] or cert["content_revision"] != snapshot["content_revision"]:
            return False
        digest = cert["facts_sha256"]
        return isinstance(digest, str) and len(digest) == 64 and hmac.compare_digest(digest, _hash(snapshot))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return False


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 字段")
        result[key] = value
    return result


def _load(value):
    if not isinstance(value, str) or len(value) > 4_000_000:
        raise ValueError("JSON 类型或长度无效")
    def reject_constant(value):
        raise ValueError("JSON 常量无效")
    return json.loads(value, object_pairs_hook=_unique, parse_constant=reject_constant)


def has_current_review_confirmation(job) -> bool:
    """Bind a stored receipt to current facts/checks in review as well as publish."""
    try:
        version = job.version
        if not _positive(version) or not _positive(job.id) or type(job.confirmed_content_version) is not int or job.confirmed_content_version != version:
            return False
        if type(job.verification_version) is not int or job.verification_version != version:
            return False
        if job.status not in {"待审核", "可发布"} or job.lifecycle_status != "正常":
            return False
        snapshot = _load(job.confirmed_content_json)
        if not validate_review_confirmation(snapshot, job_id=job.id, job_version=version):
            return False
        fingerprint = job.fingerprint
        if not isinstance(fingerprint, str) or not fingerprint.startswith(f"screenshot:{snapshot['import_id']}:") or not fingerprint.split(":", 2)[-1]:
            return False
        checks = _load(job.verification_checks)
        if not isinstance(checks, dict) or not _CHECKS <= checks.keys() or any(checks[key] is not True for key in _CHECKS):
            return False
        fields = snapshot["fields"]
        if any(name not in fields or not isinstance(getattr(job, name, None), str) or fields[name] != getattr(job, name) for name in _CORE_FIELDS):
            return False
        deadline = known_deadline(fields["deadline"])
        return fields["deadline"] == UNSPECIFIED_DEADLINE or (deadline is not None and deadline >= date.today())
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return False
