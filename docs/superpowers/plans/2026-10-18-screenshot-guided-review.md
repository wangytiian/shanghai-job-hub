# Screenshot Guided Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AI 整理、问题定位、少量必填、一次明确人工终审，不再让用户修复诊断字段或反复试错。

**Architecture:** 保留 FastAPI/SQLAlchemy/OCR/队列/私有素材/事实引用；新增纯阶段校验和版本化截图终审证明，包装现有保存与确认服务。模板及 JS 消费同一问题结构。旧调用保持严格规则。

**Tech Stack:** 现有 Python/httpx/SQLAlchemy/Jinja/JavaScript/pytest，无新依赖和数据库迁移。

## Global Constraints

- Preserve originals, OCR, manual edits, source/evidence provenance, permissions, optimistic versions, immediate approval invalidation and copy-time checks.
- Never fabricate role, location, date/year, URL, contact, salary, student eligibility or human approval; AI cannot set verification or raise quality scores.
- Only the current screenshot guided-review certificate may replace the numeric quality gate; all other publication gates remain, and legacy/non-screenshot >=70 remains unchanged.
- Diagnostic fields do not block; uncertain optional values must be excluded from formal output, while qualification/common/fee/scope conflicts must block.
- Changes stay in the current dirty feature checkout, backed up under outputs/simplify-before-20261018; no commits/push or unrelated reformatting.
- Tests use isolated databases and unique outputs basetemp; do not run pytest against business data or auto-approve operator jobs.

### Task 1: Pure readiness issues and certificate contract

**Files:** Create `app/services/screenshot_review.py`, `app/services/screenshot_review_policy.py`, `tests/test_screenshot_guided_rules.py`, `tests/test_screenshot_review_policy.py`.

**Interfaces:**
```python
@dataclass(frozen=True)
class ReviewIssue:
    path: str
    code: str
    message: str
    severity: str  # error/warning/info
    action: str
    def to_dict(self) -> dict: ...

def collect_review_issues(record, facts, *, acknowledgement=False) -> tuple[ReviewIssue, ...]: ...
def publication_facts(facts) -> ScreenshotFacts: ...

REVIEW_POLICY_VERSION = "screenshot-guided-review-v1"
def build_review_confirmation(snapshot: dict, *, job_id: int, job_version: int,
                              actor_id: int | None, confirmed_at: str) -> dict: ...
def validate_review_confirmation(snapshot: dict, *, job_id: int, job_version: int) -> bool: ...
def has_current_review_confirmation(job) -> bool: ...
```

- [x] RED isolated tests: simultaneous missing company/location/URL; diagnostic conflicts ignored; missing optional values nonblocking; critical eligibility/common/scope conflicts block; email invalid/missing; unspecified/yearless/expired dates; original material blank/incomplete; master's/PhD-only audience cannot be core undergraduate fit. Ordinary explicit source/official URLs both remain required.
- [x] GREEN implement bounded issues with paths `fields.name`, `roles.0.name`, `common_requirements.0`, `source_url`, `material_complete`; all problems returned, no DB/network. Exact numeric type/date/link validation. Publication projection removes diagnostic values and empty/unclear optional values, never qualifying/common constraints. Explicit optional conflict remains a problem rather than silently choosing a value.
- [x] RED/GREEN certificate tests. Certificate keys: policy, job_id, job_version, import_id, content_revision, actor_id, confirmed_at, facts_sha256. Hash canonical snapshot excluding only review_confirmation. Reject malformed types, duplicate JSON keys when reading, wrong source/import/fingerprint/version/hash, absent/current verification checks and mismatching snapshot-vs-current core Job facts. No dependency on workflow/jobs/distribution modules to avoid cycles; return false on malformed data, not exceptions.
- [x] Focused run: `.\.venv\Scripts\python.exe -m pytest tests/test_screenshot_guided_rules.py tests/test_screenshot_review_policy.py -q --basetemp=outputs/simplify-task1` and report RED/GREEN to `outputs/simplify-task1-report.md`. Do not commit.

### Task 2: Screenshot-specific repair and classification cleanup

**Files:** Modify `app/services/screenshot_tasks.py`, `app/services/screenshot_prefill.py`, `app/services/screenshot_contracts.py`; create `tests/test_screenshot_guided_prefill.py`.

**Interfaces:** Keep existing public task APIs, `ScreenshotFacts` and `EvidenceRef`. Change screenshot extraction rule to `screenshot-extraction-v3-guided-review` so old partial caches do not bypass new processing.

- [x] RED test same-source valid initial fields + repair paraphrase cannot create synthetic diagnostic/eligibility conflicts; omitted repair retains valid initial fields and unclear; changed date/salary/email/qualification/negation remains conflict; cross-chunk and role boundaries/common restrictions remain strict. Repair has <=2 completion calls, no OCR re-call.
- [x] GREEN local same-chunk merge retains initial validated business field when repair just paraphrases same evidence; repair may resolve previously invalid units, not rewrite unrelated correct units. Diagnostics keep latest individually valid diagnostic, do not merge prose into conflicts. Narrow label/whitespace/punctuation normalization only; never generic semantic equivalence removing numerals/negation.
- [x] Prompt explicitly targets Shanghai Lixin undergraduate/finance-related audience: graduate-only material is not core fit. Keep factual fields separate from AI explanations and use literal quotes. Do not change saved provider/key/model or add API calls. Expose user-facing AI action using existing executor, no new provider.
- [x] Focused run new tests plus existing prefill/extraction/tasks runtime tests with `--basetemp=outputs/simplify-task2`; report `outputs/simplify-task2-report.md`. No business DB/network/commits.

### Task 3: Guided confirmation backend and saved-draft failures

**Files:** Modify `app/services/screenshot_workflow.py`, `app/services/structuring.py`, `app/services/jobs.py`, `app/services/distribution.py`; create `tests/test_screenshot_guided_confirmation.py`.

**Interfaces:** consume Task1. Produce:
```python
class ScreenshotReviewValidationError(ValueError):
    issues: tuple[ReviewIssue, ...]

def confirm_guided_screenshot_import(session, import_id: int, data: dict, *, action: str,
    expected_row_version: int, expected_job_row_version=None, expected_job_version=None,
    actor=None) -> ConfirmationResult: ...
```

- [x] RED tests one explicit `acknowledge=True` generates current certificate+two outputs at score0 without raising score; missing/false/nonbool ack rejects; legacy score0 still rejected. Only real click recorded as aggregate confirmation; six bools are derived as the scope of that click, not AI attestations. Permissions unchanged.
- [x] Save draft through existing save service first; use new reserved versions for approval transaction. On all validation/render failures draft remains, generated items/status/certificate roll back. Do not alter legacy `confirm_screenshot_import` atomicity. Scope/audience proof remains conditional; do not create false missing-date/material attestations before user acknowledgement.
- [x] Add screenshot-guided policy to internal `StructuringInput` (default legacy) so optional classification tags/job_family/recruitment_type do not require invented nonempty values; core company/role/location/audience/official URL remain required. Only guided-confirm service sets it. Keep all other structuring logic/checks.
- [x] Reuse original screening, range/evidence/common and audience rules. Diagnostic/absent optional uncertainty no blanket rejection in guided path. `_screen` always sees full original text, so fees and hard restrictions cannot be hidden. Do not silently promote C/D; return structured blockers for unknown audience, clearly preserving draft.
- [x] Snapshot supports optional `review_confirmation` contract; server creates proof after structure/current human verification, before approve/generate. `validate_publishable` skips only numeric threshold when `has_current_review_confirmation(job)` is true. Copy and generation run original dynamic checks and certificate validation. Snapshot/core-field/source/role hash mismatch invalidates proof, modifications/reextract keep existing zeroing/version invalidation. Old snapshot/Job behavior unchanged.
- [x] Focused new confirmation/permissions/legacy/copy tests with `--basetemp=outputs/simplify-task3`. Report `outputs/simplify-task3-report.md`, no real operator approvals/commits.

### Task 4: Summary-first issue UI, required labels and one-click route

**Files:** Modify `app/routes/screenshot_imports.py`, `app/templates/screenshot_review.html`, `app/static/screenshot_import.js`, `app/static/screenshot_import.css`; create `tests/test_screenshot_guided_ui.py`.

**Interfaces:** consume Task1 issues, Task3 new confirmation wrapper. Keep legacy endpoints usable; form sends `review_mode=guided`, explicit ack only through final button.

- [x] RED template/HTTP tests summary+two previews before collapsed full fields; visible required/optional/conditional labels; diagnostics readonly/collapsed, no hidden diagnostics edits submitted. Problems render safe Chinese text plus field paths and stable IDs. Submission with three missing facts returns all three issues, 409 and retained inputs.
- [x] Add live readiness GET/preflight returning serialized issues and version identifiers, no mutation. Guided submit uses wrapper and returns JSON issues on expected failure. Legacy form submissions remain supported. Frontend consumes all issue paths, sets aria-invalid/aria-describedby, opens containing details, focuses target, shows top+sticky action counts. Keep button for showing reasons if submit unavailable; never leave unexplained disabled control. Do not use innerHTML with model text.
- [x] Summary and previews are primary; complete editable fields/source/images remain in disclosure. Use explicit one final acknowledgement button and an optional submit-for-review button for nonreviewers. Remove manual score input in guided mode; keep per-trigger special audience/scope inputs only if needed. AI button label `AI补全与整理`, saves/reuses OCR and existing revision invalidation. Original source/official reuse offered only explicitly or already identical, do not infer official authority from arbitrary source URL.
- [x] Use AI inferred title for display, no user-required workspace title. Do not make empty dates silently checked; summary says no date and final click covers only actually shown state. Failed OCR is not automatically proof of complete original.
- [x] JS retains all unsaved inputs on failure; no window reload after409; save feedback clear. Existing clipboard gates and dirty-state protection remain. Narrow viewport no horizontal overflow, focusable issue navigation.
- [x] Focused frontend/web/E2E plus new tests with `--basetemp=outputs/simplify-task4`; report `outputs/simplify-task4-report.md`. No operator mutation or real AI calls.

### Task 5: Integration acceptance and documentation

**Files:** README, `docs/reports/2026-10-18-screenshot-guided-review-acceptance.md`, append project ledger.

- [x] Generate task-scoped diffs against per-turn backup, review tasks (spec+quality) and fix Important/Critical. Whole feature review after integration. Preserve unrelated dirty feature changes.
- [x] Run `.\.venv\Scripts\python.exe -m pytest -q --basetemp=outputs/simplify-final` once stable; git diff --check. Verify operator business data against online backup, no unintended changes.
- [x] Isolated browser acceptance with fake AI synthetic materials: complete case low-score one-click produces two formats; three errors visible simultaneously and editable data retained; optional/diagnostic case passes; real fee/graduate-only/scope issues block; stale copy rejected. No test records in business DB. Read-only actual latest screenshots show summary/problem redesign; do not approve or reprocess user data without need.
- [x] Restart latest local service only after verifying exact processes, private127.0.0.1 binding. Capture final screenshots. Update README and append ledger using existing artifact-tool+preservation checks; no GitHub actions. Record limitations (real model timing, cloud/PostgreSQL/actual WeChat not newly verified).
