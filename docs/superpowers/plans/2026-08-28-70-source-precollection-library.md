# 70 家分层预抓取库 v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在本地招聘内容运营后台登记、展示和安全管理 70 家分层官方招聘来源，同时确保只有已验证的 4 家来源参与岗位采集。

**Architecture:** 扩展 `Source` 的来源分层和运营字段，并把 70 家目录定义集中保存在来源目录模块。采集服务仅选择 `is_enabled=True` 且 `library_tier="A"` 的来源；B/C/D 层仅提供目录与监控信息，绝不会创建可发布岗位。来源页按层级展示来源价值、状态、最近检查和下一步动作。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、SQLite、Jinja2、pytest、openpyxl。

## Global Constraints

- 项目定位为面向上海立信会计金融学院学生的非官方就业信息服务，不得暗示校方授权。
- 仅处理公开官方招聘页面；不得绕过登录、验证码、反爬或身份认证。
- 新来源默认不可采集、不可自动发布；所有岗位仍须人工审核。
- 已验证自动采集来源固定为 4 家，除非另行完成单来源试跑验收。
- API 密钥不得写入数据库、日志、测试断言或网页 HTML。
- 本工作目录不是 Git 仓库；每个任务完成后运行对应测试和全量测试，不执行 Git 提交。
- 本次重要完成项必须登记到 `项目管理/项目推进台账.xlsx`。

---

## File Structure

- Modify: `app/models.py` — 为 `Source` 增加分层、学生价值分、适配状态、下一步动作、官方招聘入口和监控摘要字段。
- Modify: `app/database.py` — 为已有 SQLite 库补齐新增 `sources` 列。
- Modify: `app/sources/catalog.py` — 集中定义 70 家来源及其安全默认值，并幂等同步到数据库。
- Create: `app/services/source_library.py` — 提供来源层级规则、来源页显示文案和安全的监控摘要更新函数。
- Modify: `app/services/real_collection.py` — 在每日任务中明确限制为 A 层且已启用的来源。
- Modify: `app/main.py` — 来源页提供层级计数和来源库数据；恢复操作不得把 B/C/D 层变成自动采集。
- Modify: `app/templates/sources.html` — 显示 70 源分层、学生价值分、适配状态和清晰的“不会自动抓取”边界。
- Modify: `app/static/app.css` — 给层级和适配状态添加紧凑、移动端可读的样式。
- Modify: `tests/test_database_migrations.py` — 验证旧库迁移后存在新增来源列。
- Modify: `tests/test_source_catalog.py` — 验证 70 条不重复目录、分层数量、默认开关及既有来源迁移。
- Create: `tests/test_source_library.py` — 验证监控状态文案与禁止自动采集的规则。
- Modify: `tests/test_real_collection.py` — 验证 B/C/D 层即使误设开关也不会进入每日采集。
- Modify: `tests/test_source_monitor.py` — 验证来源页显示分层与安全边界。
- Modify: `README.md` — 增加来源库 v2 说明和单来源启用流程。
- Modify: `项目管理/项目推进台账.xlsx` — 新增 v2 来源库完成记录。

## Task 1: 建立来源库数据模型与 70 条安全目录

**Files:**
- Modify: `app/models.py`
- Modify: `app/database.py`
- Modify: `app/sources/catalog.py`
- Modify: `tests/test_database_migrations.py`
- Modify: `tests/test_source_catalog.py`

**Interfaces:**
- Consumes: `Source` 既有的 `is_enabled`、`adapter_key` 与 `scope_group` 字段。
- Produces: `Source.library_tier: str`、`Source.student_value_score: int`、`Source.adaptation_status: str`、`Source.next_action: str`、`Source.official_career_url: str`、`Source.last_monitor_summary: str`。
- Produces: `OFFICIAL_SOURCE_CATALOG: tuple[OfficialSourceDefinition, ...]`，长度为 70。

- [ ] **Step 1: 写失败测试，锁定 70 条目录和默认安全状态**

在 `tests/test_source_catalog.py` 加入：

```python
def test_v2_catalog_has_seventy_unique_sources_with_only_four_auto_collectors():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        ensure_official_source_catalog(session)
        sources = session.query(Source).all()

    assert len(OFFICIAL_SOURCE_CATALOG) == 70
    assert len({source.name for source in OFFICIAL_SOURCE_CATALOG}) == 70
    assert len(sources) == 70
    assert {source.library_tier for source in sources} == {"A", "B", "C", "D"}
    assert len([source for source in sources if source.library_tier == "A" and source.is_enabled]) == 4
    assert all(not source.is_enabled for source in sources if source.library_tier != "A")
    assert {source.name for source in sources if source.library_tier == "B"} >= {
        "上海银行官方招聘（待专用适配）",
        "上海浦东发展银行官方招聘（待专用适配）",
        "上海农村商业银行官方招聘（待专用适配）",
        "国泰海通证券官方招聘（待专用适配）",
    }
```

在 `tests/test_database_migrations.py` 加入旧 SQLite 库迁移断言：

```python
assert {"library_tier", "student_value_score", "adaptation_status", "next_action", "official_career_url", "last_monitor_summary"} <= source_columns
```

- [ ] **Step 2: 运行目标测试，确认其失败**

Run: `python -m pytest tests/test_source_catalog.py tests/test_database_migrations.py -q`  
Expected: FAIL，因为 `Source` 没有 `library_tier` 且目录仍是 10 条。

- [ ] **Step 3: 为 `Source` 追加可迁移字段**

在 `app/models.py` 的 `Source` 类中、`scope_group` 后增加：

```python
library_tier: Mapped[str] = mapped_column(String(1), default="D", nullable=False)
student_value_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
adaptation_status: Mapped[str] = mapped_column(String(30), default="观察中", nullable=False)
next_action: Mapped[str] = mapped_column(String(160), default="等待人工复查", nullable=False)
official_career_url: Mapped[str] = mapped_column(String(500), default="", nullable=False)
last_monitor_summary: Mapped[str] = mapped_column(Text, default="等待首次检查", nullable=False)
```

在 `app/database.py` 的 SQLite 前向迁移映射中，增加与模型一致的 `ALTER TABLE sources ADD COLUMN` 定义；字符串字段使用 `TEXT NOT NULL DEFAULT ''`，分值字段使用 `INTEGER NOT NULL DEFAULT 0`，并在迁移后为旧来源补默认值 `library_tier='D'`、`adaptation_status='观察中'`、`next_action='等待人工复查'`、`last_monitor_summary='等待首次检查'`。

- [ ] **Step 4: 扩展目录定义并录入完整 70 家来源**

将 `OfficialSourceDefinition` 扩展为：

```python
@dataclass(frozen=True)
class OfficialSourceDefinition:
    name: str
    url: str
    level: str
    source_type: str
    adapter_key: str
    scope_group: str
    is_enabled: bool
    library_tier: str
    student_value_score: int
    adaptation_status: str
    next_action: str
```

目录必须精确包含设计文档列出的 4 个 A 类、26 个 B 类、20 个 C 类和 20 个 D 类来源。采用下列固定状态映射：

```python
TIER_DEFAULTS = {
    "A": (True, "已自动采集", "保持定时采集并人工核验"),
    "B": (False, "待专用适配", "完成公开页面试跑与正文清洗"),
    "C": (False, "重点监控", "检查官网招聘页是否更新"),
    "D": (False, "观察中", "在招聘季进行官方入口复查"),
}
```

为每个条目填写官方招聘入口；找不到稳定官方入口时，保留企业官网招聘页而不是第三方投递链接。A 类分值为 90–95，B 类为 72–88，C 类为 65–80，D 类为 55–75。将现有“花旗官方招聘”更新为 C 类 `重点监控`，不新增重复记录。

在 `ensure_official_source_catalog` 中同步所有目录字段，但只保留运营人员已有的 `status`、失败次数、暂停原因和采集成功时间。同步时强制将 B/C/D 的 `is_enabled=False`；仅 A 类可保留 `is_enabled=True`。

- [ ] **Step 5: 运行目标测试，确认通过**

Run: `python -m pytest tests/test_source_catalog.py tests/test_database_migrations.py -q`  
Expected: PASS。

## Task 2: 把每日采集锁死在 A 类并提供内部监控规则

**Files:**
- Create: `app/services/source_library.py`
- Modify: `app/services/real_collection.py`
- Modify: `app/main.py`
- Create: `tests/test_source_library.py`
- Modify: `tests/test_real_collection.py`

**Interfaces:**
- Consumes: `Source.library_tier`、`Source.is_enabled`、`Source.adaptation_status`。
- Produces: `can_auto_collect(source: Source) -> bool`、`monitoring_message(source: Source) -> str`、`record_monitor_check(source: Source, summary: str, checked_at: datetime) -> None`。
- Produces: 每日采集查询只会选取 `library_tier == "A"` 与 `is_enabled is True` 的来源。

- [ ] **Step 1: 写失败测试，验证非 A 类无法被采集**

在 `tests/test_source_library.py` 创建：

```python
def test_only_enabled_a_tier_source_can_auto_collect():
    source = Source(name="B层测试", url="https://example.com", level="一级", source_type="企业官网", library_tier="B", is_enabled=True)
    assert can_auto_collect(source) is False


def test_monitoring_message_never_promises_job_collection_for_c_or_d_tier():
    source = Source(name="观察测试", url="https://example.com", level="一级", source_type="企业官网", library_tier="D", adaptation_status="观察中")
    assert "不抓取岗位" in monitoring_message(source)
```

在 `tests/test_real_collection.py` 创建一个 `library_tier="B", is_enabled=True` 的来源，运行 `collect_due_sources(..., force=True)` 后断言其不计入 `attempted_sources`。

- [ ] **Step 2: 运行目标测试，确认其失败**

Run: `python -m pytest tests/test_source_library.py tests/test_real_collection.py -q`  
Expected: FAIL，因为服务模块和 A 层过滤尚不存在。

- [ ] **Step 3: 实现来源层级服务**

创建 `app/services/source_library.py`：

```python
from datetime import datetime
from app.models import Source


def can_auto_collect(source: Source) -> bool:
    return source.library_tier == "A" and source.is_enabled and source.adaptation_status == "已自动采集"


def monitoring_message(source: Source) -> str:
    if source.library_tier == "A":
        return "已验证来源：采集结果仍须人工核验。"
    if source.library_tier == "B":
        return "待专用适配：不参与每日采集。"
    if source.library_tier == "C":
        return "重点监控：仅记录官网变化，不抓取岗位。"
    return "观察库：仅保留官方入口与招聘季信息，不抓取岗位。"


def record_monitor_check(source: Source, summary: str, checked_at: datetime) -> None:
    source.last_checked_at = checked_at
    source.last_monitor_summary = summary[:300] or "未发现可确认的变化"
```

将 `collect_due_sources` 的来源查询和循环改为使用 `can_auto_collect(source)`，而不是只依赖 `is_enabled`。当 B/C/D 被错误设置为开启时，写一条 `TaskRun` 说明“已跳过非 A 层来源”，但不得请求该网站。

将 `resume_source` 限制为只恢复健康状态；对于非 A 层来源，恢复后仍保持 `is_enabled=False`。

- [ ] **Step 4: 运行目标测试，确认通过**

Run: `python -m pytest tests/test_source_library.py tests/test_real_collection.py -q`  
Expected: PASS。

## Task 3: 重做来源监控页，让分层逻辑对运营人员可见

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/sources.html`
- Modify: `app/static/app.css`
- Modify: `tests/test_source_monitor.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes: `monitoring_message(source)` 与每条 `Source` 的 v2 字段。
- Produces: `/sources` 显示总计、A/B/C/D 分层数量、每条来源的动作边界与安全说明。

- [ ] **Step 1: 写失败页面测试**

在 `tests/test_source_monitor.py` 增加：

```python
def test_sources_page_shows_v2_library_tiers_and_safe_boundaries():
    response = TestClient(create_app("sqlite+pysqlite:///:memory:")).get("/sources")
    assert response.status_code == 200
    for label in ("70 家分层来源库", "已验证自动抓取", "核心专用适配库", "重点监控库", "观察库", "学生价值分", "不会自动抓取"):
        assert label in response.text
    assert "上海银行官方招聘（待专用适配）" in response.text
    assert "西门子中国官方招聘（观察库）" in response.text
```

- [ ] **Step 2: 运行目标测试，确认其失败**

Run: `python -m pytest tests/test_source_monitor.py tests/test_web.py -q`  
Expected: FAIL，因为现有页面没有 v2 分层区域。

- [ ] **Step 3: 更新路由、模板和样式**

在 `/sources` 路由中生成：

```python
tier_summaries = {
    tier: sum(1 for source in source_records if source.library_tier == tier)
    for tier in ("A", "B", "C", "D")
}
```

向模板传入 `tier_summaries` 和 `{source.id: monitoring_message(source)}`。在表格前增加四张紧凑统计卡片，并在页面顶部固定说明：

```text
70 家分层来源库：目前只有 A 类 4 家已验证官方来源参与每日采集。B/C/D 类不会自动抓取、更不会自动发布。
```

表格列改为“来源 / 层级与适配状态 / 学生价值与范围 / 健康与最近检查 / 下一步动作 / 操作”。对 A、B、C、D 加 `tier-badge tier-a` 等 CSS 类；移动端表格不隐藏来源名、层级或下一步动作。

- [ ] **Step 4: 运行目标测试，确认通过**

Run: `python -m pytest tests/test_source_monitor.py tests/test_web.py -q`  
Expected: PASS。

## Task 4: 文档、项目台账和端到端验证

**Files:**
- Modify: `README.md`
- Modify: `项目管理/项目推进台账.xlsx`
- Modify: `tests/test_source_catalog.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Consumes: 已完成的 70 条目录、来源页和每日采集安全过滤。
- Produces: 合作演示可说明的来源库 v2 操作规则，以及可追溯的项目更新记录。

- [ ] **Step 1: 写失败测试，验证 README 的关键使用边界**

在 `tests/test_web.py` 增加文件内容断言：

```python
def test_readme_documents_v2_source_library_and_manual_review_boundary():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "70 家分层来源库" in readme
    assert "只有 A 类" in readme
    assert "人工审核" in readme
```

- [ ] **Step 2: 运行目标测试，确认其失败**

Run: `python -m pytest tests/test_web.py::test_readme_documents_v2_source_library_and_manual_review_boundary -q`  
Expected: FAIL，因为 README 尚未记录 v2 来源库。

- [ ] **Step 3: 更新 README 与 Excel 台账**

在 `README.md` 的采集说明后加入“70 家分层来源库 v2”：说明 A=4 家自动采集、B=26 家待专用适配、C=20 家重点监控、D=20 家观察；说明单来源需三次公开试跑成功、正文清洗合格并人工确认后才可从 B 升到 A。

使用工作区的电子表格工具打开 `项目管理/项目推进台账.xlsx`，新增一行：日期为实施当天；模块为“来源库与采集治理”；版本为“v2.0”；状态为“已完成”；更新摘要为“建立70家分层预抓取库：4家自动采集、26家待专用适配、20家重点监控、20家观察库；新增学生价值分与非A层禁止自动采集规则。”；验证列填写“目录70条去重、A层过滤测试、全量pytest通过、来源页人工检查”。保存到原文件，并导出或渲染一张检查图确认列宽与文字可读。

- [ ] **Step 4: 运行全量验证**

Run: `python -m pytest -q`  
Expected: PASS，且测试数量不少于改动前的 64 项。

Run: `powershell -NoProfile -Command "& .\\run_local.ps1"`  
Expected: 本地服务可启动且无数据库迁移错误。

在浏览器打开 `http://127.0.0.1:8000/sources`，人工确认：70 条来源可见、4 张层级统计正确、上海银行显示“待专用适配”、西门子显示“观察库”、运行每日采集不会尝试 B/C/D 来源。

