# Operator Review Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AI score handling and human review clear, traceable, and efficient without changing automatic-publication safeguards.

**Architecture:** Store a compact JSON verification checklist on each job and validate it in the structuring service before a record can enter manual final review. Keep AI score state independent from final quality score; enrich the jobs query with score-status filtering, queue counters, and a deterministic priority order. Jinja templates render all user feedback in the current page.

**Tech Stack:** FastAPI, SQLAlchemy/SQLite, Jinja2, vanilla JavaScript, pytest.

## Global Constraints

- `quality_score` remains human-only and starts at 0 for unreviewed real jobs.
- AI suggested score never changes job status or final score.
- No unified deadline is valid when the reviewer confirms current validity.
- Batch scoring only processes real, pending-verification, new-recruitment A/B/C jobs with no final score.
- Existing publication checks continue to require a human final score of at least 70.

---

### Task 1: Persist and validate the six-item human checklist

**Files:**
- Modify: `app/models.py`, `app/database.py`, `app/services/structuring.py`
- Test: `tests/test_structuring.py`

**Interfaces:**
- Consumes: `StructuringInput` from `app.services.structuring`.
- Produces: `StructuringInput.verification_checks: dict[str, bool]` and `Job.verification_checks: str` containing JSON.

- [ ] **Step 1: Write the failing tests**

```python
def test_structure_job_rejects_missing_human_verification_check(session):
    job = _pending_verification_job(session)
    checks = {
        "source_checked": True,
        "scope_checked": True,
        "audience_checked": True,
        "location_checked": True,
        "application_checked": True,
        "timeliness_checked": False,
    }

    with pytest.raises(ValueError, match="时效"):
        structure_job(session, job.id, _valid_input(verification_checks=checks), "本地管理员")


def test_structure_job_saves_complete_human_verification_checklist(session):
    job = _pending_verification_job(session)
    result = structure_job(session, job.id, _valid_input(), "本地管理员")

    assert json.loads(result.verification_checks)["application_checked"] is True
    assert session.query(ReviewLog).filter_by(job_id=job.id, action="人工核验清单已确认").count() == 1
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/test_structuring.py -q`

Expected: FAIL because `StructuringInput` does not accept `verification_checks` and `Job` has no stored checklist.

- [ ] **Step 3: Implement the smallest persisted checklist**

```python
# app/models.py
verification_checks: Mapped[str] = mapped_column(Text, default="{}", nullable=False)

# app/services/structuring.py
REQUIRED_VERIFICATION_CHECKS = {
    "source_checked": "原始来源",
    "scope_checked": "岗位或公告范围",
    "audience_checked": "面向学生人群",
    "location_checked": "工作地点",
    "application_checked": "官方投递入口",
    "timeliness_checked": "时效",
}

missing = [label for key, label in REQUIRED_VERIFICATION_CHECKS.items() if not data.verification_checks.get(key)]
if missing:
    errors["verification_checks"] = f"请确认：{'、'.join(missing)}"
job.verification_checks = json.dumps(data.verification_checks, ensure_ascii=False, sort_keys=True)
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/test_structuring.py -q`

Expected: all structuring tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/models.py app/database.py app/services/structuring.py tests/test_structuring.py
git commit -m "feat: require human review checklist"
```

### Task 2: Add an auditable score queue and filters

**Files:**
- Modify: `app/main.py`, `app/templates/jobs.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `GET /jobs?score_status=<待建议|AI建议|规则建议|不适用>`.
- Produces: `score_queue_counts` and ordered `jobs` in the jobs template context.

- [ ] **Step 1: Write the failing web tests**

```python
def test_jobs_page_filters_by_ai_score_status_and_explains_score_layers(client, session):
    response = client.get("/jobs?data_type=real&score_status=规则建议")

    assert response.status_code == 200
    assert "AI 建议分（非审核结论）" in response.text
    assert "规则建议" in response.text


def test_jobs_page_prioritizes_pending_a_grade_jobs_with_higher_suggested_score(client, session):
    # Seed A-grade 82-point and B-grade 90-point real pending records.
    response = client.get("/jobs?data_type=real")

    assert response.text.index("A级82分岗位") < response.text.index("B级90分岗位")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/test_web.py -k "ai_score_status or prioritizes_pending_a_grade" -q`

Expected: FAIL because `score_status` is not parsed and the explanatory label is absent.

- [ ] **Step 3: Implement filter, priority order, and queue summary**

```python
# app/main.py, inside jobs()
score_status: str = ""
if score_status:
    statement = statement.where(Job.ai_score_status == score_status)
statement = statement.order_by(
    Job.status != "待核验",
    case((Job.intake_grade == "A", 0), (Job.intake_grade == "B", 1), (Job.intake_grade == "C", 2), else_=3),
    Job.ai_suggested_score.desc(),
    Job.collected_at.desc(),
)
```

Render a four-state queue summary (`待建议`, `AI建议`, `规则建议`, `不适用`) and a native select for `score_status`. Rename visible headers to `入库初筛`, `AI 建议分（非审核结论）`, and `人工最终分`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m pytest tests/test_web.py -k "ai_score_status or prioritizes_pending_a_grade" -q`

Expected: selected score-status records render; A grade sorts before B grade.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/templates/jobs.html tests/test_web.py
git commit -m "feat: add operator score queue filters"
```

### Task 3: Make batch and form feedback safe and understandable

**Files:**
- Modify: `app/main.py`, `app/templates/jobs.html`, `app/templates/job_structuring.html`, `app/static/app.css`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: existing `POST /jobs/scoring/suggest-batch` and `POST /jobs/{job_id}/structure`.
- Produces: current-page feedback with `role="status"` or `role="alert"`; no raw validation JSON response for valid routes.

- [ ] **Step 1: Write the failing web tests**

```python
def test_score_batch_redirect_reports_result_and_keeps_final_score_unchanged(client, session):
    response = client.post("/jobs/scoring/suggest-batch", follow_redirects=True)

    assert response.status_code == 200
    assert "建议分批处理完成" in response.text
    assert "人工最终分" in response.text


def test_structuring_page_renders_missing_checklist_as_inline_error(client, pending_job):
    response = client.post(f"/jobs/{pending_job.id}/structure", data=_valid_form_payload())

    assert response.status_code == 200
    assert "请确认：" in response.text
    assert 'data-submission-feedback' in response.text
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest tests/test_web.py -k "score_batch_redirect_reports or missing_checklist" -q`

Expected: FAIL because the template has no checklist error output and the current score copy is different.

- [ ] **Step 3: Implement current-page feedback**

```html
<button data-score-batch-button class="button button-primary" type="submit">生成 AI 建议分（最多 5 条）</button>
<span data-score-batch-status hidden aria-live="polite">正在生成本批建议分，请勿重复点击…</span>
```

Use a submit listener that disables only the clicked batch button and reveals the live status. Render a success notice after redirect with processed, AI, rule, skipped, and failed counts. In the structuring template, render six required checkbox labels from `verification_checks`; bind the server error to the checklist fieldset and focus it when invalid.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python -m pytest tests/test_web.py -k "score_batch_redirect_reports or missing_checklist" -q`

Expected: both routes return the current HTML screen with readable feedback.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/templates/jobs.html app/templates/job_structuring.html app/static/app.css tests/test_web.py
git commit -m "feat: improve review and batch feedback"
```

### Task 4: Full regression, visual verification, tracker, and integration

**Files:**
- Modify: `项目管理/项目推进台账.xlsx`
- Verify: `tests/`, local `http://127.0.0.1:8000/jobs?data_type=real`

**Interfaces:**
- Consumes: all completed Tasks 1–3.
- Produces: a verified local workbench and one project-tracker update.

- [ ] **Step 1: Run the complete automated suite**

Run: `python -m pytest -q`

Expected: exit code 0 with all tests passing.

- [ ] **Step 2: Verify the operational path in the local browser**

Check: jobs list displays four queue counts and score filters; a pending record displays distinct score labels; structuring page blocks an unchecked checklist inline; blank deadline remains acceptable after all checklist items are checked.

- [ ] **Step 3: Record the shipped change**

Add a row in `项目管理/项目推进台账.xlsx` titled `运营审核工作台 v1.0`, summarizing queue filters, score-layer labels, human checklist, and inline feedback. Render the edited workbook before delivery.

- [ ] **Step 4: Commit and push**

```bash
git add 项目管理/项目推进台账.xlsx
git commit -m "docs: record operator review workbench update"
git push origin main
```
