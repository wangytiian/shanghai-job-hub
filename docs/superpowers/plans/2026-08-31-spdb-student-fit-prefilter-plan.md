# 浦发上海岗位学生适配预筛 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在浦发上海官方社会招聘采集前，以可解释规则排除明显不适合学生的成熟社招岗位。

**Architecture:** 在`app.sources.spdb`的官方详情抓取阶段，根据详情页`hrsJobRequire`执行纯确定性预筛；符合过滤条件的岗位不进入`SpdbDetail`。采集服务统计预筛结果并写入来源运行摘要，岗位状态与人工审核流程保持不变。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、pytest、httpx。

## Global Constraints

- 只使用官方详情页已出现的文字，不由模型推断学历、经验或应届资格。
- 只过滤明确“三年及以上/五年及以上经验”且不存在应届开放信号的岗位。
- 经验不明及一至三年经验岗位保留给人工审核。
- 不自动发布、不删除历史岗位、不弱化 HTTPS 校验。

---

### Task 1: 详情页学生适配预筛

**Files:**
- Modify: `app/sources/spdb.py`
- Test: `tests/test_spdb_source.py`

**Interfaces:**
- Consumes: `fetch_spdb_shanghai_job_details(client, limit, today)`与官网字段`hrsJobRequire`。
- Produces: `is_spdb_student_fit(requirement: str) -> tuple[bool, str]`；只有返回`True`的详情进入`list[SpdbDetail]`。

- [ ] **Step 1: Write the failing test**

```python
def test_spdb_student_fit_keeps_explicit_graduate_and_uncertain_roles():
    from app.sources.spdb import is_spdb_student_fit

    assert is_spdb_student_fit("三年以上银行从业经验")[0] is False
    assert is_spdb_student_fit("三年以上经验，优秀应届生可投")[0] is True
    assert is_spdb_student_fit("1-3年相关经验")[0] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_spdb_source.py::test_spdb_student_fit_keeps_explicit_graduate_and_uncertain_roles -q`

Expected: FAIL because `is_spdb_student_fit` does not yet exist.

- [ ] **Step 3: Write minimal implementation**

```python
GRADUATE_SIGNAL_PATTERN = re.compile(r"应届|毕业生|校招|校园招聘|20(24|25|26)届")
MATURE_EXPERIENCE_PATTERN = re.compile(
    r"(?:3|三|5|五)\s*(?:年|年以上|年及以上).{0,12}(?:工作|从业)?经验|"
    r"(?:工作|从业)经验.{0,12}(?:3|三|5|五)\s*(?:年|年以上|年及以上)"
)

def is_spdb_student_fit(requirement: str) -> tuple[bool, str]:
    text = requirement.strip()
    if GRADUATE_SIGNAL_PATTERN.search(text):
        return True, "含应届生开放信号"
    if MATURE_EXPERIENCE_PATTERN.search(text):
        return False, "明确三年及以上经验且未见应届生开放信号"
    return True, "经验要求未触发预筛"
```

在详情抓取后、创建`SpdbDetail`前调用函数；不适配岗位跳过，保留计数和原因。

- [ ] **Step 4: Run focused and full tests**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_spdb_source.py -q`，再运行`.\\.venv\\Scripts\\python.exe -m pytest -q`。

Expected: 全部通过。

### Task 2: 采集结果可观察性

**Files:**
- Modify: `app/sources/spdb.py`
- Modify: `app/services/real_collection.py`
- Test: `tests/test_spdb_source.py`

**Interfaces:**
- Consumes: 任务 1 返回的过滤原因。
- Produces: `SpdbFetchResult(details: list[SpdbDetail], filtered_count: int, filter_reasons: list[str])`，并将汇总写入`Source.last_monitor_summary`。

- [ ] **Step 1: Write the failing test**

```python
def test_spdb_fetch_reports_student_fit_filtered_count():
    from app.sources.spdb import fetch_spdb_shanghai_job_details

    result = fetch_spdb_shanghai_job_details(FakeSpdbClientWithSeniorExperience(), limit=10, today=date(2026, 8, 31))

    assert result.filtered_count == 1
    assert result.filter_reasons == ["明确三年及以上经验且未见应届生开放信号"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_spdb_source.py::test_spdb_fetch_reports_student_fit_filtered_count -q`

Expected: FAIL because the current fetch function returns a plain list.

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass(frozen=True)
class SpdbFetchResult:
    details: list[SpdbDetail]
    filtered_count: int
    filter_reasons: list[str]
```

让抓取函数返回`SpdbFetchResult`，并在`collect_spdb_shanghai_jobs`中循环`result.details`；采集完成后写入：

```python
source.last_monitor_summary = (
    f"浦发上海官方采集：创建 {result.created_jobs} 条，更新 {result.updated_jobs} 条，"
    f"学生适配预筛过滤 {fetched.filtered_count} 条"
)
```

- [ ] **Step 4: Run focused and full tests**

Run: `.\\.venv\\Scripts\\python.exe -m pytest tests\\test_spdb_source.py -q`，再运行`.\\.venv\\Scripts\\python.exe -m pytest -q`。

Expected: 全部通过且来源对象摘要含“学生适配预筛过滤”。

### Task 3: 本地运行与项目台账

**Files:**
- Modify: `项目管理/项目推进台账.xlsx`

**Interfaces:**
- Consumes: 全部测试通过的实现和本地真实来源试跑结果。
- Produces: 台账新增一条可追溯更新说明。

- [ ] **Step 1: 运行受控真实来源试跑**

Run: 通过本地来源任务调用浦发适配器，只创建待核验记录，不发布任何内容。

- [ ] **Step 2: 验收页面**

Run: 打开`http://127.0.0.1:8000/sources`，确认浦发来源摘要展示创建、更新和预筛过滤数量；打开岗位列表确认新增项目均为`待核验`。

- [ ] **Step 3: 更新 Excel 台账**

新增“浦发上海岗位学生适配预筛”记录，写明过滤范围、保留边界、测试与试跑结果。
