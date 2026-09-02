# 多模型 AI 与公众号内容提炼 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 增加安全的 GPT/OpenAI 兼容中转模型配置，并以当前文本模型生成受约束的公众号内容提炼稿。

**Architecture:** 使用提供方设置记录保存非敏感配置，用 Windows 凭据管理器保存每个提供方的密钥。业务模块通过统一文本完成接口调用。AI 返回结构化内容 JSON，固定渲染器负责输出 FINJOB 公众号视觉稿并提供降级模板。

**Tech Stack:** FastAPI、SQLAlchemy、httpx、Jinja2、pytest、Windows Credential Manager。

## Global Constraints

- API Key 不得进入数据库、模板、日志、测试输出或 Git。
- 模型不得补造原文没有的招聘事实。
- 只有已人工审核且 A/B 入库分级岗位可生成渠道内容。
- GPT 中转使用 OpenAI 兼容 Chat Completions 接口。

---

### Task 1: 多提供方安全配置

**Files:**
- Modify: `app/models.py`, `app/database.py`, `app/services/ai_settings.py`, `app/main.py`, `app/templates/ai_settings.html`
- Test: `tests/test_ai_settings.py`, `tests/test_database_migrations.py`

**Interfaces:**
- Produces: `AiSettingsService.complete_text(session, prompt) -> str` 和 `get_all_settings(session)`。

- [ ] 写入 GPT 提供方保存、独立密钥和 OpenAI 兼容地址测试的失败测试。
- [ ] 运行 `pytest tests/test_ai_settings.py -v`，确认新接口尚不存在而失败。
- [ ] 为提供方设置添加 `base_url` 与 `is_active_text_provider`，添加 SQLite 向前迁移。
- [ ] 实现独立凭据账号、OpenAI 兼容客户端、当前提供方选择及统一完成接口。
- [ ] 在设置页新增 GPT 中转地址/密钥/模型/测试和当前提供方选择。
- [ ] 运行 `pytest tests/test_ai_settings.py tests/test_database_migrations.py -v`。

### Task 2: 将既有 AI 调用切换至统一接口

**Files:**
- Modify: `app/main.py`, `app/services/real_collection.py`
- Test: `tests/test_ai_settings.py`, `tests/test_web.py`

**Interfaces:**
- Consumes: `AiSettingsService.complete_text(session, prompt)`。

- [ ] 写入已选择提供方被公告预填与入库初筛使用的失败测试。
- [ ] 运行对应 pytest 用例，确认旧代码仍直接使用 Qwen 而失败。
- [ ] 将公告结构化、初筛回调替换为统一完成接口。
- [ ] 运行相关 pytest 用例，确认通过。

### Task 3: AI 内容提炼和稳定公众号渲染

**Files:**
- Create: `app/services/ai_content_draft.py`
- Modify: `app/services/distribution.py`, `app/main.py`, `app/templates/wechat_draft.html`
- Test: `tests/test_ai_content_draft.py`, `tests/test_distribution.py`

**Interfaces:**
- Produces: `build_content_prompt(job) -> str`、`parse_content_draft(content, job) -> ContentDraft` 和 `build_wechat_draft(job, content_draft=None) -> WechatDraft`。

- [ ] 写入 AI JSON 不能注入未经证据支持的截止时间、薪资或编制的失败测试。
- [ ] 运行 `pytest tests/test_ai_content_draft.py tests/test_distribution.py -v`，确认缺少模块而失败。
- [ ] 实现 JSON 解析与证据约束，未证实内容丢弃或降级为空。
- [ ] 在公众号草稿页生成 AI 提炼稿；请求失败时渲染基础稿并显示反馈。
- [ ] 使用固定 HTML/CSS 输出 FINJOB 的五段式版式，不让 AI 写 HTML。
- [ ] 运行 `pytest tests/test_ai_content_draft.py tests/test_distribution.py -v`。

### Task 4: 回归验证与变更记录

**Files:**
- Modify: `项目管理/项目推进台账.xlsx`
- Test: `tests/`

- [ ] 运行完整 `pytest -q`。
- [ ] 启动本地服务并验证设置页与一条公众号草稿页返回 200。
- [ ] 在台账中增加本次版本、功能和验证摘要。
- [ ] 提交 Git 并推送到已配置 GitHub 远端。
