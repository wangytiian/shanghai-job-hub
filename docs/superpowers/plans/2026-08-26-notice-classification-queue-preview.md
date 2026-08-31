# 非招聘通知拦截与队列摘要 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 拦截招聘进度通知进入发布流程，并让内容队列只保留简短可读摘要。

**Architecture:** 在 `Job` 增加公告类型和分类建议字段；分类服务使用确定性关键词产生建议，人工操作决定最终类型。分发服务移除对完整 `evidence_text` 的引用；归档进度通知时删除该岗位的待发送队列。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、SQLite、Jinja2、pytest。

## Global Constraints

- 本期仅用关键词建议，不调用 AI。
- 未人工确认为“新招聘”的公告不能进入结构化或发布。
- 完整采集原文仅存证据区，不得进入队列或公众号草稿。
- 已发布内容不自动撤回。
- 项目不是 Git 仓库，不执行 commit。

---

### Task 1: 分类服务与数据字段

**Files:**
- Modify: `app/models.py`
- Modify: `app/database.py`
- Create: `app/services/notice_classification.py`
- Test: `tests/test_notice_classification.py`

**Interfaces:**
- Produces: `suggest_notice_type(title, evidence_text) -> str` 与 `classify_job(session, job_id, notice_type, operator_name) -> Job`。

- [ ] 写失败测试：体检通知建议“招聘进度通知”；新招聘可确认；进度通知归档时删除队列。
- [ ] 运行 `& .\.venv\Scripts\python.exe -m pytest tests\test_notice_classification.py -q`，确认失败。
- [ ] 添加 `notice_type`、`notice_type_suggestion`；实现关键词建议及分类后的状态、日志和队列删除。
- [ ] 再次运行同一测试，确认通过。

### Task 2: 详情页分类操作与结构化门槛

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/job_detail.html`
- Modify: `app/services/structuring.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `POST /jobs/{job_id}/classification`；仅 `notice_type == "新招聘"` 的待核验公告可提交结构化。

- [ ] 写失败测试：体检通知详情显示“标记为招聘进度通知”；未确认为新招聘时结构化被拒绝。
- [ ] 运行 `& .\.venv\Scripts\python.exe -m pytest tests\test_web.py -q`，确认失败。
- [ ] 添加分类表单路由、详情页动作和结构化服务门槛。
- [ ] 再次运行同一测试，确认通过。

### Task 3: 队列摘要与现有记录清理

**Files:**
- Modify: `app/services/distribution.py`
- Modify: `app/templates/queues.html`
- Create: `scripts/archive_non_recruiting_notices.py`
- Test: `tests/test_distribution.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `_public_article(job)` 不含 `evidence_text`；清理脚本归档命中体检、面试、录用、公示等词的真实未发布记录。

- [ ] 写失败测试：公众号队列内容不包含 `evidence_text`，只包含短核验说明。
- [ ] 运行 `& .\.venv\Scripts\python.exe -m pytest tests\test_distribution.py -q`，确认失败。
- [ ] 改造队列生成函数并实现一次性归档脚本；脚本只处理真实、未发布且命中关键词的记录。
- [ ] 运行全量 `& .\.venv\Scripts\python.exe -m pytest -q`，确认通过；执行清理脚本并查看结果。
