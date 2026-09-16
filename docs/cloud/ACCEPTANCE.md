# 云端试运行验收清单

本清单用于 3—5 名受邀成员共享同一业务数据的首轮试运行。只有实际执行并记录证据的项目才能勾选；本地自动化测试不能替代云端、备份或人工流程演练。

## 已由本地自动化测试覆盖

- [x] 受邀账号登录、首次改密、停用、启用、重置密码及会话撤销。
- [x] 成员、审核成员、管理员的服务端权限和 POST CSRF 校验。
- [x] 任务去重、租约领取、过期恢复、退避重试、任务详情和批次状态汇总。
- [x] SQLite 导入预演、空目标库保护与基础业务数据迁移。
- [x] 云端 Secret 文件只读、Host 白名单、响应安全头、OpenAPI 关闭与 schema 就绪检查。

## 部署前必须填写

- [ ] 域名与 DNS：`PUBLIC_BASE_URL`、`PUBLIC_HOST`、`ALLOWED_HOSTS`。
- [ ] 受控 Linux 主机、Docker、HTTPS 端口 80/443 和备份异机存储位置。
- [ ] PostgreSQL 密码与两个模型 Provider 的独立 Secret 文件。
- [ ] 首位管理员用户名、私密交付临时密码的团队渠道。

## 容器与数据演练

- [x] 在当前 Windows 主机运行 `docker compose -f compose.test.yaml run --rm tests`：`240 passed, 2 skipped`（2026-09-06）。隔离 PostgreSQL 健康检查通过；两项跳过的是仅适用于 Windows 主机的 PowerShell 语法检查。
- [ ] 执行 `docker compose run --rm migrate`，确认 `/health/ready` 返回 ready。
- [ ] 使用只读挂载的旧 SQLite 先执行 `migrate-sqlite --dry-run`，核对行数后再正式导入。
- [ ] 创建管理员，再创建普通成员和审核成员；用两个浏览器验证登录、权限与并发编辑冲突。
- [ ] 完成至少 3 条真实公告的采集、人工核验、审核、生成、人工发布回填流程。
- [ ] 演练 worker 重启、web 重启、数据库短暂不可用与任务恢复。
- [ ] 生成备份，在独立数据库恢复后核对账号、岗位、草稿和任务数据。

## 当前限制

- Docker Desktop、镜像构建和隔离 PostgreSQL 测试已在当前 Windows 主机验证；尚未在目标 Linux 主机执行生产迁移、HTTPS、备份与恢复演练。
- 现阶段持久化 worker 已接管 AI 建议分；其他既有采集、附件及内容生成入口仍需逐项完成任务化后，才可视为完全异步化。
