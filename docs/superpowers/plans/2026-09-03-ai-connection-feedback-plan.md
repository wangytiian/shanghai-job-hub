# AI Connection Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show an immediate, safe success or failure result after an operator tests either configured AI provider.

**Architecture:** The test routes will redirect back to the settings page with a short, non-sensitive result token. The settings page renders a provider-specific result panel and uses a small browser-side submit handler to show progress and prevent duplicate requests.

**Tech Stack:** FastAPI, Jinja2, pytest, CSS, vanilla browser JavaScript.

## Global Constraints

- API keys must never appear in HTML, query parameters, logs, or feedback text.
- The existing persisted connection status remains the durable configuration record.
- The test result describes only the provider selected by the submitted test form.

---

### Task 1: Add provider-scoped result feedback

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/ai_settings.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `AiProviderSetting.connection_status`, `text_model`, and `last_tested_at`.
- Produces: `test_provider` and `test_result` query values consumed only by `/settings/ai` rendering.

- [ ] **Step 1: Write the failing tests**

```python
response = client.post("/settings/ai/openai/test", follow_redirects=False)
page = client.get(response.headers["location"])
assert "测试成功" in page.text
assert "gpt-5.6-terra" in page.text
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_web.py -q -k connection_feedback`

Expected: FAIL because the response location and page do not yet include the provider-specific feedback.

- [ ] **Step 3: Add the minimal route and template changes**

```python
return RedirectResponse("/settings/ai?test_provider=openai_compatible&test_result=success", status_code=303)
```

Render a green success notice only when the returned provider matches the relevant settings card. Render a red error notice based on the stored sanitized error summary for failed or unconfigured tests.

- [ ] **Step 4: Run the focused tests and verify success**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_web.py -q -k connection_feedback`

Expected: PASS.

### Task 2: Add visible in-progress feedback

**Files:**
- Modify: `app/templates/ai_settings.html`
- Modify: `app/static/app.css`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: forms marked with `data-ai-test-form`.
- Produces: disabled test button and the visible text `正在测试，请稍候…` until the page navigates.

- [ ] **Step 1: Write the failing page assertions**

```python
page = client.get("/settings/ai")
assert 'data-ai-test-form' in page.text
assert "正在测试，请稍候" in page.text
```

- [ ] **Step 2: Run the test and verify failure**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_web.py -q -k loading_feedback`

Expected: FAIL because the page has no testing-state markup.

- [ ] **Step 3: Add the minimal client behavior and styles**

```javascript
form.addEventListener("submit", () => {
  button.disabled = true;
  button.textContent = "正在测试…";
  status.hidden = false;
});
```

- [ ] **Step 4: Run the focused tests and verify success**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_web.py -q -k loading_feedback`

Expected: PASS.

### Task 3: Verify the whole path

**Files:**
- Modify: `项目管理/项目推进台账.xlsx`

- [ ] **Step 1: Run all tests**

Run: `.\\.venv\\Scripts\\python.exe -m pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Restart the local service and inspect the settings page**

Check that success feedback appears after the configured GPT test and that the API key remains absent from rendered HTML.

- [ ] **Step 3: Record the completed update**

Add one row to the existing project tracker describing the test feedback, tests, and secret-safety boundary.
