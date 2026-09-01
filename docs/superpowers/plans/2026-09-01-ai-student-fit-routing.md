# AI 学生适配判断与分层分发 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 补充岗位族、上海适配、学生适配和分发建议，并阻止不适合核心学生用户的岗位生成学生渠道内容。

**Architecture:** AI 草稿分为原文事实与运营判断；数据库保存最终确认结果；结构化页显示建议与依据；分发服务检查分发建议。

**Tech Stack:** FastAPI、SQLAlchemy、SQLite、Jinja2、pytest、Qwen。

## Global Constraints

- 原文事实必须可追溯，AI 不得推断薪资、编制、转正、户口、具体地址或截止时间。
- 岗位族、地点分类、学生适配和分发建议均属于 AI 运营建议，必须有依据和置信度。
- 不适合核心学生用户的岗位保留资料库，但不得生成公众号或微信群队列。
- SQLite 仅做前向兼容增列，不删除历史记录。

---

### Task 1: 增加 AI 运营判断模型和学生适配规则

**Files:** `app/models.py`、`app/database.py`、`app/services/ai_structuring.py`、新建 `app/services/student_fit.py`、`tests/test_ai_structuring.py`、`tests/test_database_migrations.py`

- [ ] 写失败测试：解析含“副教授及以上职称、博士毕业生、出站博士后”的公告，断言输出 `不适合核心学生用户` 与 `不进入学生分发`。
- [ ] 运行 `.\\.venv\\Scripts\\python.exe -m pytest tests/test_ai_structuring.py -q`，确认因字段与规则不存在而失败。
- [ ] 实现 `StudentFitRecommendation` 与 `recommend_student_fit(evidence_text, target_audience)`；将 `副教授`、`正教授`、`博士后`、`高级职称`、`3年以上`、`负责人` 作为高优先级拦截词。
- [ ] 给 `Job`、SQLite 前向迁移、`AnnouncementDraft` 和 AI JSON 提示词增加 `student_fit_level`、`distribution_recommendation`、`ai_rationale`、`ai_confidence`。
- [ ] 运行相关测试确认通过。

### Task 2: 保存并展示 AI 运营判断

**Files:** `app/services/structuring.py`、`app/main.py`、`app/templates/job_structuring.html`、`app/templates/job_detail.html`、`tests/test_structuring.py`、`tests/test_web.py`

- [ ] 写失败测试：结构化提交后保存学生适配和分发建议；AI 预填页面显示 `明确上海` 与 `学生适配`。
- [ ] 运行聚焦测试确认失败。
- [ ] 扩展 `StructuringInput` 与 POST 路由保存运营判断；提交时写入 `ReviewLog(action="AI建议已确认")`。
- [ ] 将页面拆分为“原文事实”和“AI 运营判断”；地点字段优先使用 AI 草稿值，显示依据与置信度，并让运营人员可覆盖建议。
- [ ] 运行 `tests/test_structuring.py`、`tests/test_web.py` 确认通过。

### Task 3: 拦截不适合学生的分发

**Files:** `app/services/distribution.py`、`tests/test_distribution.py`

- [ ] 写失败测试：可发布岗位设置 `不进入学生分发` 后，调用 `create_distribution_items` 必须报“已保留资料库，不生成学生渠道内容”。
- [ ] 运行聚焦测试确认失败。
- [ ] 在分发服务状态校验后增加拦截，并确保不创建任何 `DistributionItem`。
- [ ] 运行全部分发测试确认通过。

### Task 4: 验收、台账和同步

**Files:** `项目管理/项目推进台账.xlsx`、`tests/test_web.py`

- [ ] 写失败测试：岗位详情页显示学生适配和分发建议。
- [ ] 运行测试确认失败，完成页面实现后确认通过。
- [ ] 使用现有 `@oai/artifact-tool` 流程登记台账、渲染并检查最新记录。
- [ ] 全量运行 `.\\.venv\\Scripts\\python.exe -m pytest -q`，在本地验证团校公告显示“不适合核心学生用户 / 不进入学生分发”。
- [ ] 提交并推送本模块全部代码、测试、规格、计划与台账。
