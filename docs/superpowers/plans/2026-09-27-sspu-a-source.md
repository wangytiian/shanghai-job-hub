# 二工大 A 类来源实施与验收计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把二工大公开招聘公告接入 A 类自动采集，实际产出有证据、待人工核验的上海岗位。

**Architecture:** 新增独立新闻正文适配器，复用 CampusAnnouncement/CampusJobDetail、请求预算、入库策略、现有调度。无需数据库迁移，不改变其他来源准入与发布审批。

**Tech Stack:** Python、httpx、BeautifulSoup、SQLAlchemy、SQLite、pytest。

## Global Constraints

- 只接二工大；来源 A 类不等于岗位审批通过。
- 不新增依赖，不绕过访问控制，不禁用 TLS，不调用付费模型，不推送 GitHub。
- 新岗位全部待核验；不确定人群沿已有 C 类人工复核，硬性排除不放宽。
- 分岗位保留地点/资格/投递证据，不从学校或公司地址推断工作地点。
- 三页、每页十篇、间隔一秒、180 秒总预算、12 秒单次超时、最多一次瞬时错误重试。
- 所有修改使用 apply_patch；测试先于实现；不得修改真实数据库直到备份并通过离线验证。
- 用户已授权自行规划推进，不再等待普通中间确认。

## 状态与基线

- 基线 `0dcd092`，原工作区干净；独立工具因外层目录非 Git 仓库不可用，已在实际仓库创建 `feat/sspu-a-source`，不在 main 上开发。
- 开始前全量测试：423 passed，1 条既有 Starlette/httpx 弃用警告。
- 所有工作包状态仅在本文维护。

### Task 1: 公开新闻适配器（完成，独立复核通过）

**Files:** Create `app/sources/sspu_news.py`; Create `tests/test_sspu_source.py`; optional redacted fixtures under `tests/fixtures/sources/sspu/`.

**Interfaces:**

```python
parse_sspu_announcements(payload: object) -> list[CampusAnnouncement]
parse_sspu_details(payload: object, announcement: CampusAnnouncement) -> list[CampusJobDetail]
fetch_sspu_announcements(client, pages: int = 3, *, controller=None) -> list[CampusAnnouncement]
fetch_sspu_details(client, announcement: CampusAnnouncement, *, controller=None) -> list[CampusJobDetail]
```

- [x] 读取设计与已确认接口的实际响应，核实列表与 HTML 分段字段；限公开正常阅读。
- [x] 先写解析/网络契约测试，执行 `.venv/Scripts/python.exe -m pytest tests/test_sspu_source.py -q` 并记录红灯。
- [x] 最小实现接口、有限翻页、重复页停止、请求预算、公开状态检查；返回现有数据契约。
- [x] 测试覆盖源氏段落多岗位、宣安段落格式、未知地点、跨岗应届条件不泄漏、稳定身份、明确投递与坏 href、空正文/非公开/错误JSON、分页和超时。复杂表格尚未支持，明确分类跳过。

验收示例（示例企业必须是脱敏样本）：

```python
assert shanghai.location_category == '明确上海'
assert elsewhere.location_category != '明确上海'
assert '2027届' not in audience_unknown.evidence_text
assert 'file:' not in shanghai.official_url
assert shanghai.identity_key == salary_changed.identity_key
```

- [x] 跑绿色测试、自审，提交适配器和测试；不改采集服务、目录或正式数据库。适配器相关 85 项通过；提交 `b1cde26`、`02bbd1e`、`c855a04`。

### Task 2: A 类目录、正式采集与调度（完成，独立复核通过）

**Files:** Modify `app/sources/catalog.py`, `app/services/real_collection.py`; Create `tests/test_sspu_collection.py`; adjust existing source count tests only if necessary.

**Interfaces:** consumes Task 1 public functions; produces `collect_sspu_jobs(session, client, pages=3, now=None, intake_ai_complete=None) -> RealCollectionResult`.

- [x] 先写失败测试：目录中 `sspu-news` 为 A 且可调度，目录同步保留停用；指定来源采集进入待核验，二次相同输入新增为零，详情变化触发复核。
- [x] 新增来源和调度分派，使用现有 `evaluate_review_intake` 与 `_save_detail`。保持其他来源代码行为不变。SSPU 加独占学历、真实学生资格和元数据变更检查，不调用模型。
- [x] 统计公告数、解析岗位数、新增/更新/无变化/人工复核、规则过滤原因、实际异常类型；全部正文失败必须明确失败。
- [x] 验证示例：

```python
assert source.library_tier == 'A' and can_auto_collect(source)
assert all(job.status == '待核验' for job in inserted)
assert second.created_jobs == 0
assert unchanged.created_at == original_created_at
assert disabled_source.is_enabled is False
```

- [x] 专项与全量回归；独立复核跨岗位污染、失效链接与状态覆盖风险；修正并复测。最终全量 521 passed，既有警告 1 条。

### Task 3: 真实运行与交付（完成）

**Files:** update this plan; Create `docs/sources/2026-09-27-sspu-a-validation.md`; backup/report artifacts in ignored `outputs/sspu-a/`.

- [x] 使用 SQLite backup API 备份正式库，`PRAGMA integrity_check` 确认 `ok`。
- [x] 正常公开请求先临时库试采；人工抽核至少源氏/宣安各一条，确认未知截止、上海证据与应届条件。
- [x] 同步仅新增来源默认配置，执行二工大正式采集；再采一次确认无重复新增。没有运行全来源强制采集。首次 56 新增，第二次 0 新增/56 无变化，均无请求处理错误。
- [x] 重启当前本地服务使新代码生效，验证 `/health/live`、`/health/ready`、`/sources` 与岗位页均 200。保留现有 Windows 计划任务，不重复创建。
- [x] 记录真实结果、错误/过滤、下一次计划时间、未知截止与连续运行仍待观察；不承诺已达日均目标。详见 `docs/sources/2026-09-27-sspu-a-validation.md`。

## 复核记录

设计自审：无新增认证/发布权限，无批量升级其他来源。用户指定直接 A 与既有 B 类三轮准入不冲突，因为仅对新二工大来源采用明确授权的初始 A 配置。

独立复核：集成审查发现并修复模型回调、元数据变更保留审批、硕士表达及实习管理经验误判；解析审查发现并修复共同福利尾部串岗。全部问题已有失败回归、修复和独立复验。更改沿现有架构，无新增表、依赖或其他来源抓取。
