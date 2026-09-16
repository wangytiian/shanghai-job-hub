# 云端运行手册

在受控的 Linux 主机中安装 Docker、配置域名 DNS 和有效 HTTPS 可达性后，复制 `.env.example` 为受权限保护的 `.env`，将数据库密码替换为随机值。模型 Key 不写入 `.env`、镜像或 Git；云端 SecretProvider 的只读文件实现接入后，单独挂载两个 provider 的密钥文件。

首次上线顺序：

1. `docker compose up -d db`
2. `docker compose run --rm migrate`
3. 先预演旧库导入：`docker compose run --rm web python -m app.cli migrate-sqlite --source /migration/recruiting_local.db --dry-run`；核对输出行数后，去掉 `--dry-run` 正式导入。源 SQLite 必须以只读方式挂载，目标库必须为空。
4. `docker compose run --rm web python -m app.cli create-admin --username owner --display-name 管理员`
5. `docker compose up -d web worker proxy`
6. 访问 `/health/live`、`/health/ready`，再用管理员账号登录。

部署前备份，使用 `pg_dump` 生成可恢复的数据库备份并复制到主机外受控存储。恢复演练必须在独立数据库执行，核对用户、岗位、草稿和任务记录后再报告恢复目标已达成。

当前 worker 已具备安全领取和失败可见能力；在所有现有采集、AI 和附件入口完成任务化前，生产环境不应依赖它执行这些现有同步流程。

首次部署前，先执行 `docker compose -f compose.test.yaml run --rm tests`。Compose 文件已固定 ASCII 项目名，因此项目目录包含中文时也不会因自动命名失败。它会创建隔离的 `recruiting_test` PostgreSQL，不使用生产数据库或生产密钥；该检查通过后才进行首次部署。当前 Windows 主机已于 2026-09-06 验证该命令通过（`240 passed, 2 skipped`）。
