# B 类试点样本入库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让上财招聘、交大实习两个 B 类来源的合格试采样本经管理员显式操作，限量进入真实岗位待核验池；让受众待确认的 C 级线索在有证据的人工复核后升 B，始终保留最终审核和发布边界。

**Architecture:** 保留 B 试采与 A 日常采集两条现有路径；新增一个从持久化试采样本到 `Job` 的受限导入服务和一个 C→B 人工复核服务。网页仅调用服务，所有事实与权限在服务端复核；无需迁移数据库或新增抓取适配器。

**Tech Stack:** Python、FastAPI、SQLAlchemy、SQLite/PostgreSQL、pytest；复用 `SourceTrialRun`、`SourceTrialSample`、`Job`、`ReviewLog`、`AuditEvent`。

## Global Constraints

- 只允许来源键 `sufe-jobs` 与 `sjtu-internships`；`library_tier` 保持 B，`is_enabled` 保持 False，`can_auto_collect` 保持 A-only。
- 试采本身不得写 Job；导入必须指定已结束的真实 run ID，并对最多 10 个新岗位重新判断当前时效及证据。
- `qualified` 可进入待核验；仅缺 `TARGET_AUDIENCE_UNCLEAR` 且通过 `evaluate_review_intake` 的样本以 C/人工复核进入待核验。其他缺证、排除和抓取错误不导入。
- 保留 `REAL_RISK_FLAG`、来源/轮次/样本出处、稳定指纹；不修改 AI Key 相关的两处现有未提交改动。
- 人工 C→B 只处理真实、新招聘、待核验、因受众不明确落 C 的线索；需官方证据 URL、摘录、说明、审核权限、版本校验和双审计记录。晋级后仍需结构化与最终审核。
- 本地实库写入前用 SQLite backup API 制作一致性备份；真实试采遵守原有请求预算，不绕过登录、校内权限、验证码或机器人限制。

---

### Task 0: 修复校园接口的上海地点漏判（真实试采新增发现）

**Files:** Modify `app/sources/campus_json.py`; test `tests/test_campus_json_source.py`.

- [ ] **Step 1: RED tests.** 岗位地址只写街道时，若正文明确该岗位工作地点为上海，或单位地址明确上海且同一街道门牌吻合，应规范成“明确上海”；岗位地址明确外市、仅企业名含上海、仅单位地址在上海但岗位地址不同，均不得被误提升。
- [ ] **Step 2: Minimal parser change.** 只改校园 JSON 详情的地点规范化，保留原始岗位地点和证明句；不改共享 `evaluate_candidate` 或 A 类采集入口。
- [ ] **Step 3: GREEN tests and live re-trial.** 本地固定样本测试后，对已备份的交大 B 来源重新做一次有界试采；新轮次样本必须按当前规则、时效与人工证据进入显式导入。

### Task 1: 有界 B 试采样本显式导入

**Files:** Create `app/services/b_pilot_intake.py`; test `tests/test_b_pilot_intake.py`; reuse `app/services/source_trials.py`, `app/services/real_collection.py` without changing the zero-Job guarantee of source trials.

**Interfaces:** `import_trial_run(session: Session, source_id: int, run_id: str, actor_id: int | None, *, sample_ids: tuple[int, ...], now: datetime | None = None, max_jobs: int = 10, request_id: str = "") -> PilotImportResult`；调用时必须显式选定 1–10 个样本；结果包含 `created`, `updated`, `unchanged`, `skipped`, `skip_reasons`, `invalidated`。

- [ ] **Step 1: Write failing tests.** 使用内存库建立白名单来源、`completed` 轮次和 `SourceTrialSample`。断言合格样本变成 `is_demo=False/status=待核验/risk_flags=REAL_RISK_FLAG`，来源仍 B/禁用；同轮重复导入不增行。分别覆盖错误来源键、来源暂停、轮次归属/状态/版本、过期、非上海、缺正文/投递、仅受众不明、样本哈希篡改、明确 Top 院校硬门槛、选定样本 ID 归属及最大 10 新岗。测试例 API：

```python
result = import_trial_run(session, source.id, run.run_id, None,
    sample_ids=(selected_sample.id,), now=datetime(2026, 10, 5), max_jobs=10)
assert result.created == 1
assert session.query(Job).one().status == "待核验"
assert (source.library_tier, source.is_enabled) == ("B", False)
```

- [ ] **Step 2: Verify RED.** `python -m pytest tests/test_b_pilot_intake.py -q`，预期因模块或函数尚不存在而失败，而不是测试装置错误。
- [ ] **Step 3: Implement minimal service.** 使用精确白名单和 `SourceTrialRun` 归属/版本检查；网页必须传单个样本 ID，服务验证其归属后仅处理选定样本；以 `SourceCandidate` 重建样本事实，校验正文 SHA256，与当前 `evaluate_candidate` / `evaluate_review_intake` 复核并拒绝明确院校硬门槛；`CampusJobDetail` 适配 `_save_detail`，以原来源指纹去重。导入后附加 `run_id/sample_id` 出处，保存 `AuditEvent`；仅一次事务提交。仅由管理员或本地管理员执行，异常回滚。`qualified` 但 `screen_intake` 为 C 时按显式学生证据设为 B；`review_only` 一律 C。
- [ ] **Step 4: Verify GREEN.** `python -m pytest tests/test_b_pilot_intake.py tests/test_source_trials.py tests/test_source_candidate_policy.py tests/test_sbs_integration.py -q`；确认原 B 试采零 Job、A 日常调度不变。
- [ ] **Step 5: Review diff.** 特别检查没有修改 `can_auto_collect` 或批量晋升来源，也没有将采集日期伪装成岗位发布日期。

**实施中发现的保护边界：** 较新试采若使同指纹旧岗位不再合格，要显式撤销旧核验并记录 `source_trial.invalidate`；较旧轮次不得覆盖较新导入或撤销，轮次高水位从审计关联的 `SourceTrialRun.finished_at` 取得，不用人审的 `last_verified_at`。原文更新要同时递增 `version` 和 `row_version`。这两点也需覆盖 A 类商学院复采，且发布前校验应独立拒绝 C/D。

### Task 2: 受众缺证 C→B 的人工复核服务

**Files:** Create `app/services/intake_promotion.py`; test `tests/test_intake_promotion.py`; modify `app/services/reviews.py` to reject final approval of C/D grades.

**Interfaces:** `promote_review_only_job(session: Session, job_id: int, *, actor_id: int | None, expected_version: int, expected_fingerprint: str, evidence_url: str, evidence_excerpt: str, note: str, request_id: str = "", now: datetime | None = None) -> Job`。

- [ ] **Step 1: Write failing tests.** 为一条 `intake_reason=REVIEW_NOTE` 的真实、待核验、新招聘 C 级岗位断言升档后仍待核验；`ReviewLog` 与 `AuditEvent` 均记录证据、旧/新等级及操作者。测试 D/演示/过期/未分类/非受众缺证、空或非 HTTP(S) URL、空摘录、旧版本和重复提交均被拒绝；正文变化回 C；普通账号拒绝。

```python
job = promote_review_only_job(session, job.id, actor_id=reviewer.id,
    expected_version=job.version, expected_fingerprint=job.content_fingerprint,
    evidence_url="https://employer.example.com/jobs/123",
    evidence_excerpt="接受 2027 届本科毕业生申请", note="已核对企业官方岗位页")
assert (job.intake_grade, job.status) == ("B", "待核验")
```

- [ ] **Step 2: Verify RED.** `python -m pytest tests/test_intake_promotion.py -q`，预期因服务缺失失败。
- [ ] **Step 3: Implement minimal service.** 校验操作者 `can_review`/管理员与岗位条件，使用 `UPDATE ... WHERE version/row_version/content_fingerprint/grade/status` 条件写入；只更新 B 分级、审计事实和失效字段，递增版本。清理旧核验与 AI 建议；同一事务写两个审计表。原文复采时由既有 `_save_detail(review_note=REVIEW_NOTE)` 重置 C。
- [ ] **Step 4: Verify GREEN.** `python -m pytest tests/test_intake_promotion.py tests/test_sbs_integration.py tests/test_reviews.py -q`；若历史演示 fixture 因真实日期已过期失败，单列为既有失败，不修改生产发布时效门槛来“修绿”。
- [ ] **Step 5: Review diff.** 晋级动作不得变成发布动作；其他 C/D 不可借此放行。

### Task 3: 网页操作与权限回归

**Files:** Modify `app/main.py`, `app/templates/source_trials.html`, `app/templates/job_detail.html`; test `tests/test_b_pilot_web.py`。

**Interfaces:** `POST /sources/{source_id}/trials/{run_id}/import`（管理员）；`POST /jobs/{job_id}/intake-promotion`（审核员及管理员）。

- [ ] **Step 1: Write failing endpoint tests.** 本地测试逐条导入按钮（含 sample_id）、未选样本不能导入、回显新增/跳过数、升档表单和错误反馈；云端测试 member 直 POST 403、reviewer 只可升档不可导入、管理员需 CSRF、未知 run 不能导入。断言按钮隐藏不是唯一权限检查。
- [ ] **Step 2: Verify RED.** `python -m pytest tests/test_b_pilot_web.py -q`，预期 404 或缺按钮。
- [ ] **Step 3: Add routes and UI.** `required_action_for_request` 显式映射导入到 `sources.approve`、升档到 `review.decide`；导入路由必须接收一个 `sample_id`，异常显示于当前页。报告页每个合格样本展示逐条导入操作；岗位详情仅对合格 C 级展示证据表单、当前版本与指纹隐藏值。所有表单沿用现有 CSRF 中间件。
- [ ] **Step 4: Verify GREEN.** `python -m pytest tests/test_b_pilot_web.py tests/test_permissions.py tests/test_source_admission.py tests/test_web.py -q`，记录与本功能无关的既有日期 fixture 失败。
- [ ] **Step 5: Review diff and run full suite.** 以工作区内全新 `--basetemp` 与 `-p no:cacheprovider` 运行全量 pytest；对比开工时 11 fail + 26 环境 error 基线，不能把新增回归归为旧问题。

### Task 4: 真实试点与结果核对

**Files:** Create `docs/sources/2026-10-05-b-pilot-run-report.md`; leave `data/recruiting_local.db` and backup outside Git。

- [ ] **Step 1: Back up.** 用 SQLite `Connection.backup()` 将 `data/recruiting_local.db` 一致性备份到工作区 `backups/` 下的新文件；检查 `PRAGMA integrity_check` 与原库行数，不覆盖已有备份。
- [ ] **Step 2: Small live trials.** 仅各跑一次 `python -m app.cli source-trial --source-key sufe-jobs --pages 1 --list-limit 10 --detail-limit 3 --total-seconds 180` 与 `python -m app.cli source-trial --source-key sjtu-internships --pages 1 --list-limit 10 --detail-limit 3 --total-seconds 180`。有访问限制、429 或接口变化则停止，不绕过。
- [ ] **Step 3: Inspect before import.** 核对每条合格／仅受众缺证样本的真实雇主、职位正文、上海地点、发布时间、投递证据及外校可投性；只导入可回查样本。若不合格则报告零新增，不人为放宽硬边界。
- [ ] **Step 4: Import and verify.** 通过页面或同一服务限量导入指定 `run_id`；查询新增 `Job` 指纹、待核验状态、风险标记、来源出处和重复数。再次导入验证零重复，确认每日 A 调度仍不包含 B。
- [ ] **Step 5: Record outcome.** 报告真实轮次、样本决策、入库条数、问题及是否达到 1–2 条／日目标；不把试采数当作可发布数。
