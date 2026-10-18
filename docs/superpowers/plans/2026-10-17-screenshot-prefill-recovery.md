# Screenshot prefill recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Screenshot users receive AI-prefilled, evidence-checked fields instead of a blank form when one field is malformed.

**Architecture:** Preserve FastAPI/SQLite/private upload/durable queue and strict parser. Add schema+partial recovery as pure functions, screenshot-only structured AI requests, and a bounded repair path in the existing worker.

**Tech Stack:** Existing Python/httpx/SQLAlchemy/Jinja/JavaScript/pytest only, no new dependency or database migration.

## Global Constraints

- Preserve all user edits, original images and OCR text; do not change model credentials/provider selection or publish/approve automatically.
- Strict evidence, role boundaries, fees/common restrictions, permissions and optimistic version guards stay enforced.
- Tests use isolated databases and unique outputs basetemp. No production DB in pytest. Do not commit/push.
- Capture baseline source files in outputs before edits so incremental diffs can be reviewed despite previous uncommitted work.

### Task 1: Pure schema, repair prompt and per-unit recovery

**Files:** Create `app/services/screenshot_prefill.py`, `tests/test_screenshot_prefill.py`. Do not edit strict parser.

**Interfaces:**

```python
def screenshot_response_schema() -> dict: ...  # {name, strict, schema}
def recover_extraction_chunk(content: str, text: str, evidence_refs=(), *, text_offset=0, content_revision=1) -> ScreenshotFacts: ...
def build_repair_prompt(text: str, rejected_content: str, error_summary: str) -> str: ...
```

- [x] Write red tests: valid unit retained when another field has fabricated quote; malformed JSON rejected; one outer JSON fence accepted; invalid global enum is empty/unclear; cross-role eligibility isolated; invalid role title removes only that role; common failure produces blocking warning; unknown properties do not silently become facts; repair prompt labels rejected output untrusted.
- [x] Implement schema using allowed field names/enums; required object properties and additionalProperties=false. Reuse strict parser validation for each unit; never fabricate references or weaken `_parse_field`.
- [x] Restore valid global fields independently; validate role units with their original title order/anchors to prevent a rejected title from expanding another role's scope. Invalid values empty+unclear and safe diagnostic warnings. Common failure warning starts `AI公共条件待核对` for workflow gate.
- [x] Focused verification: `.\.venv\Scripts\python.exe -m pytest tests/test_screenshot_prefill.py tests/test_screenshot_extraction.py -q --basetemp=outputs/pytest-prefill-pure`.
- [x] Write red/green and self-review report `outputs/prefill-task1-report.md`.

### Task 2: Structured AI requests with bounded compatibility fallback

**Files:** Modify `app/services/ai_settings.py`; create `tests/test_screenshot_structured_ai.py`.

**Interface:** `AiSettingsService.freeze_screenshot_text_request(session, schema: dict) -> Callable[[str], str]`; existing `freeze_text_request` stays compatible. Internal client method/optional parameter names can be chosen locally and documented.

- [x] Red tests for Chat response_format JSON Schema, Responses text.format schema, Bailian format, old complete path unchanged, known unsupported schema error single fallback, auth/rate/network errors no compatibility retry, secrets absent from exceptions.
- [x] Implement screenshot-specific bounded structured requests; gpt-5.6-terra can use documented reasoning none for extraction, with explicit unsupported-parameter handling if needed. Do not enable costly arbitrary automatic provider switching.
- [x] Explicit schema unsupported400/422 only permits one legacy JSON fallback; unsupported temperature/reasoning errors must be handled safely without infinite retry or assuming auth failure means compatibility.
- [x] Focused verify `.\.venv\Scripts\python.exe -m pytest tests/test_screenshot_structured_ai.py tests/test_ai_settings.py -q --basetemp=outputs/pytest-prefill-ai`.
- [x] Report exact client/freeze signatures, red/green evidence in `outputs/prefill-task2-report.md`.

### Task 3: Integrate worker recovery and UI

**Files:** `app/services/screenshot_tasks.py`, `screenshot_contracts.py`, `screenshot_workflow.py`, `app/routes/screenshot_imports.py`, `app/templates/screenshot_review.html`, `app/static/screenshot_import.js`, related task/frontend/HTTP tests.

- [x] Red test initial invalid response→one repair→valid fields; repair failure→persist valid facts and issue list; model late result cannot overwrite current edits; common-recovery warning blocks confirmation until explicit current-version human correction; OCR calls remain unchanged.
- [x] Worker uses new schema freeze when service exposes it, otherwise legacy injected test service contract. On strict ValueError build bounded repair prompt and retry once; partial recovery persists if repair remains invalid or unavailable. No malformed or ungrounded output enters confirmed snapshot.
- [x] Increment extraction rule in contracts and use that one source throughout tasks/pure processing; persist safe diagnostics and request counts in extraction_chunks_json, not raw provider HTTP payload or keys.
- [x] UI names and explains prefilled facts, unresolved fields, current configured OCR/text model; rename retry CTA to clear `AI重新提取字段` when OCR complete. Preserve dirty-state/permissions. No automatic approval, copied link invention or model provider changes.
- [x] Focused tests, whole suite, incremental code review; fix Important/Critical.
- [x] Backup current operator DB and source, then once process the user's failed screenshot with existing API using successful OCR; record filled count/blocked fields, do not approve or create test posts. Start current local app. Document actual results, update project ledger using its existing safe workflow, no GitHub push.

## Acceptance checks

```python
# Conceptual assertions that covering tests and the real target must exercise:
assert initial_bad_field_does_not_erase_valid_company
assert fabricated_quote_never_reaches_confirmed_snapshot
assert repair_call_count <= 1
assert existing_ocr_is_reused
assert manual_edits_win_over_late_model_output
```

No architecture replacement, no OCR provider upgrade, no bulk reprocessing of unrelated records.
