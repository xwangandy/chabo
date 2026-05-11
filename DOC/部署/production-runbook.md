# 插播网页端生产发布 Runbook

更新时间：2026-05-02

适用范围：React SPA + FastAPI API + SQLite MVP 生产环境。

上线交付清单与 PR 模板见 `DOC/部署/m9-delivery-checklist.md`。

## 1. 发布前

1. 确认 `/etc/chabo/env` 只包含生产域名、强随机 token 和正式 Bot 配置。
2. 确认 `CHABO_DEV_AUTH_BYPASS=0`、`CHABO_DEV_SESSION_ENABLED=0`、`CHABO_SESSION_COOKIE_SECURE=1`。
3. 执行生产 gate：

```bash
cd /opt/chabo
chabo verify-web --profile production \
  --host chabo.example \
  --health-url https://chabo.example/api/health \
  --audit-chain
```

4. 生成审计链留档：

```bash
./scripts/export-audit-integrity-report.sh --created-from "2026-05-02 00:00:00" --strict
```

5. 生成 SQLite 备份，并在临时副本上演练恢复：

```bash
chabo backup-db --target /var/backups/chabo/pre-release.sqlite3
./scripts/rehearse-sqlite-restore.sh /var/backups/chabo/pre-release.sqlite3
```

## 2. 发布

推荐使用脚本：

```bash
cd /opt/chabo
./scripts/deploy-web-production.sh chabo.example https://chabo.example/api/health
```

脚本会执行：拉取代码、备份 SQLite、安装依赖、构建前端、跑 production gate、重启 API、再次跑 health 和审计链。

### 失败处理

- `verify-web` 失败：不要发布。先看失败 step，若是测试/构建失败则修代码；若是 preflight 失败则修 `/etc/chabo/env` 或生产依赖；若是审计链失败则停止上线，导出当前报告并人工核对最近审计变更。
- 审计报告导出失败：不要发布。确认 `CHABO_DB_PATH`、报告目录权限和审计链校验结果；保留失败日志。
- 恢复演练失败：不要发布。优先确认备份文件可读、SQLite 文件完整、迁移脚本可重复执行；重新生成备份后再演练。
- 发布脚本重启前失败：服务尚未重启，保持旧版本运行；修复后重新发布。
- 发布脚本重启后 health 失败：立即查看 `journalctl -u chabo-api -n 200`，若 5 分钟内无法恢复，执行第 4 节回滚。
- H5 冒烟失败：不要开放给业务用户；保留 `web/.runtime/h5-smoke/` 截图，优先修遮挡、横向溢出、按钮不可点击。

## 3. 发布后

1. 打开管理端 `/admin#settings`，确认发布准入标签均为绿色。
2. 打开 `/admin#wallet`，确认总余额、冻结预算、最近流水和待审入账符合预期。
3. 打开 `/admin#audit`，选择上线时间窗、勾选“严格”、点击“校验链路”。
4. 执行 H5 冒烟：

```bash
cd /opt/chabo/web
CHABO_ADMIN_TOKEN=<强随机 token> npm run smoke:h5
```

## 4. 回滚

先 dry-run：

```bash
cd /opt/chabo
./scripts/rollback-web-production.sh --dry-run /var/backups/chabo/pre-release.sqlite3 chabo.example https://chabo.example/api/health
```

确认动作顺序无误后执行：

```bash
./scripts/rollback-web-production.sh /var/backups/chabo/pre-release.sqlite3 chabo.example https://chabo.example/api/health
```

回滚脚本会先备份当前 DB，再停止 API、恢复指定 SQLite、启动 API，并跑带 `--audit-chain` 的 production gate。

### 回滚失败处理

- dry-run 失败：不要正式回滚，先修路径、权限、服务名或 health URL。
- 恢复备份失败：不要删除当前库；使用回滚脚本生成的 `pre-rollback-*.sqlite3` 作为现场保护。
- 回滚后 health 失败：查看 API 日志；若备份库本身不可用，恢复 `pre-rollback-*.sqlite3` 回到回滚前状态，再重新评估。
- 回滚后审计链失败：保持服务内部可访问但暂停业务操作，导出审计链报告并人工确认备份时间点是否早于 hash 链启用。

## 5. 定期任务

建议 cron：

```cron
15 2 * * * cd /opt/chabo && chabo backup-db >/var/log/chabo-backup.log 2>&1
25 2 * * * cd /opt/chabo && ./scripts/export-audit-integrity-report.sh --strict >/var/log/chabo-audit-report.log 2>&1
```

审计日志保留期由 `CHABO_AUDIT_RETENTION_DAYS` 控制。SQLite 阶段不直接删除 `audit_logs`，以免破坏链式摘要；到达保留期后优先做整库归档和异机备份。后续切 PostgreSQL 时再引入按月分区、冷存储和只读归档库。
