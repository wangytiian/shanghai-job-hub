# 公告结构化表单内校验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将公告结构化校验失败改为表单内标红、回显和中文原因提示。

**Architecture:** 路由捕获领域校验错误后重新渲染结构化模板；模板依据字段错误映射给输入控件增加错误样式和说明。成功路径保持 303 跳转岗位详情。

**Tech Stack:** FastAPI、Jinja2、SQLAlchemy、pytest。

## Global Constraints

- 保留既有 `structure_job` 事实与发布准入校验。
- 不虚构招聘事实；附件待核验不能进入待审核。

---

### Task 1: 结构化错误字段映射

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_web.py`

- [ ] 写失败测试：POST 缺失岗位名称时应返回 200 的结构化页，并包含字段错误标记和已填写内容。
- [ ] 运行：`pytest tests/test_web.py -k structure -q`，预期测试因当前返回 400 JSON 而失败。
- [ ] 在路由中捕获 `ValueError`，把错误和表单数据重新交给模板渲染。
- [ ] 运行同一测试，预期通过。

### Task 2: 页面内反馈与视觉标记

**Files:**
- Modify: `app/templates/job_structuring.html`
- Modify: `app/static/app.css`
- Test: `tests/test_web.py`

- [ ] 写失败测试：业务错误应显示顶部摘要、字段内 `aria-invalid` 和字段说明。
- [ ] 运行测试并确认缺少该页面结构而失败。
- [ ] 在模板按字段错误映射显示红框、说明和黄色状态类提醒。
- [ ] 运行全量：`pytest -q`，预期全绿。
