# 来源检测升级与招聘渠道扩充 Implementation Plan

> **For agentic workers:** 使用 `executing-plans` 逐任务实施，以复选框跟踪进度。用户本轮只要求交付方案；切换模型并收到实施指令后再修改业务代码。无需自动分派子代理。

**Goal:** 让运营人员准确判断来源能否采集，优先接入中国银行招聘公告，并形成可重复使用的新渠道验证流程。

**Architecture:** 保留 FastAPI/Jinja2、SQLAlchemy、来源适配器、人工审核和现有云端 PostgreSQL/worker 架构。扩展来源诊断与现有采集分发，不引入 LangChain、Redis、浏览器集群或新的独立服务。

**Tech Stack:** 现有 Python、httpx、BeautifulSoup、SQLAlchemy、pytest；使用项目 `.venv`。

日期：2026-09-06。状态：核心功能已实施并完成本地与容器化回归；中国银行已通过隔离试采并升为 A 类。其余候选渠道仅完成可行性调查，未自动接入。本文件内除特别说明外，所有相对路径均相对于 `D:/黄药师 AI招聘项目/网页/`。

## 1. 实施边界和历史文档

- 云端架构依据：`D:/黄药师 AI招聘项目/ARCHITECTURE.md` 和根目录 `IMPLEMENTATION_PLAN.md`。
- `网页/` 内同名文档是此前业务优化方案，不能覆盖、替换或误当成云端架构。
- 本方案是来源模块增量任务，不要求顺带完成云端方案所有未完成事项。
- 工作区已有大量用户修改。先检查 `git status --short`、相关 diff 和 AGENTS.md；保留既有变化，不重置，不使用 `git add .`，不自动提交或推送。
- 本轮不自动审核、发布或向公众号、群聊发送内容。诊断和试采不调用付费模型，不读个人凭据，不直接写入正式岗位池。
- 不因为 HTTP 200、页面有“招聘”二字、企业知名或静态学生价值分高而开启自动采集。
- 正式接入必须保留来源、日期、正文、附件入口、版本、去重和过期阻断；全国公告不得自动标为上海岗位。

## 2. 已检查的事实与不确定项

2026-09-06 本地数据库查询：A 5、B 25、C 21、D 28。它是运行数据库快照，不是源码目录数量；不得硬编码页面计数。

当前 `source_health.py` 的 `check_source_connection()` 只执行 HTTP 请求，成功就报告“官网连接正常”；`source_library.py` 的 `can_auto_collect()` 要求 A 类、启用、已自动采集。B 类目录默认 `adapter_key=pending_validation`。

| 来源 | 本轮初查证据 | 实施决策 |
|---|---|---|
| 中国银行 | 当前 `/aboutboc/ab8/` 实际是媒体栏目；正确 `/aboutboc/bi4/` 有日期和公告列表，包含 2026-09-03 发布的 2027 校招公告 | 第一批实现适配，详情及附件需进一步验证 |
| 上海国际集团 | `/site/recruit` 是招聘栏目，本次只提取到表头 | 定位动态内容或确认空列表，保留 B 类 |
| 德勤、普华永道、安永 | 职业介绍页可访问，有学生入口 | 先验证真正的岗位列表和详情，不能采介绍页当岗位 |
| 招商、中信 | HTTP 成功，直接 HTML 没有有效岗位列表；招商配置是社招入口 | 核对校招入口及公开数据加载方式 |
| 上汽 | 当前 `/chinese/careers/` 返回 404 | 先核对官网招聘入口 |
| 上海电气、上港 | 本次返回 521 | 记录访问异常，后续复核 |
| 上海建工、上海机场、光明食品 | 本次返回 403 | 记录受限，不通过绕过校验解决 |
| 锦江 | 首页 HTML 几乎没有正文 | 定位招聘子站 |
| 其他 B 类 | 多个本机连接错误 | 分辨网络、证书和入口问题，不断言网站失效 |

这些是一轮探测，不代表可采集验收完成。站点内容会变化，实施时重新核验。当前来源初始化函数会覆盖目录字段、强制禁用非 A 类；晋级不能只修改数据库，否则重启会被恢复。

## 3. 本轮交付和不扩张的范围

必须交付：

1. 分阶段来源诊断、诊断记录、易理解的页面反馈。
2. 中国银行入口修正、专用采集、离线测试和隔离试采；达到验收标准才启用。
3. 全部 B 类诊断报告，以及一个上海学生综合渠道、一个专业服务校招渠道的深入可行性报告。
4. 最新验证记录、使用说明、现有 A 类回归测试。

新增渠道调查完成即算完成其任务，不要求在本轮强行全部上线。访问受限或没有公开数据时，报告原因并继续其他任务。只有新权限、付费采购或业务定位变化需要问用户。

## 4. 文件职责

| 文件 | 修改或新增内容 |
|---|---|
| `app/services/source_health.py` | 保留连接检查兼容入口，接入分阶段诊断 |
| `app/services/source_diagnostics.py`（新增） | 诊断结果、错误分类、受限的样本检查 |
| `app/models.py`、`app/database.py` | 来源诊断记录及本地兼容迁移 |
| `migrations/versions/` | 新建增量 Alembic revision；不改旧 revision |
| `app/services/health.py` | 若 schema revision 变化，同步现有就绪检查要求 |
| `app/sources/boc.py`（新增） | 中国银行列表与详情解析，复用现有数据结构 |
| `app/sources/catalog.py` | 修正中行入口和适配键，保护历史 source ID 与运营暂停状态 |
| `app/services/real_collection.py` | 注册中行采集分支，复用现有入库链路 |
| `app/main.py`、`app/templates/sources.html` | 分开连接检查与深度试采反馈；诊断权限 |
| `scripts/probe_sources.py`（新增） | 命令行批量诊断、隔离试采与结果导出 |
| `tests/test_source_diagnostics.py`、`tests/test_boc_source.py`（新增） | 语义诊断、站点解析与失败测试 |
| `tests/fixtures/sources/boc/`（新增） | 脱敏最小列表、详情及异常 HTML 样本 |
| 现有 source、collection、migration 测试 | 兼容性、目录初始化、迁移及去重回归 |
| `docs/sources/2026-09-06-validation-report.md`（新增） | 实测结论、未通过项、候选渠道优先级 |

## 5. 任务一：让来源诊断表达真实状态

接口契约：新增 `diagnose_source(source, client, checked_at, *, depth="connection") -> SourceDiagnosticResult`。返回值至少包含以下字段：

```python
connection_status: str  # ok / timeout / tls_error / http_error / connection_error
content_status: str    # not_checked / recruitment_list / detail_verified /
                       # no_openings / wrong_entry / dynamic_or_unverified / blocked
adapter_status: str    # missing / available / sample_passed
http_status: int | None
final_url: str
sample_count: int
detail_success_count: int
message: str
```

连接状态、内容状态、适配状态分别展示，不用一个“正常”覆盖所有结论。字段为内部枚举，网页输出中文。

- [ ] 新增行为测试：200 新闻页不能报告可采集；200 JS 空壳标为待验证；空岗位列表不能误报故障；403/521/超时/TLS 各有准确说明；缺适配器不晋级。
- [ ] 运行新增测试确认失败，再实现最小诊断逻辑。已知站点用明确栏目路径及站点解析器判断；未知站点只报待验证，不能只凭关键词判定。
- [ ] 连接检查最多一个页面、15 秒；深度试采只由 CLI 执行，最多 3 篇详情、不下载附件、单请求 12 秒、总预算 60 秒、请求间隔至少 0.5 秒。大响应流式限额 2 MB，超限中止并报告；重定向最多 3 次，每跳校验目标，拒绝私有/回环/链路本地地址。
- [ ] 深度诊断不能在 Web 请求中顺序检测全部 B 类。保留页面单站连接检查；页面显示“深度试采待执行”与最近的 CLI 诊断结果。本轮无需引入新后台调度系统。
- [ ] 保存 `SourceDiagnostic` 独立记录：source_id、checked_at、depth、上述状态及计数、短错误码、message。只保存截断的脱敏结果，不保存响应请求头、Cookie 或密钥。
- [ ] SQLite 使用既有兼容迁移模式；PostgreSQL 添加独立 Alembic 增量 migration，确认现有 initial revision 使用动态 metadata 的实际行为，避免重复建表。测试旧库升级和空库全链迁移。
- [ ] 诊断不得改变来源 tier/is_enabled，不更新 `last_success_at`（它代表采集成功），不清零正式采集失败次数。
- [ ] 保留现有连接检查路由与返回兼容性；云端执行诊断沿用服务端权限与 CSRF。补测普通读者不能通过诊断入口修改状态。

验收示例：一个 200 新闻页面应显示“网址可访问｜入口不是招聘列表｜尚不可自动采集”，而不是“来源正常”。

## 6. 任务二：中国银行接入

列表地址：`https://www.boc.cn/aboutboc/bi4/`。

复用 `OfficialListing`、`OfficialDetail`、`OfficialAttachment`，新增接口：

```python
parse_boc_list(html: str, base_url: str) -> list[OfficialListing]
parse_boc_detail(html: str, listing: OfficialListing) -> OfficialDetail
fetch_boc_listings(client, limit: int = 12) -> list[OfficialListing]
fetch_boc_detail(client, listing: OfficialListing) -> OfficialDetail
```

- [ ] 少量访问正确列表、一个校招详情和其公开附件链接，核对正文容器、日期位置及分页格式。保存最小离线 HTML 样本；测试不依赖联网。
- [ ] 列表测试：排除导航、媒体新闻、重复 URL；日期来自公告列表；保留招聘公告和录用公示的真实类型供后续分类，公示不得进入开放岗位推荐。
- [ ] 详情测试：正文无页脚导航；正确关联标题和发布时间；相对附件 URL 解析正确；验证码/空壳/错误页不产生有效详情；附件链接不等于附件已读取。
- [ ] 实现专用正文定位，避免通用全文回退将媒体介绍误当招聘正文；必要时复用已有工具，但不要改动所有来源的通用规则来迁就中行。
- [ ] 在现有采集分发中加入 `boc_announcements`。复用 `_save_detail` 与真实来源入库规则，保持 `is_demo=False`、待核验和人工发布门槛。
- [ ] 不把全国招聘合并推断为“明确上海”；多岗位公告保存为公告，缺岗位明细/附件未读时保留缺口。
- [ ] 测试重复采集不新增重复记录、正文变化更新事实版本、过期公告阻断、旧审批与旧草稿不能继续使用。
- [ ] 修改目录入口、适配键；名称去掉“待专用适配”时保留历史 Source 主键和关联岗位，不能用删除旧来源的方式重命名。
- [ ] 只有任务五验收通过后，将目录定义升 A；保证重启不降回 B，同时已被运营暂停或禁用的来源不会自动恢复。目录初始化重复执行结果稳定。

## 7. 任务三：改进来源页面

- [ ] 页面分别显示“连接结果”“招聘内容”“采集适配”，未检测显示“未验证”，不把创建时默认 status=正常当成已验证。
- [ ] 保留“验证官网连接”按钮，明确说明它不启用自动抓取；显示最近深度试采时间、样本数、成功数、失败原因。
- [ ] 统计 A/B/C/D 和实际可采集来源数量来自数据库条件；删除“固定 71 家”等过时硬编码，避免把来源层级 `level` 与来源库分类 `library_tier` 混为一谈。
- [ ] 页面提示给出可操作下一步，如“需更新招聘栏目地址”“需读取动态岗位列表”“正文不足，保留待适配”。不显示未经处理的异常堆栈。
- [ ] 桌面与窄屏检查长名称、结果说明和按钮；已有采集入口和公众号线索导入继续可用。

## 8. 任务四：新增渠道调查

先用新的 CLI 对全部 B 类做一轮连接/入口分类；只对已有适配器或明确可验证的候选做深度试采。单站失败继续下一站，整个命令输出成功、异常、未验证的数量。

CLI 约定：

```powershell
& .\.venv\Scripts\python.exe scripts/probe_sources.py --tier B --depth connection
& .\.venv\Scripts\python.exe scripts/probe_sources.py --adapter boc_announcements --depth sample --limit 3
```

默认以只读方式访问正式数据库以读取来源；默认诊断输出到终端。需要在页面查看时，使用显式 `--persist-diagnostics`，只追加诊断表。试采业务记录仅写临时测试库，不得在 CLI 默认路径写正式 jobs 表。

进一步调查以下两个方向，均需输出真实样本或失败证据：

1. 上海学生综合渠道：先从上海官方文件所列高校毕业生就业服务平台、国资骐骥定位真实公开入口；发现只提供登录后的服务时，报告限制，改查公开招聘公告或学校公开就业网。
2. 专业服务校招：优先德勤学生入口，沿官网链接定位岗位列表；没有可用公开数据则检查安永或普华永道学生入口。

每个候选报告：正式名称、官方背书链接、准确栏目 URL、是否需登录、页面加载方式、列表/详情/附件可读性、上海与应届筛选方式、一个真实样本、重复与时效风险、适配工作量（小/中/大及原因）、接入或暂缓建议。不能将“请求失败”写成“无招聘”。不提前承诺新增岗位数。

本轮将新渠道报告写入文档，不凭一次访问批量加入 A 类。公众号人工链接、企业/校友供稿、国家大学生就业服务平台列为后续渠道；现有公众号导入继续使用，不新建供稿系统。

## 9. 任务五：验证与完成条件

- [ ] 所有解析测试使用离线样本；模拟客户端验证请求数量、超时、重定向和失败处理，不用真实模型。
- [ ] 中国银行真实列表读取成功；至少 3 个不同详情样本通过正文核对，其中至少一个学生相关公告；老公告可作为解析样本，但不能作为有效新增岗位。
- [ ] 在隔离库试采两次，确认重复数稳定；报告新增、更新、过期排除、非招聘通知、正文失败、附件待核验数量。只解析成功但没有未截止岗位时如实报告。
- [ ] 正式启用条件：离线测试通过、真实样本符合上述要求、第二次隔离运行不重复入库、无错误上海标签、历史来源关联保持。任何一项不满足则保留 B 类并记录原因。
- [ ] 现有 5 个 A 类的解析与采集测试回归，确认通用逻辑未被中行适配破坏。

命令（在 `网页/` 执行，新测试文件由本轮实现）：

```powershell
& .\.venv\Scripts\python.exe -m pytest -q tests/test_source_diagnostics.py tests/test_boc_source.py tests/test_source_health.py tests/test_source_library.py tests/test_source_catalog.py tests/test_source_monitor.py tests/test_real_collection.py tests/test_collection_updates.py tests/test_spdb_source.py
& .\.venv\Scripts\python.exe -m pytest -q
```

若有 schema 修改，再执行迁移测试及隔离 PostgreSQL 验证。不要仅凭 `compose.test.yaml` 启动了数据库就宣称所有用例使用 PostgreSQL；必须有显式连接测试库的迁移/写读用例。

```powershell
$env:Path = 'C:\Program Files\Docker\Docker\resources\bin;' + $env:Path
docker compose -f compose.test.yaml build tests
docker compose -f compose.test.yaml run --rm tests
```

记录最终退出码、通过/跳过数和原因。命令返回运行会话时保留 session_id 并读到结束，不能将进度点当成通过。环境失败修复后再测，不使用系统全局 pytest 替代项目虚拟环境。

交接报告必须说明：改了哪些功能、中国银行是否已启用、实际有效新增条数、各 B 类诊断状态、新渠道优先级、未完成事项和理由。若重启本地服务，先确认是本项目进程，独立后台启动并在下一次命令中确认首页可访问。

## 10. 参考入口与执行提示词

- 中国银行原误配入口：https://www.boc.cn/aboutboc/ab8/
- 中国银行招聘公告：https://www.boc.cn/aboutboc/bi4/
- 上海国际集团招聘：https://www.sigchina.com/site/recruit
- 德勤学生入口：https://www.deloitte.com/cn/zh/cn-careers/students.html
- 上海官方渠道依据：https://edu.sh.gov.cn/xxgk2_zdgz_xxxsgz_01/20260211/519721f4f0a5447fa39a305111871a4a.html

切换模型后可直接发送：

> 阅读 `网页/docs/superpowers/plans/2026-09-06-source-validation-and-expansion.md`，按任务一至五实施。先检查实际代码和既有修改，不修改已确认架构。完成来源诊断升级、中国银行适配、B 类检测报告及两个新渠道可行性验证，运行测试并记录真实结果。只有达到方案验收条件才启用来源；访问受限时保留证据并继续其他任务。重要业务选择再问我，不自动发布、提交或推送代码。
