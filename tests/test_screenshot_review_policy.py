"""Certificate integrity and current Job binding, without persistence."""
from copy import deepcopy
import importlib
import json
from types import SimpleNamespace

import pytest


def policy():
    assert importlib.util.find_spec("app.services.screenshot_review_policy") is not None, "review certificate policy is missing"
    return importlib.import_module("app.services.screenshot_review_policy")


def snapshot():
    return {"schema_version": 1, "import_id": 7, "content_revision": 2, "fields": {
        "employer_name": "星辰公司", "job_title": "财务实习生", "announcement_title": "招聘",
        "job_family": "财会", "recruitment_type": "实习", "location_category": "明确上海", "location_detail": "上海",
        "target_audience": "应届生", "direction_tags": "财会", "deadline": "2099-12-31",
        "official_url": "https://example.com/jobs", "source_url": "https://example.com/source",
        "posting_scope": "single_role", "attachment_status": "not_required", "application_method": "email",
        "application_contact": "jobs@example.com", "student_fit_level": "核心适配", "distribution_recommendation": "进入学生分发审核",
        "notice_type": "新招聘"}, "roles": [], "common_requirements": [], "evidence_refs": [], "student_roles_excerpt": False}


def signed(actor=None):
    snap = snapshot()
    snap["review_confirmation"] = policy().build_review_confirmation(snap, job_id=3, job_version=4,
        actor_id=actor, confirmed_at="2026-10-18T10:30:00+08:00")
    return snap


def job_for(snap):
    return SimpleNamespace(id=3, version=4, confirmed_content_version=4, verification_version=4,
        fingerprint="screenshot:7:abc", status="待审核", lifecycle_status="正常",
        verification_checks=json.dumps({name: True for name in ("source_checked", "scope_checked", "audience_checked", "location_checked", "application_checked", "timeliness_checked")}),
        confirmed_content_json=json.dumps(snap), **snap["fields"])


def test_certificate_keys_hash_and_local_actor_are_exact():
    snap = signed()
    cert = snap["review_confirmation"]
    assert set(cert) == {"policy", "job_id", "job_version", "import_id", "content_revision", "actor_id", "confirmed_at", "facts_sha256"}
    assert cert["policy"] == policy().REVIEW_POLICY_VERSION == "screenshot-guided-review-v1"
    assert len(cert["facts_sha256"]) == 64 and cert["actor_id"] is None
    assert policy().validate_review_confirmation(snap, job_id=3, job_version=4)
    assert policy().has_current_review_confirmation(job_for(snap))


def test_canonical_hash_ignores_only_confirmation_and_not_insertion_order():
    snap = signed(actor=2)
    reversed_snapshot = dict(reversed(list(snap.items())))
    assert policy().validate_review_confirmation(reversed_snapshot, job_id=3, job_version=4)
    snap["fields"]["salary"] = "5000"
    assert not policy().validate_review_confirmation(snap, job_id=3, job_version=4)


@pytest.mark.parametrize("name,value", [("job_id", True), ("job_id", 0), ("job_version", "4"), ("actor_id", False),
    ("actor_id", -1), ("confirmed_at", ""), ("confirmed_at", "today"), ("confirmed_at", "2026-02-30T12:00:00")])
def test_builder_rejects_invalid_metadata(name, value):
    kwargs = dict(job_id=3, job_version=4, actor_id=None, confirmed_at="2026-10-18T10:30:00")
    kwargs[name] = value
    with pytest.raises(ValueError):
        policy().build_review_confirmation(snapshot(), **kwargs)


@pytest.mark.parametrize("key,value", [("import_id", True), ("content_revision", 0), ("schema_version", True),
    ("roles", {}), ("evidence_refs", [{"wrong": "shape"}]), ("student_roles_excerpt", 1)])
def test_invalid_snapshot_contract_is_not_signed(key, value):
    snap = snapshot()
    snap[key] = value
    with pytest.raises(ValueError):
        policy().build_review_confirmation(snap, job_id=3, job_version=4, actor_id=None, confirmed_at="2026-10-18T10:30:00")


@pytest.mark.parametrize("key,value", [("job_id", 9), ("job_version", 5), ("import_id", 8), ("content_revision", 3),
    ("policy", "old"), ("facts_sha256", "x" * 64), ("actor_id", True)])
def test_changed_certificate_metadata_is_invalid(key, value):
    snap = signed()
    snap["review_confirmation"][key] = value
    assert not policy().validate_review_confirmation(snap, job_id=3, job_version=4)


@pytest.mark.parametrize("attribute,value", [("version", 5), ("version", True), ("confirmed_content_version", 0),
    ("confirmed_content_version", True), ("verification_version", 0), ("verification_checks", "{}"),
    ("fingerprint", "source:7:abc"), ("fingerprint", "screenshot:8:abc"),
    ("employer_name", "另一公司"), ("location_detail", "杭州"), ("target_audience", "博士"),
    ("application_contact", "other@example.com"), ("posting_scope", "multi_role_announcement"),
    ("attachment_status", "pending"), ("distribution_recommendation", "仅保留资料库"),
    ("deadline", "2001-01-01"), ("lifecycle_status", "已撤销"), ("status", "已截止")])
def test_certificate_requires_current_job_versions_identity_facts_and_checks(attribute, value):
    job = job_for(signed())
    setattr(job, attribute, value)
    assert not policy().has_current_review_confirmation(job)


def test_every_current_core_fact_is_bound_even_when_snapshot_field_was_removed():
    snap = snapshot()
    del snap["fields"]["location_detail"]
    snap["review_confirmation"] = policy().build_review_confirmation(snap, job_id=3, job_version=4, actor_id=None,
        confirmed_at="2026-10-18T10:30:00")
    job = job_for(snap)
    job.location_detail = "上海"
    assert not policy().has_current_review_confirmation(job)


@pytest.mark.parametrize("raw", ["null", "[]", "{", None, 2])
def test_malformed_stored_json_returns_false_without_throwing(raw):
    job = job_for(signed())
    job.confirmed_content_json = raw
    assert not policy().has_current_review_confirmation(job)


def test_duplicate_json_keys_and_checks_are_rejected():
    job = job_for(signed())
    job.confirmed_content_json = job.confirmed_content_json[:-1] + ',"import_id":7}'
    assert not policy().has_current_review_confirmation(job)
    job = job_for(signed())
    job.verification_checks = job.verification_checks[:-1] + ',"source_checked":true}'
    assert not policy().has_current_review_confirmation(job)


def test_expired_signed_snapshot_is_invalid_even_with_matching_current_facts():
    snap = snapshot()
    snap["fields"]["deadline"] = "2001-01-01"
    snap["review_confirmation"] = policy().build_review_confirmation(snap, job_id=3, job_version=4, actor_id=None,
        confirmed_at="2026-10-18T10:30:00")
    assert not policy().has_current_review_confirmation(job_for(snap))


def test_yearless_signed_deadline_cannot_receive_score_exception():
    snap = snapshot()
    snap["fields"]["deadline"] = "10月31日"
    snap["review_confirmation"] = policy().build_review_confirmation(snap, job_id=3, job_version=4, actor_id=None,
        confirmed_at="2026-10-18T10:30:00")
    assert not policy().has_current_review_confirmation(job_for(snap))
