# 公告结构化与人工审核闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让待核验公告可由运营人员补全事实字段后进入待审核，并通过人工审核转为可发布。

**Architecture:** 保持 `Job` 作为唯一岗位事实记录，不增加新表。新增结构化服务负责验证和保存字段，FastAPI 增加结构化页面与提交路由；详情页根据状态展示下一步操作。审核日志继续记录结构化与最终审核动作。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、Jinja2、SQLite、pytest。

## Global Constraints

- 仅允许人工提交结构化字段；不调用 AI、不自动发布。
- 官方报名链接、适合人群和截止日期必须有值；链接仅允许 http/https。
- 待核验不能直接进入可发布。
- 保留原始公告来源链接与证据，不用结构化字段覆盖原文。
- 项目不是 Git 仓库，不执行 commit。

---

### Task 1: 结构化服务与验证

**Files:**
- Create: `app/services/structuring.py`
- Test: `tests/test_structuring.py`

**Interfaces:**
- Consumes: `Session`、岗位 ID、字段字符串。
- Produces: `structure_job(session, job_id, data, operator_name) -> Job`。

- [ ] **Step 1: 写失败测试**

```python
def test_structure_job_requires_link_audience_and_deadline(session):
    data = StructuringInput("测试单位", "测试岗位", "综合管理", "校招", "明确上海", "上海", "大四/应届校招", "工商运营", "2026-09-01", "")
    with pytest.raises(ValueError, match="官方报名链接"):
        structure_job(session, 1, data, "本地管理员")


def test_structure_job_saves_fields_marks_pending_review_and_writes_log(session):
    data = StructuringInput("测试单位", "测试岗位", "综合管理", "校招", "明确上海", "上海", "大四/应届校招", "工商运营", "2026-09-01", "https://example.com/apply")
    job = structure_job(session, 1, data, "本地管理员")
    assert job.status == "待审核"
    assert job.official_url == "https://example.com/apply"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `& .\.venv\Scripts\python.exe -m pytest tests\test_structuring.py -q`

Expected: FAIL，因为 `app.services.structuring` 尚不存在。

- [ ] **Step 3: 实现最小服务**

```python
@dataclass(frozen=True)
class StructuringInput:
    employer_name: str
    job_title: str
    job_family: str
    recruitment_type: str
    location_category: str
    location_detail: str
    target_audience: str
    direction_tags: str
    deadline: str
    official_url: str
    note: str = ""


def structure_job(session, job_id, data, operator_name):
    validate_structuring_input(data)
    job = session.get(Job, job_id)
    if job is None or job.status != "待核验":
        raise ValueError("只有待核验公告可以结构化")
    # 将 data 的字段逐项写入 job，设为待审核，并新增 ReviewLog。
```

- [ ] **Step 4: 运行测试确认通过**

Run: `& .\.venv\Scripts\python.exe -m pytest tests\test_structuring.py -q`

Expected: PASS。

### Task 2: 结构化页面与提交路由

**Files:**
- Create: `app/templates/job_structuring.html`
- Modify: `app/main.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `GET /jobs/{job_id}/structure` 和 `POST /jobs/{job_id}/structure`。
- Produces: 填写表单；成功后重定向到岗位详情。

- [ ] **Step 1: 写失败测试**

```python
def test_pending_verification_job_opens_structuring_form():
    response = client.get("/jobs/11/structure")
    assert "公告结构化" in response.text
    assert "官方报名链接" in response.text


def test_structuring_form_redirects_after_valid_submission():
    response = client.post("/jobs/11/structure", data=valid_data, follow_redirects=False)
    assert response.status_code == 303
```

- [ ] **Step 2: 运行测试确认失败**

Run: `& .\.venv\Scripts\python.exe -m pytest tests\test_web.py -q`

Expected: FAIL，因为结构化路由尚不存在。

- [ ] **Step 3: 最小路由和页面实现**

```python
@app.get("/jobs/{job_id}/structure")
def job_structuring(request, job_id):
    # 仅待核验岗位可打开；传入 job 和原文证据。


@app.post("/jobs/{job_id}/structure")
def submit_job_structuring(job_id, employer_name: str = Form(), job_title: str = Form(), official_url: str = Form()):
    # 接收表单字段，构建 StructuringInput，调用 structure_job，成功重定向详情页。
```

- [ ] **Step 4: 运行测试确认通过**

Run: `& .\.venv\Scripts\python.exe -m pytest tests\test_web.py -q`

Expected: PASS。

### Task 3: 详情页状态操作与闭环验证

**Files:**
- Modify: `app/templates/job_detail.html`
- Modify: `tests/test_web.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `Job.status`。
- Produces: 待核验显示“开始公告结构化”；待审核显示最终审核操作；可发布显示分发操作。

- [ ] **Step 1: 写失败测试**

```python
def test_pending_verification_detail_links_to_structuring_not_final_approval():
    response = client.get("/jobs/11")
    assert "开始公告结构化" in response.text
    assert "通过并进入可发布" not in response.text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `& .\.venv\Scripts\python.exe -m pytest tests\test_web.py -q`

Expected: FAIL，因为详情页尚未按待核验状态分流。

- [ ] **Step 3: 最小模板和说明更新**

```jinja2
{% if job.status == '待核验' %}
  <a href="/jobs/{{ job.id }}/structure">开始公告结构化</a>
{% elif job.status == '待审核' %}
  {# 展示通过、退回、淘汰表单 #}
{% endif %}
```

- [ ] **Step 4: 完整验证**

Run: `& .\.venv\Scripts\python.exe -m pytest -q`

Expected: PASS，并手动访问真实待核验岗位确认页面可填写、提交后可审核。
