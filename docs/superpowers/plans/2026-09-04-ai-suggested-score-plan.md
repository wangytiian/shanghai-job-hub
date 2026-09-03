# AI Suggested Score v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add evidence-backed AI suggested scores and a safe small-batch scoring queue without changing human publication authority.

**Architecture:** Persist suggested-score metadata separately from the human final score. A scoring service calculates a deterministic baseline, optionally asks the active model for evidence-backed fit/value points, and never promotes a record on model failure. The jobs page invokes a capped batch and exposes the result.

**Tech Stack:** FastAPI, SQLAlchemy/SQLite, Jinja2, pytest.

## Global Constraints

- `quality_score` is only the human final score and starts at 0.
- D-grade, expired, progress notices, and existing final scores are excluded from AI batches.
- AI output must contain evidence from source text; invalid AI output falls back to rules.
- Batch size is capped at 5.

---

### Task 1: Suggested-score service and data persistence

**Files:**
- Modify: `app/models.py`, `app/database.py`
- Create: `app/services/ai_scoring.py`
- Test: `tests/test_ai_scoring.py`

- [ ] Write failing tests for deterministic score breakdown, D-grade exclusion, evidence-validated AI contributions, C-grade cap, and rule fallback.
- [ ] Run `pytest tests/test_ai_scoring.py -q` and confirm it fails because the scoring service does not exist.
- [ ] Implement score data structures and a service returning a score, reason, JSON breakdown, confidence, and status.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Batch route and score visibility

**Files:**
- Modify: `app/main.py`, `app/templates/jobs.html`, `app/templates/job_detail.html`, `app/templates/job_structuring.html`
- Test: `tests/test_web.py`

- [ ] Write failing web tests for batch eligibility, five-item cap, and the separate suggested/final score labels.
- [ ] Run focused web tests and confirm failure.
- [ ] Add the guarded batch endpoint and render results plus the new fields; remove the default 70 from human final-score input.
- [ ] Run focused web tests and confirm pass.

### Task 3: Regression, tracker, and local verification

**Files:**
- Modify: `项目管理/项目推进台账.xlsx`

- [ ] Run full pytest suite.
- [ ] Run one local batch request using the active configured model; verify it does not change final quality scores.
- [ ] Record the update in the project ledger and render-check the added row.
- [ ] Restart the local server, verify the jobs page responds, commit, and push.
