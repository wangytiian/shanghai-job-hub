# AI 公告预分类与结构化预填 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让运营人员在公告结构化页面点击按钮后获得 Qwen 生成的可编辑预填草稿，且不改变任何状态。

**Architecture:** 新建 AI 结构化服务，通过现有凭据服务在后端调用百炼；严格解析 JSON 并输出不可写入的草稿对象。页面请求草稿后重渲染表单，人工提交仍走现有结构化服务。

**Tech Stack:** FastAPI、httpx、SQLAlchemy、Jinja2、pytest、阿里云百炼 Qwen 兼容接口。

## Global Constraints

- API Key 仅由 Windows Credential Manager 在后端读取。
- AI 返回无法确认的字段必须为空；不得自动改状态或发布。
- 模型错误、超时、JSON 错误均降级为手工表单。
- 不增加学生个人数据。

---

### Task 1: AI 草稿解析与安全校验

**Files:** Create `app/services/ai_structuring.py`; Test `tests/test_ai_structuring.py`.

- [ ] 先写失败测试：合法 JSON 转为草稿；Markdown 包裹 JSON 可解析；无效 JSON 报出可展示错误；草稿不包含 API Key。
- [ ] 运行 `& .\.venv\Scripts\python.exe -m pytest tests\test_ai_structuring.py -q`，确认失败。
- [ ] 实现 `AnnouncementDraft`、`parse_ai_draft` 和字段白名单。
- [ ] 重新运行专项测试，确认通过。

### Task 2: 百炼调用与页面预填

**Files:** Modify `app/services/ai_settings.py`, `app/main.py`, `app/templates/job_structuring.html`; Test `tests/test_web.py`.

- [ ] 写失败测试：未配置 Key 返回设置提示；已模拟模型结果时，表单显示 AI 草稿但岗位仍为待核验。
- [ ] 运行 `& .\.venv\Scripts\python.exe -m pytest tests\test_web.py -q`，确认失败。
- [ ] 复用现有模型配置，新增后端调用和 `POST /jobs/{job_id}/structure/ai-draft`；页面显示证据与不确定项。
- [ ] 再次运行页面测试，确认通过。

### Task 3: 全量验证与使用说明

**Files:** Modify `README.md`; Modify `项目管理/项目推进台账.xlsx`.

- [ ] 运行 `& .\.venv\Scripts\python.exe -m pytest -q`。
- [ ] 在未配置 Key 状态下访问页面，确认可继续手工填写。
- [ ] 更新 README 操作说明及项目台账，记录 Key 不落库和人工确认边界。
