# 批量公告类型确认 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让运营人员一次确认所有规则明确建议为“新招聘”的待判断真实公告，同时保留显式人工触发和完整审计记录。

**Architecture:** 服务层统一复用公告类型建议规则，在点击后重新筛选仍符合条件的真实待核验公告，逐条更新分类并写入审核记录。岗位中心显示数量、风险说明及处理结果，不对进度通知、非招聘或演示数据做批量修改。

**Tech Stack:** FastAPI、SQLAlchemy、Jinja2、pytest/TestClient。

## Global Constraints

- 仅由人工点击 POST 操作触发；页面加载不得自动变更记录。
- 仅处理 `is_demo=False`、`status=待核验`、`notice_type=待判断` 且规则建议为“新招聘”的记录。
- 每条被处理记录必须产生独立审核日志。

---

### Task 1: 可审计的批量分类服务

**Files:**
- Modify: `app/services/notice_classification.py`
- Test: `tests/test_notice_classification.py`

**Interfaces:**
- Produces: `suggested_new_recruitment_jobs(session) -> list[Job]` 和 `confirm_suggested_new_recruitments(session, operator_name) -> int`。

- [ ] **Step 1: 写失败测试**

```python
def test_batch_confirmation_only_classifies_pending_real_suggested_recruitment():
    assert confirm_suggested_new_recruitments(session, "测试管理员") == 1
    assert session.get(Job, eligible.id).notice_type == "新招聘"
    assert session.get(Job, progress.id).notice_type == "待判断"
    assert session.get(Job, demo.id).notice_type == "待判断"
```

- [ ] **Step 2: 运行测试并确认缺少函数失败**

Run: `python -m pytest tests/test_notice_classification.py -k batch_confirmation -v`

- [ ] **Step 3: 最小实现**

```python
def suggested_new_recruitment_jobs(session):
    candidates = session.scalars(select(Job).where(...)).all()
    return [job for job in candidates if suggest_notice_type(job.job_title, job.evidence_text) == "新招聘"]
```

- [ ] **Step 4: 重新运行该测试并确认通过**

### Task 2: 岗位中心入口与结果反馈

**Files:**
- Modify: `app/main.py:326-360,482-489`
- Modify: `app/templates/jobs.html`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: 批量服务返回的候选数量。
- Produces: `POST /jobs/classification/confirm-suggestions` 和 `/jobs` 顶部反馈。

- [ ] **Step 1: 写失败测试**

```python
def test_jobs_page_shows_batch_notice_confirmation_and_post_creates_audit_log():
    assert "批量确认" in client.get("/jobs").text
    response = client.post("/jobs/classification/confirm-suggestions", follow_redirects=False)
    assert response.status_code == 303
```

- [ ] **Step 2: 运行测试并确认缺少入口失败**

Run: `python -m pytest tests/test_web.py -k batch_notice_confirmation -v`

- [ ] **Step 3: 最小实现**

```python
return RedirectResponse(
    f"/jobs?data_type=real&status=待核验&classification_feedback=已批量确认 {count} 条新招聘公告",
    status_code=303,
)
```

- [ ] **Step 4: 重新运行页面测试并确认通过**

### Task 3: 完整回归与人工验证

**Files:**
- Test: `tests/test_notice_classification.py`, `tests/test_web.py`

- [ ] **Step 1: 运行完整测试**

Run: `python -m pytest -q`

- [ ] **Step 2: 在本地岗位中心验证候选数量、说明、触发后的结果反馈和审计记录**

Expected: 批量按钮只显示候选数；点击前不改数据；处理后数量减少且每条有审核历史。
