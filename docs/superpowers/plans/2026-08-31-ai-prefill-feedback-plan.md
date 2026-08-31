# AI 预填反馈 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让公告结构化页面的 AI 预填操作在原页面清楚展示进行中、成功和失败结果。

**Architecture:** 浏览器提交 AI 预填表单时，由页面脚本将按钮切换为加载状态并防止重复提交。后端将可预期的 AI 配置和模型错误重定向回结构化页面，以安全的提示文本显示；成功响应保留现有表单预填逻辑并显示成功提示。

**Tech Stack:** FastAPI、Jinja2、原生浏览器 JavaScript、pytest/TestClient。

## Global Constraints

- 不改变 AI 提取、岗位审核、发布校验或真实岗位数据。
- 不在页面显示 API Key、原始模型报错或堆栈。
- 保留手工结构化入口，失败后表单仍可继续填写。

---

### Task 1: 后端可恢复错误反馈

**Files:**
- Modify: `app/main.py:385-401`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `GET /jobs/{job_id}/structure?ai_feedback=error&ai_message=...`，用于安全显示 AI 预填失败原因。

- [ ] **Step 1: 写出失败测试**

```python
def test_ai_prefill_missing_key_returns_to_structuring_page_with_safe_feedback():
    response = client.post(f"/jobs/{job_id}/structure/ai-draft", follow_redirects=False)
    assert response.status_code == 303
    assert "ai_feedback=error" in response.headers["location"]
```

- [ ] **Step 2: 运行测试并确认它因当前 400 响应而失败**

Run: `python -m pytest tests/test_web.py -k ai_prefill_missing_key -v`

- [ ] **Step 3: 实现安全重定向和模板反馈参数**

```python
return RedirectResponse(
    url=f"/jobs/{job_id}/structure?ai_feedback=error&ai_message=...",
    status_code=303,
)
```

- [ ] **Step 4: 重新运行该测试，确认通过**

### Task 2: 页面加载态与结果提示

**Files:**
- Modify: `app/templates/job_structuring.html`
- Modify: `app/static/app.css`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: 模板变量 `ai_feedback`、`ai_message`、`ai_draft`。
- Produces: 提交时的 `aria-busy` 加载提示、禁用状态和可见错误/成功提示。

- [ ] **Step 1: 写出失败测试**

```python
def test_structuring_page_renders_ai_loading_contract_and_error_feedback():
    response = client.get(f"/jobs/{job_id}/structure?ai_feedback=error&ai_message=模型暂不可用")
    assert 'data-ai-prefill-form' in response.text
    assert 'AI 正在读取公告' in response.text
    assert '模型暂不可用' in response.text
```

- [ ] **Step 2: 运行测试并确认它因缺少加载标记和反馈内容而失败**

Run: `python -m pytest tests/test_web.py -k ai_loading_contract -v`

- [ ] **Step 3: 实现最小页面脚本和样式**

```html
<form data-ai-prefill-form ...>
  <button data-ai-prefill-button ...>AI 预填表单</button>
  <span data-ai-prefill-status hidden>AI 正在读取公告…</span>
</form>
```

```javascript
form.addEventListener('submit', () => {
  button.disabled = true;
  form.setAttribute('aria-busy', 'true');
  status.hidden = false;
});
```

- [ ] **Step 4: 重新运行页面测试，确认通过**

### Task 3: 回归验证与可视检查

**Files:**
- Test: `tests/test_web.py`

- [ ] **Step 1: 运行完整测试套件**

Run: `python -m pytest -q`

- [ ] **Step 2: 启动本地服务并在结构化页面验证加载、成功及未配置 Key 的失败反馈**

Expected: 加载时按钮不可重复点击；成功保留预填字段；失败返回同一页面且无敏感信息。
