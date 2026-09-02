# GPT Responses API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support and accurately test Chat Completions and Responses API modes for GPT-compatible providers.

**Architecture:** Persist an API-mode setting per provider. The AI settings service passes it to a single client which chooses the endpoint, request payload, and response parser. The settings page makes the choice visible and only declares success after a parsed model response.

**Tech Stack:** FastAPI, SQLAlchemy/SQLite, httpx, Jinja2, pytest.

## Global Constraints

- API keys remain in Windows Credential Manager only.
- Existing GPT records default to `chat_completions`.
- UI and stored errors must not expose keys.

---

### Task 1: Persist and render the API mode

**Files:**
- Modify: `app/models.py`, `app/database.py`, `app/services/ai_settings.py`, `app/main.py`, `app/templates/ai_settings.html`
- Test: `tests/test_ai_settings.py`, `tests/test_web.py`

- [ ] Add failing tests for saving and rendering `responses` mode.
- [ ] Run targeted tests and confirm they fail because the new argument and UI option do not exist.
- [ ] Add `api_mode` with default `chat_completions`, migrate SQLite, persist it in `save_openai_settings`, accept the form value, and render the select field.
- [ ] Run targeted tests and confirm they pass.

### Task 2: Parse Responses API output and strengthen tests

**Files:**
- Modify: `app/services/ai_settings.py`
- Test: `tests/test_ai_settings.py`

- [ ] Add failing tests for `/responses` URL, `output_text` parsing, nested output parsing, empty-output rejection, and connection testing through a parsed response.
- [ ] Run targeted tests and confirm the failures identify the missing mode support.
- [ ] Implement the two request/parse modes and make `test_connection` use a minimal parsed completion.
- [ ] Run targeted tests and then the full test suite.

### Task 3: Configure and verify the local Terra provider

**Files:**
- Modify: `项目管理/项目推进台账.xlsx`

- [ ] Set the existing OpenAI-compatible local setting to `responses` mode without reading or printing its key.
- [ ] Execute a minimal real completion through `gpt-5.6-terra`; record only status, mode, model, and safe error if any.
- [ ] Add an update row to the project ledger and render-check it.
- [ ] Commit and push after tests and real verification.
