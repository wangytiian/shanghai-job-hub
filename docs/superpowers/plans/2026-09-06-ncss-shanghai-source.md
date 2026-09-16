# 国家大学生就业服务平台上海岗位接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not use subagents for this plan.

**Goal:** 将国家大学生就业服务平台的公开上海在投递岗位安全采集进现有待核验池。

**Architecture:** 新建一个只处理 NCSS 公开 JSON 列表和公开 HTML 详情的适配器。采集服务复用已有 `_save_detail`、内容版本、过期阻断、人工审核与发布边界；目录先以禁用 B 类登记专用适配键，隔离试采通过后再升为 A 类并启用，不会添加登录、投递或新的后台服务。

**Tech Stack:** Python 3.12、httpx、BeautifulSoup、SQLAlchemy、pytest；使用项目 `.venv`。

**实施状态：** 解析器、禁用目录登记、待核验入库链路、离线测试和容器化回归已完成。2026-09-06 真实隔离试采未取得可保存正文，未满足升级 A 类的验收条件；来源保持 B 类禁用，不参与每日采集。

## Global Constraints

- 只请求 `https://www.ncss.cn/student/jobs/jobslist/ajax/` 与其公开 `/student/jobs/{jobId}/detail.html` 页面，不登录、不投递、不调用收藏/举报/个人资料接口。
- 列表参数固定 `areaCode=310000`，每轮至多 3 页、30 条；详情请求超时 12 秒、相邻请求至少间隔 0.5 秒。
- 详情必须有可提取正文、有“投递简历”、且无“职位已下线”才可进入待核验池。
- 平台地点筛选或企业名称不能替代详情证据；地点不明确或矛盾时保留待人工核验。
- 新记录绝不自动审核、发布、发送公众号或代表用户投递。
- 工作区为用户的脏工作树：不重置、不覆盖无关改动、不自动提交或推送。

---

## File structure

| 文件 | 职责 |
|---|---|
| `app/sources/ncss.py` | 公共列表/详情请求、JSON/HTML 解析及“仍可投递”门槛 |
| `app/sources/catalog.py` | 注册 A 类 NCSS 上海来源与专用适配键 |
| `app/services/real_collection.py` | 将 NCSS 详情交给既有 `_save_detail`，登记来源级状态与 TaskRun |
| `tests/fixtures/sources/ncss/*.json|html` | 最小、脱敏的列表、可投递详情、下线详情样本 |
| `tests/test_ncss_source.py` | 适配器解析、分页、下线和内容拒绝测试 |
| `tests/test_real_collection.py` | 采集去重、更新、过期/下线安全边界测试 |
| `tests/test_source_catalog.py` | 目录初始化、A 类启用与幂等性 |
| `docs/sources/2026-09-06-ncss-trial-report.md` | 真实隔离试采的可复核记录 |

### Task 1: 定义离线适配器契约与样本

**Files:**
- Create: `tests/fixtures/sources/ncss/shanghai-list.json`
- Create: `tests/fixtures/sources/ncss/active-detail.html`
- Create: `tests/fixtures/sources/ncss/offline-detail.html`
- Create: `tests/test_ncss_source.py`

**Interfaces:**
- Produces: `NcssListing` 与 `NcssDetail` 的预期字段，供 Task 2 实现。

- [ ] **Step 1: 写入最小脱敏样本。**

列表样本必须包含两个 `areaCodeName="上海"` 记录、一个非上海记录、两个不同 `jobId`，以及 `pagenation`。可投递详情样本包含标题、企业名、上海地点、正文和“投递简历”；下线样本包含“职位已下线”。样本中不放 Cookie、CSRF、手机号、邮箱或真实个人资料。

- [ ] **Step 2: 写第一个失败测试。**

```python
from pathlib import Path

from app.sources.ncss import parse_ncss_list


FIXTURES = Path(__file__).parent / "fixtures" / "sources" / "ncss"


def test_parse_ncss_list_keeps_only_shanghai_records_and_preserves_identity():
    listings = parse_ncss_list((FIXTURES / "shanghai-list.json").read_text(encoding="utf-8"))

    assert [listing.job_id for listing in listings] == ["job-a", "job-b"]
    assert listings[0].title == "AI 产品运营实习生"
    assert listings[0].employer_name == "上海示例科技有限公司"
    assert listings[0].detail_url.endswith("/student/jobs/job-a/detail.html")
```

- [ ] **Step 3: 运行失败测试。**

Run: `& .\.venv\Scripts\python.exe -m pytest -q tests\test_ncss_source.py::test_parse_ncss_list_keeps_only_shanghai_records_and_preserves_identity`  
Expected: FAIL，原因是 `app.sources.ncss` 尚不存在。

- [ ] **Step 4: 为详情状态补充失败测试。**

```python
import pytest

from app.sources.ncss import NcssListing, parse_ncss_detail


def test_parse_ncss_detail_accepts_only_public_active_job():
    listing = NcssListing("job-a", "AI 产品运营实习生", "上海示例科技有限公司", "上海市学生事务中心", "2026-09-06", "https://www.ncss.cn/student/jobs/job-a/detail.html")
    detail = parse_ncss_detail((FIXTURES / "active-detail.html").read_text(encoding="utf-8"), listing)

    assert detail.identity_key == "job-a"
    assert detail.location_category == "上海"
    assert "岗位职责" in detail.evidence_text


def test_parse_ncss_detail_rejects_offline_job():
    listing = NcssListing("job-offline", "过期岗位", "上海示例科技有限公司", "上海市学生事务中心", "2026-09-06", "https://www.ncss.cn/student/jobs/job-offline/detail.html")

    with pytest.raises(ValueError, match="已下线"):
        parse_ncss_detail((FIXTURES / "offline-detail.html").read_text(encoding="utf-8"), listing)
```

- [ ] **Step 5: 运行两个详情测试。**

Run: `& .\.venv\Scripts\python.exe -m pytest -q tests\test_ncss_source.py -k detail`  
Expected: FAIL，原因是 `NcssListing`、`parse_ncss_detail` 尚不存在。

### Task 2: 实现 NCSS 公共读取与解析器

**Files:**
- Create: `app/sources/ncss.py`
- Modify: `tests/test_ncss_source.py`

**Interfaces:**
- Consumes: Task 1 的 JSON/HTML 样本与断言。
- Produces:

```python
NCSS_LIST_URL = "https://www.ncss.cn/student/jobs/jobslist/ajax/"

@dataclass(frozen=True)
class NcssListing:
    job_id: str
    title: str
    employer_name: str
    source_name: str
    published_at: str
    detail_url: str

@dataclass(frozen=True)
class NcssDetail:
    title: str
    published_at: str
    detail_url: str
    evidence_text: str
    identity_key: str
    employer_name: str
    location_category: str
    location_detail: str
    recruitment_type: str = "待核验"
    official_url: str = ""

def parse_ncss_list(payload: str) -> list[NcssListing]: ...
def parse_ncss_detail(html: str, listing: NcssListing) -> NcssDetail: ...
def fetch_ncss_shanghai_listings(client, pages: int = 3) -> list[NcssListing]: ...
def fetch_ncss_detail(client, listing: NcssListing) -> NcssDetail: ...
```

- [ ] **Step 1: 实现最小 JSON 解析。**

`parse_ncss_list` 使用 `json.loads`，只从 `data.list` 读取字典；仅保留 `areaCodeName == "上海"`、有 `jobId`、`jobName`、`recName` 的记录，按 `jobId` 去重。`published_at` 由毫秒时间戳转为 `%Y-%m-%d`，不能转换时为空字符串。详情 URL 使用固定 `https://www.ncss.cn/student/jobs/{job_id}/detail.html`。

- [ ] **Step 2: 实现最小详情解析。**

使用 `BeautifulSoup` 和现有 `extract_article_text`。若页面包含“职位已下线”、不含“投递简历”、或提取正文少于 20 个字符，抛出 `ValueError`。标题优先页面岗位标题，企业名优先详情企业字段、否则列表企业名；`identity_key` 永远为 `listing.job_id`。地点仅在详情文本明确出现“上海”时写 `location_category="上海"`，否则写 `"原文未明确"`。

- [ ] **Step 3: 实现有界联网读取。**

`fetch_ncss_shanghai_listings` 拒绝不在 1–3 的页数；从第 1 页至目标页按顺序 GET 列表 URL，固定传入：

```python
{"areaCode": "310000", "offset": str(page), "limit": "10", "jobType": "", "jobName": ""}
```

每页使用 12 秒超时并 `raise_for_status()`；空页只结束后续分页，不把它解释为来源无岗位。`fetch_ncss_detail` 用 12 秒 GET 并在成功解析后 `time.sleep(0.5)`。

- [ ] **Step 4: 运行适配器测试。**

Run: `& .\.venv\Scripts\python.exe -m pytest -q tests\test_ncss_source.py`  
Expected: PASS。

- [ ] **Step 5: 添加请求边界测试并重跑。**

用测试客户端记录参数，断言 `pages=4` 抛出 `ValueError`，`pages=2` 只请求 offset 1、2，且 URL 与参数中含 `areaCode=310000`。  
Run: `& .\.venv\Scripts\python.exe -m pytest -q tests\test_ncss_source.py`  
Expected: PASS。

### Task 3: 接入禁用目录和待核验采集链路

**Files:**
- Modify: `app/sources/catalog.py`
- Modify: `app/services/real_collection.py`
- Modify: `tests/test_source_catalog.py`
- Modify: `tests/test_real_collection.py`

**Interfaces:**
- Consumes: `fetch_ncss_shanghai_listings`、`fetch_ncss_detail`、`NcssDetail`。
- Produces: `collect_ncss_shanghai_jobs(session, client, limit_pages=3, now=None, intake_ai_complete=None) -> RealCollectionResult`。

- [ ] **Step 1: 写失败的目录测试。**

```python
def test_catalog_registers_ncss_shanghai_public_jobs_as_disabled_until_trial_acceptance(session):
    ensure_official_source_catalog(session)
    source = session.query(Source).filter_by(name="国家大学生就业服务平台上海岗位").one()

    assert source.library_tier == "B"
    assert source.adapter_key == "ncss_shanghai_jobs"
    assert source.is_enabled is False
    assert source.scope_group == "上海学生就业"
```

Run: `& .\.venv\Scripts\python.exe -m pytest -q tests\test_source_catalog.py -k ncss`  
Expected: FAIL，目录中还没有该来源。

- [ ] **Step 2: 注册来源和采集分发分支。**

在 `OFFICIAL_SOURCE_CATALOG` 的 A 类区新增：

```python
_source(
    "国家大学生就业服务平台上海岗位",
    "https://www.ncss.cn/student/jobs/index.html",
    "上海学生就业",
    "B",
    88,
    adapter_key="ncss_shanghai_jobs",
    source_type="国家公共就业平台",
)
```

在 `collect_due_sources` 的 `adapter_key` 分支中调用 `collect_ncss_shanghai_jobs`。在该函数中按现有中国银行采集模式：获取目录来源、更新 `last_checked_at`、列表为空时抛出来源级异常；逐条详情调用 `_save_detail`；`ValueError` 的“已下线/不可投递/正文”记为单条跳过而非新增；完成后写入 `TaskRun`，成功/失败均复用 `_record_source_success`、`_record_source_failure`。函数可由隔离试采直接调用；B 类禁用状态下不会被每日分发调用。

- [ ] **Step 3: 写失败的采集安全边界测试。**

```python
def test_collect_ncss_saves_active_shanghai_detail_as_pending_verification(session, monkeypatch):
    monkeypatch.setattr("app.services.real_collection.fetch_ncss_shanghai_listings", lambda _client, pages: [listing])
    monkeypatch.setattr("app.services.real_collection.fetch_ncss_detail", lambda _client, _listing: active_detail)

    result = collect_ncss_shanghai_jobs(session, object(), limit_pages=1, now=datetime(2026, 9, 6, 12, 0))

    job = session.query(Job).one()
    assert result.created_jobs == 1
    assert job.status == "待核验"
    assert job.location_category == "上海"
    assert job.fingerprint.startswith("国家大学生就业服务平台上海岗位|job-a|")
```

Run: `& .\.venv\Scripts\python.exe -m pytest -q tests\test_real_collection.py -k ncss`  
Expected: FAIL，因为采集函数尚不存在。

- [ ] **Step 4: 实现最小采集函数并运行聚焦测试。**

实现函数时不创建新的入库路径；每一条成功详情直接调用 `_save_detail`。连续两次相同输入必须由既有 fingerprint 逻辑返回“无变化”。  
Run: `& .\.venv\Scripts\python.exe -m pytest -q tests\test_source_catalog.py -k ncss tests\test_real_collection.py -k ncss`  
Expected: PASS。

- [ ] **Step 5: 增加下线与二次去重测试。**

在相同测试中使 `fetch_ncss_detail` 对一条记录抛 `ValueError("职位已下线")`，断言无 Job 新增；对活跃详情连续调用两次，断言第二次 `created_jobs == 0`、`unchanged_jobs == 1`。  
Run: `& .\.venv\Scripts\python.exe -m pytest -q tests\test_real_collection.py -k ncss`  
Expected: PASS。

### Task 4: 隔离真实试采、报告和全量验证

**Files:**
- Create: `docs/sources/2026-09-06-ncss-trial-report.md`
- Modify: `docs/sources/2026-09-06-validation-report.md`

**Interfaces:**
- Consumes: 已通过的采集器和临时 SQLite 数据库。
- Produces: 可复核的真实试采结果及最终验收状态。

- [ ] **Step 1: 在临时 SQLite 中运行真实试采两次。**

使用 `tempfile.TemporaryDirectory()`、`create_database("sqlite+pysqlite:///...")`、`ensure_official_source_catalog(session)` 与 `httpx.Client(follow_redirects=True, max_redirects=3)`。每次仅传 `limit_pages=1`。不得使用默认数据库 URL，也不得写入正式 `jobs` 表。

- [ ] **Step 2: 核对试采证据。**

记录首轮和次轮的新增、无变化、有更新、单条失败数；查询 Job，确认每条 `status == "待核验"`、`is_demo is False`，且没有自动发布字段变化。若真实页面返回下线/异常，记录事实并保留来源 A 类启用状态仅在至少一个活跃详情成功时成立。

- [ ] **Step 3: 写试采报告。**

报告必须列出检查时间、精确列表 URL、页数、接口公开性、真实结果、下线/跳过数、地点证据、已知限制（第三方同步来源、企业官网尚待人工核验）和下一步运营动作。不得记录岗位申请数据、Cookie、CSRF 或个人信息。

- [ ] **Step 4: 试采验收后晋级来源并更新总验证报告。**

确认至少一个活跃详情在隔离库首次创建、第二次无重复，并且所有聚焦测试通过后，将目录定义改为 A 类；重新初始化目录，确认来源启用、适配键为 `ncss_shanghai_jobs`，且不覆盖人为暂停状态。随后在 `2026-09-06-validation-report.md` 增加 NCSS 的最终状态、来源等级、试采结果和“仅待核验”的边界；原有渠道调查结论保留。

- [ ] **Step 5: 运行完整验证。**

Run: `& .\.venv\Scripts\python.exe -m pytest -q`  
Expected: 全部测试通过。

Run:

```powershell
$env:Path = 'C:\Program Files\Docker\Docker\resources\bin;' + $env:Path
docker compose -f compose.test.yaml build tests
docker compose -f compose.test.yaml run --rm --remove-orphans tests
```

Expected: 容器测试通过；只接受已有平台条件导致的明确 skipped，不接受新的 failures。

- [ ] **Step 6: 重启并检查本地页面。**

仅在确认 8000 端口属于本项目 Uvicorn 进程时重启；随后请求 `http://127.0.0.1:8000/sources`，确认 HTTP 200 且页面显示“国家大学生就业服务平台上海岗位”。

## Plan self-review

- 覆盖了已确认的上海范围、分页上限、详情在投递门槛、待核验、去重、版本、下线阻断、隔离试采、正式验证和页面可见性。
- 未使用 TODO/TBD 或泛化的“适当处理”占位语；所有接口名称在任务中定义。
- 不包含账户、投递、浏览器自动化、全国列表或不受控搜索结果。
