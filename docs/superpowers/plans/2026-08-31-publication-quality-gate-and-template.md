# 招聘公告发布质量门槛与模板修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让未核验或信息不完整的招聘公告无法发布，并生成事实准确、可复制的公众号草稿。

**Architecture:** 在 `Job` 增加公告范围、附件状态、投递方式和投递联系信息。结构化服务负责把表单状态写入记录；发布校验服务集中执行拦截规则；公众号草稿根据公告范围和投递方式使用条件化模板。

**Tech Stack:** Python 3, FastAPI, SQLAlchemy, Jinja2, pytest, SQLite。

## Global Constraints

- 所有对外内容必须经人工最终审核；AI 只能提取原文事实。
- 不得在草稿中展示待核验占位值或推断事实。
- 保留 SQLite 前向兼容迁移和既有审核日志。

---

### Task 1: 增加公告事实状态与发布校验

**Files:**
- Modify: `app/models.py`, `app/database.py`, `app/services/jobs.py`, `app/services/reviews.py`
- Test: `tests/test_jobs.py`, `tests/test_reviews.py`

- [ ] 写失败测试，覆盖附件待核验、低质量分、未核验风险和占位字段均不能通过。
- [ ] 运行 `python -m pytest tests/test_jobs.py tests/test_reviews.py -q`，确认失败原因是规则尚不存在。
- [ ] 增加字段并实现 `validate_publishable()` 的确定性规则；审核通过要求核验备注并更新时间。
- [ ] 再次运行上述测试，确认通过。

### Task 2: 结构化表单与 AI 预填

**Files:**
- Modify: `app/services/structuring.py`, `app/services/ai_structuring.py`, `app/main.py`, `app/templates/job_structuring.html`
- Test: `tests/test_structuring.py`, `tests/test_ai_structuring.py`, `tests/test_web.py`

- [ ] 写失败测试，覆盖“附件待核验”不能提交、AI 草稿携带公告范围及投递方式。
- [ ] 运行相关测试确认失败。
- [ ] 写入结构化字段、增加下拉选项并扩展 AI JSON 解析；缺失事实使用明确未知状态。
- [ ] 运行相关测试确认通过。

### Task 3: 条件化公众号草稿与旧数据保护

**Files:**
- Modify: `app/services/distribution.py`, `app/main.py` 或迁移服务
- Test: `tests/test_distribution.py`, `tests/test_web.py`

- [ ] 写失败测试，覆盖邮箱投递文案、多岗位汇总文案以及不展示占位字段。
- [ ] 运行测试确认失败。
- [ ] 实现条件模板；启动时将不安全的旧“可发布”记录退回待核验。
- [ ] 运行分发和网页测试确认通过。

### Task 4: 回归验证与项目记录

**Files:**
- Modify: `项目管理/项目推进台账.xlsx`

- [ ] 运行 `python -m pytest -q`。
- [ ] 启动本地服务，检查结构化页面、通过/拦截提示和公众号草稿。
- [ ] 在项目台账新增本次升级简述及验证结果。
