# 招聘截止时间可选化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 允许已核验的招聘公告在没有统一截止时间时安全进入审核和分发。

**Architecture:** 用一个规范文本值“公告未明确统一截止时间”表示原文确实未提供日期，而不是空值或推断日期。结构化服务负责归一化和校验，发布校验允许该规范值，渠道渲染依据该值使用专门的提醒文案。

**Tech Stack:** FastAPI、SQLAlchemy、Jinja2、pytest。

## Global Constraints

- 官方链接、原文证据、明确招聘事项、人工终审、质量分及附件规则仍为发布硬门槛。
- 不得由 AI 或人工虚构截止日期。
- 所有变更必须先有失败测试，再写生产代码。

---

### Task 1: 截止时间规范化及发布校验

**Files:**
- Modify: `app/services/structuring.py`
- Modify: `app/services/jobs.py`
- Modify: `tests/test_structuring.py`
- Modify: `tests/test_jobs.py`

**Interfaces:**
- Produces: `structure_job(..., deadline="")` 将记录保存为 `公告未明确统一截止时间`。
- Produces: `validate_publishable(job)` 不把该规范值视为占位字段。

- [ ] **Step 1: Write the failing tests**

```python
def test_structure_job_allows_blank_deadline_as_explicitly_unstated(session):
    job = _pending_verification_job(session)
    result = structure_job(session, job.id, _valid_input(deadline=""), "本地管理员")
    assert result.deadline == "公告未明确统一截止时间"

def test_validation_allows_explicitly_unstated_deadline(demo_job):
    demo_job.deadline = "公告未明确统一截止时间"
    assert "占位字段" not in validate_publishable(demo_job)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_structuring.py tests/test_jobs.py -q`

- [ ] **Step 3: Write minimal implementation**

```python
UNSPECIFIED_DEADLINE = "公告未明确统一截止时间"

def _normalize_deadline(value: str) -> str:
    return value.strip() or UNSPECIFIED_DEADLINE
```

Use the normalized deadline before persistence and remove the old non-empty deadline requirement.

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/test_structuring.py tests/test_jobs.py -q`

### Task 2: 渠道稿与表单说明

**Files:**
- Modify: `app/services/distribution.py`
- Modify: `app/templates/job_structuring.html`
- Modify: `app/templates/job_detail.html`
- Modify: `tests/test_distribution.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Produces: `build_wechat_draft(job)` 针对未明确日期输出催看原文的真实提醒。

- [ ] **Step 1: Write the failing tests**

```python
def test_wechat_draft_explains_unstated_deadline(session, pending_review_job):
    pending_review_job.deadline = "公告未明确统一截止时间"
    draft = build_wechat_draft(pending_review_job)
    assert "建议尽快查看官方原文或附件确认报名安排" in draft.html
    assert "请在截止日期前" not in draft.html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_distribution.py::test_wechat_draft_explains_unstated_deadline -q`

- [ ] **Step 3: Write minimal implementation**

Branch channel application copy for the normalized deadline value and remove the HTML `required` attribute from the deadline input. Add plain-language form/help and review-option copy.

- [ ] **Step 4: Run focused tests**

Run: `pytest tests/test_distribution.py tests/test_web.py -q`

### Task 3: Regression verification and project record

**Files:**
- Modify: `项目管理/项目推进台账.xlsx`

- [ ] **Step 1: Run full suite**

Run: `.venv\\Scripts\\python.exe -m pytest -q`

- [ ] **Step 2: Record the completed behavior**

Add one ledger entry explaining that real announcements without a unified deadline can be reviewed and published with an explicit warning, while hard verification gates remain.
