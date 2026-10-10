# 四路公开来源本地入库 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对 NCSS、工程技术大学、应用技术大学、注协逐路验证，并将合格真实线索送入现有本地人工核验流程。

**Architecture:** 复用已有来源目录、试采报告、限量导入与审核链路。新增两个协议适配模块，不改数据库架构；源 B 禁用日常采集，岗位按证据初筛。

**Tech Stack:** Python/httpx/BeautifulSoup/SQLAlchemy/pytest，项目 `.venv`。

## Global Constraints

- 本地脏工作树原地窄修改；保留已有内容，不提交或推送，不删除岗位。
- 只用经核实 HTTPS 官方入口；不登录、不绕过限制、不关闭 TLS。
- 每轮最多 3 页/30 列表/10 详情，12 秒单请求、600 秒总预算、至少 1 秒间隔。
- 仅受众缺证可 B 待确认；地点、投递、正文缺证不导入，硕士起/资深/过期/校内限制不导入本科池。
- 来源保持 B 且日常采集禁用；无自动审核/发布。真实库写入前备份。

## 工作包与进度（唯一执行入口）

| 包 | 范围/产物 | 验收 | 状态 |
|---|---|---|---|
| P1 | `app/sources/ncss.py`、对应离线测试 | 主卡事实/投递/学历/地点，不串导航、弹窗；有界关键词检索 | 已完成，19 项定向通过 |
| P2 | `app/sources/campus_jsonp.py`、新测试 | 两校公开列表与详情、JSONP 安全、单岗位事实 | 已完成，15 项定向通过，含空正文修复 |
| P3 | `app/sources/shcpa.py`、新测试 | rid 稳定、发布日期/截止/邮箱、实际地点 | 已完成，14 项定向通过 |
| P4 | catalog/trials/pilot/policy/UI 与集成测试 | 白名单一致、去重/审核撤销/本科排除 | 已完成，最终全量 785 项通过 |
| P5 | 真实试采、备份、导入、报告/台账 | 各路结果、可见岗位/审计、重复不增、全量回归 | 已完成，新增 16 条，重复新增 0，台账第 47 行 |

最终实证：[四源本地接入报告](../../sources/2026-10-10-four-source-local-intake-report.md)。新增记录 11A/5B，真实库 A49/B11/C69/D64；来源仍 B 且禁用自动采集。16 为记录数，粗略公司+标题 13 组，不代表稳定日均产出。

### Task 1: NCSS（P1）

**Files:** `app/sources/ncss.py`, `tests/test_ncss_source.py`, 脱敏 fixture。

**Interfaces:** 保留现有 NcssListing/NcssDetail/fetch/parse 调用兼容；关键词列表为新增可选参数；详情兼容 `candidate_from_detail` 属性，真实投递证据不得伪造。报告 `outputs/four-source-20261010/task-1-report.md`。

- [x] 写测试复现 `.jobdetail-box` 只有职责而遗漏主卡学生要求；导航上海、隐藏投递弹窗不能充当证据。
- [x] Run `.venv/Scripts/python.exe -m pytest -q tests/test_ncss_source.py`，保存 RED 原因。
- [x] 最小修改真实主卡字段抽取，保持时效拒绝；可选关键词请求仍受总列表预算限制。
- [x] 同命令 GREEN；不改公共策略/目录/试采服务，这些归 P4。

### Task 2: 两校 JSONP（P2）

**Files:** 新建 `app/sources/campus_jsonp.py`, `tests/test_campus_jsonp_source.py`。

**Interfaces:** `SUES_INTERNSHIP_SOURCE`, `SIT_INTERNSHIP_SOURCE` 配置；`fetch_jsonp_listings(client, source, pages=1, *, timeout=12.0, controller=None)` 与 `fetch_jsonp_details(client, source, listing, *, timeout=12.0, controller=None)`。详情为具有 CampusJobDetail 属性的对象，含 `identity_key/title/employer_name/detail_url/evidence_text/location_category/location_detail/published_at/official_url`。

- [x] JSONP 合法/恶意代码尾随、错误形状、单岗位/多岗位、异地/占位文本/未知日期字段测试先 RED。
- [x] 配置端点 `/pros_jiuye/s/zxh/owebsiteData/position`，列表 fbbk=1/type=list，详情 wid/type=input；回调固定 `researchCallback`，纯 JSON 解析。
- [x] 保留原文字段；只从具体职责证明学生/地点/投递。正文截止优先，不猜测 field2 日期；实测字段须注明来源。
- [x] Run `.venv/Scripts/python.exe -m pytest -q tests/test_campus_jsonp_source.py` GREEN；报告 `outputs/four-source-20261010/task-2-report.md`。

### Task 3: 注协 HTML（P3）

**Files:** 新建 `app/sources/shcpa.py`, `tests/test_shcpa_source.py`。

**Interfaces:** `SHCPA_LIST_URL`, `fetch_shcpa_listings(client, pages=1, *, timeout=12.0, controller=None)`, `fetch_shcpa_detail(client, listing, *, timeout=12.0, controller=None)`；详情属性与 P2 一致。

- [x] 先测试同域 rid 身份、公司名不等于上海地点、邮箱、正文/日期缺证、已经过期及占位文本。
- [x] Run `.venv/Scripts/python.exe -m pytest -q tests/test_shcpa_source.py` RED。
- [x] 解析公开表格，不跟外站、登录或随机枚举 rid；只读列出的详情，保留协会声明。
- [x] 同命令 GREEN；报告 `outputs/four-source-20261010/task-3-report.md`。

### Task 4: 接入现有试采/导入（P4）

**Files:** catalog.py/source_trials.py/b_pilot_intake.py/source_candidate_policy.py/main.py/source_trials.html 与对应测试。

- [x] 添加三个新源 B 禁用条目；试采/导入统一白名单，NCSS 增入既有试点名单。
- [x] 写集成测试先 RED：合格→待核验；受众缺证→B标记；硕士起/过期/伪造源不导入；相同样本二次不增。
- [x] 扩展受控 TrialAdapter，仅注册四源；不新增爬虫调度，不改变其他来源状态。
- [x] 补足平台原生投递特殊情况时必须有可核对的主卡实际操作证据、官方域名/固定路径；仅“投递简历”字串不放行。
- [x] 全局复用既有审计、内容版本和发布防护；UI 的单岗位直链与原有列表入口分清。
- [x] 定向测试 GREEN，独立审查修复后全量回归一次。

### Task 5: 本地实证交付（P5）

- [x] SQLite 在线备份；保存四路受控试采报告（每轮最多10详情），查看逐项排除原因。
- [x] 核对可用原文后选择明确样本调用 `import_trial_run`，保存 ID 与计数；不符合则保留试采样本。
- [x] 再导入同轮同样本验证 created=0；只读验证新岗位字段、审核状态/审计，页面200。
- [x] 保存 `docs/sources/2026-10-10-four-source-local-intake-report.md`，追加项目更新台账，不覆盖历史。
- [x] 最终报告区分新增岗位、存量更新、未入库样本、自动来源启用与尚需授权的实习僧。
