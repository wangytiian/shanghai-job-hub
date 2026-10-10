# B 级岗位人工筛选池 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让有完整岗位事实、但学生可投资格待人工确认的真实线索进入 B 级人工筛选池，并对现有符合条件的 C 级线索安全重判。

**Architecture:** 保持来源等级与岗位等级分离。现有 `review_only` 候选由 C 改存 B，但保留 `REVIEW_NOTE`；发布与最终审核在该标记被人工留证清除前拒绝。历史数据通过独立、可预览、带审计且幂等的服务迁移，不依赖复采触发。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy、SQLite、pytest、Jinja2。

## Global Constraints

- 不改变 `Source.library_tier`、B 来源试采白名单和 A 来源每日调度。
- B 是人工筛选线索，不是可发布岗位；带 `TARGET_AUDIENCE_UNCLEAR` 标记时最终审核与分发均应拒绝。
- 历史重判只处理真实、待核验、已确认为新招聘、未截止且事实版本可追溯的 `REVIEW_NOTE` C；不重判已人工处理、待判断、其他 C 原因或 D。
- 保留原有 `REVIEW_NOTE` 文案和历史精确匹配；不清除原文、来源、岗位记录或已有用户修改。
- 每项改动先写失败测试，看到预期失败后写最少实现并重新运行测试。

---

### Task 1: B 级规则和采集映射

**Files:**
- Modify: `app/services/intake_screening.py:25-36`
- Modify: `app/services/real_collection.py:204-246,285-298`
- Test: `tests/test_intake_screening.py`, `tests/test_sbs_integration.py`, `tests/test_sspu_collection.py`, `tests/test_b_pilot_intake.py`

**Interfaces:**
- Consumes: `evaluate_review_intake(...) -> CandidateDecision`，其中仅缺 `TARGET_AUDIENCE_UNCLEAR` 为 `review_only`。
- Produces: `screen_intake(...) -> IntakeScreeningResult`；`_save_detail(..., review_note=REVIEW_NOTE)` 保存 B／待确认标记。

- [ ] **Step 1: Write failing tests.** 在 `test_intake_screening.py` 添加一条岗位级本科/具体职责/投递邮箱且无学生字样的 B 测试、一条只有“请看附件”的 C 测试；将 SBS/SSPU/B 试点中 `review_only` 新入库断言改为 `B, REVIEW_NOTE, 待核验`。示例：

```python
def test_complete_junior_role_without_student_statement_is_b():
    result = screen_intake(
        "策划专员",
        "岗位职责：负责活动策划。任职要求：本科，相关经验者优先。"
        "工作地点：上海。官方投递邮箱：jobs@example.com",
    )
    assert (result.grade, result.route) == ("B", "普通待核验")

def test_notice_without_role_or_application_stays_c():
    assert screen_intake("招聘公告", "请查看附件了解具体岗位安排。").grade == "C"
```

- [ ] **Step 2: Verify RED.** Run `python -m pytest tests/test_intake_screening.py tests/test_sbs_integration.py tests/test_sspu_collection.py tests/test_b_pilot_intake.py -q`; expected failures must be the new B expectations, not import or fixture errors.
- [ ] **Step 3: Implement minimal rules.** Keep hard filters and A precedence. B requires a specific role title, job-specific duty/requirement text, junior/degree evidence, and explicit application evidence; keep the existing social-recruitment B route only when these basic facts exist. In `_save_detail`, use `intake_grade="B" if review_note else intake.grade`, `intake_route="普通待核验" if review_note else intake.route`; update the changed-content branch likewise. No other candidate verdict may enter B by this mapping.
- [ ] **Step 4: Verify GREEN.** Re-run the exact focused command and inspect changed C expectations; add a negative test if a generic recruitment notice or a different-position paragraph incorrectly becomes B.
- [ ] **Step 5: Review diff.** Run `git diff -- app/services/intake_screening.py app/services/real_collection.py tests/test_intake_screening.py tests/test_sbs_integration.py tests/test_sspu_collection.py tests/test_b_pilot_intake.py`; preserve unrelated existing changes in these dirty files.

### Task 2: 未确认 B 的发布硬拦截与人工证据确认

**Files:**
- Modify: `app/services/jobs.py:40-70`
- Modify: `app/services/intake_promotion.py:33-155`
- Modify: `app/templates/job_detail.html:24-35`
- Modify: `app/main.py:1233-1268`
- Test: `tests/test_jobs.py`, `tests/test_reviews.py`, `tests/test_intake_promotion.py`, `tests/test_b_pilot_web.py`

**Interfaces:**
- Consumes: `REVIEW_NOTE` / `[TARGET_AUDIENCE_UNCLEAR]` 存储标记；`promote_review_only_job(...)` 保持现有签名、权限与版本校验。
- Produces: C→B 和 B→B（确认受众）均清除标记并写双审计，但仍保持 `status=待核验`。

- [ ] **Step 1: Write failing tests.** 添加 `validate_publishable` 对带标记 B 的拒绝测试，以及审核员对 `B + REVIEW_NOTE` 提交官方 URL、原文摘录、说明后的留档测试。缺字段、过期、旧版本、非审核员仍拒绝。示例：

```python
def test_audience_unconfirmed_b_cannot_be_published(session):
    job = _job(session, intake_grade="B", intake_reason=REVIEW_NOTE)
    assert any("受众" in error for error in validate_publishable(job))

def test_reviewer_can_confirm_pending_b_without_publishing(session):
    reviewer = _user(session)
    job = _job(session, intake_grade="B", intake_reason=REVIEW_NOTE)
    result = _promote(session, job, reviewer.id)
    assert (result.intake_grade, result.status) == ("B", "待核验")
    assert "TARGET_AUDIENCE_UNCLEAR" not in result.intake_reason
    assert result.verification_checks == "{}"
```

- [ ] **Step 2: Verify RED.** Run `python -m pytest tests/test_jobs.py tests/test_reviews.py tests/test_intake_promotion.py tests/test_b_pilot_web.py -q`; confirm only the new B behavior fails.
- [ ] **Step 3: Implement minimal guard and confirmation.** In `validate_publishable`, append `"学生受众尚未人工确认"` if `"[TARGET_AUDIENCE_UNCLEAR]" in job.intake_reason` before existing checks. In `promote_review_only_job`, capture `old_grade = job.intake_grade`, accept only `old_grade in {"B", "C"} and job.intake_reason == REVIEW_NOTE`, compare-and-swap on `old_grade`, write `new_grade="B"`, and use `"入库升档：C→B"` or `"受众确认：B"` in `ReviewLog`. Preserve existing resets, audit transaction, actor check and fingerprint/version gates. The detail form is available for either B or C with the marker and says “确认受众，仍待完整核验”; web feedback distinguishes B→B from C→B.
- [ ] **Step 4: Verify GREEN.** Run focused tests. Also run `python -m pytest tests/test_distribution.py tests/test_intake_promotion.py -q` to prove publication is still gated.
- [ ] **Step 5: Review diff.** Check only targeted branches changed; `REVIEW_NOTE` constant must remain byte-for-byte compatible with stored data.

### Task 3: 历史 C 预览、在线备份与有界重判

**Files:**
- Create: `app/services/b_intake_regrade.py`
- Create: `scripts/regrade_b_intake.py`
- Test: `tests/test_b_intake_regrade.py`

**Interfaces:**
- Consumes: `Job`, `REVIEW_NOTE`, `evaluate_review_intake`, `candidate_from_detail`, `record_event`；不调用采集网络。
- Produces: `preview_b_regrade(session, now) -> list[dict]`（ID、决定、原因），`apply_b_regrade(session, now, ids) -> int`（仅显式预览 ID、事务内重判、幂等）。

- [ ] **Step 1: Write failing tests.** 创建 1 条完整且当前 `REVIEW_NOTE` C 岗位、1 条已截止、1 条 `notice_type=待判断`、1 条无投递路径、1 条非 `REVIEW_NOTE` C。断言预览只给第一条 `eligible`，执行后仅其变 B、标记保留、分数/核验清空、版本递增、`ReviewLog` 与 `AuditEvent` 各新增一条，二次执行 0 条。
- [ ] **Step 2: Verify RED.** Run `python -m pytest tests/test_b_intake_regrade.py -q`; expected failure is missing service or incorrect eligibility.
- [ ] **Step 3: Implement service.** 先按 `Job.intake_grade == "C"`、`Job.intake_reason == REVIEW_NOTE` 找行；逐条检查真实、待核验、新招聘、未截止、版本指纹、来源/正文/上海地点/投递证据。用存储原文构造 `SourceCandidate` 并要求 `evaluate_review_intake(...).verdict == "review_only"`；在执行前重新预览所选 ID。更新 `grade=B, route=普通待核验`，保留 `REVIEW_NOTE`，清零旧分数/核验、增加 `version/row_version`，同事务记录两种审计。不处理未选 ID。
- [ ] **Step 4: Implement script with safe mode.** 默认只打印 JSON 预览。`--apply --ids 108,109` 才连接写事务；脚本先用 SQLite online backup API 创建唯一时间戳备份到项目 `backups`（验证路径在项目下且新文件不存在），再调用服务。脚本输出 eligible、skipped、applied、backup path；没有候选时不硬造 B。
- [ ] **Step 5: Verify GREEN.** Run `python -m pytest tests/test_b_intake_regrade.py -q` and script `python scripts/regrade_b_intake.py`（默认只读）两次，结果稳定。不要在单元测试中写真实数据库。

### Task 4: 真实数据执行与回归验收

**Files:**
- Create: `docs/sources/2026-10-10-b-intake-regrade-report.md`
- Data: `data/recruiting_local.db`（仅经备份后的有界迁移）
- Test: 全套 `tests/`

**Interfaces:**
- Consumes: Task 3 脚本的只读预览和显式 ID；不改变来源调度。
- Produces: B>0 的真实待筛线索、每条迁移审计、可恢复备份和数量报告。

- [ ] **Step 1: Read-only preview.** Run `python scripts/regrade_b_intake.py`;人工检查建议 ID 的原文、来源、工作地点、专业方向、期限和风险。不得把已截止、异地、资深或公告未判的新记录写 B。
- [ ] **Step 2: Focused live verification.** 对选中岗位读官方原文（若网站暂时不可用，跳过该 ID），记录 `source_url`、证据、检查时间；不假定旧数据库字段就等于当前仍开放。
- [ ] **Step 3: Backup and apply.** 若 `108`、`109` 两条均通过 Step 2 的当前官网验证，运行 `python scripts/regrade_b_intake.py --apply --ids 108,109` 一次；任一条未通过则只提交通过验证的 ID，若都未通过则本步不写数据库。脚本先做 SQLite online backup；检查备份完整性和 applied ID。
- [ ] **Step 4: Regression.** Run `python -m pytest -q` and `python -c 'import sqlite3; c=sqlite3.connect("file:data/recruiting_local.db?mode=ro",uri=True); print(c.execute("SELECT intake_grade,COUNT(*) FROM jobs WHERE is_demo=0 GROUP BY intake_grade ORDER BY intake_grade").fetchall())'`. Expected: tests pass; B count >0;迁移记录均仍为待核验、带受众待确认标记，审计可追溯。
- [ ] **Step 5: Report.** 在报告中列明改动、预览/跳过、备份路径、B 实际数量、测试结果、未解决的人工核验事项。若外部网站无法验证则保留备份与代码，但不强行为历史记录升档。

## Self-review

- 范围只覆盖岗位 B 人工池、发布隔离及现有 C 的可审计重判；不扩张抓取源。
- 旧 `REVIEW_NOTE` 字面值与来源调度保持不变；`C→B` 和 `B→B` 都需人工证据，自动 B 仍无法发布。
- Task 4 的“B>0”以真实可验证岗位为前提，不以强行改等级满足数字目标。
