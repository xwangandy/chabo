# 插播 React 网页端部署说明

本文用于部署三端网页：管理端、广告主端、频道主端。Bot webhook 仍可继续跑旧的 `chabo run-web`；React 网页端使用独立的 `chabo run-api`。

## 生产目录与账号

建议生产机固定使用以下目录：

```bash
sudo useradd --system --home /opt/chabo --shell /usr/sbin/nologin chabo || true
sudo install -d -o chabo -g chabo -m 0755 /opt/chabo
sudo install -d -o chabo -g chabo -m 0750 /var/lib/chabo /var/log/chabo
sudo install -d -o root -g chabo -m 0750 /etc/chabo
```

应用代码部署到 `/opt/chabo`，SQLite 默认放 `/var/lib/chabo/chabo.sqlite3`，生产环境变量放 `/etc/chabo/env`。

## 生产环境变量

先复制模板，再替换域名和所有强密钥：

```bash
cd /opt/chabo
sudo install -o root -g chabo -m 0640 .env.production.example /etc/chabo/env
sudo editor /etc/chabo/env
```

至少确认这些项不是占位值：

```bash
CHABO_PUBLIC_HOST=chabo.example
CHABO_DB_PATH=/var/lib/chabo/chabo.sqlite3
CHABO_ADMIN_TOKEN=<强随机 token>
CHABO_WEBHOOK_SECRET=<强随机 secret>
CHABO_API_SECRET_KEY=<强随机 secret>
CHABO_BOT_TOKEN=<Telegram Bot token>
CHABO_BOT_USERNAME=<Bot username>
CHABO_SESSION_COOKIE_SECURE=1
CHABO_WEB_ALLOWED_ORIGINS=https://chabo.example
CHABO_API_PROXY_HEADERS=1
CHABO_API_FORWARDED_ALLOW_IPS=127.0.0.1
CHABO_DEV_SESSION_ENABLED=0
CHABO_DEV_AUTH_BYPASS=0
```

前端如果和 API 同域部署，`web/.env.production.example` 保持 `VITE_CHABO_API_BASE_URL=` 即可；如果 API 独立域名，再复制为 `web/.env.production` 并改成完整 API origin。

## 构建前端

```bash
cd /opt/chabo/web
npm ci
npm run build
```

构建产物在 `/opt/chabo/web/dist`。nginx 只需要读取这个目录，不需要 Node.js 常驻。

## 启动 API

```bash
sudo cp DOC/部署/systemd-chabo-api.service /etc/systemd/system/chabo-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now chabo-api
```

API 默认监听 `127.0.0.1:8081`。`systemd-chabo-api.service` 会在启动前执行：

```bash
chabo preflight --host "$CHABO_PUBLIC_HOST"
```

所以 `/etc/chabo/env` 中必须设置 `CHABO_PUBLIC_HOST`。若 preflight 发现弱 token、开发免登录、DB 不可写等 critical 问题，服务不会启动。

Bot 侧“打开网页端”按钮依赖 `CHABO_PUBLIC_BASE_URL` 生成一次性登录链接。生产环境必须配置为 HTTPS 地址，例如 `https://chabo.example`；`chabo preflight` 会把生产环境缺失或非 HTTPS 的 `CHABO_PUBLIC_BASE_URL` 判为 critical。

## 配置 nginx

```bash
sudo cp DOC/部署/nginx-chabo-web.conf /etc/nginx/sites-available/chabo-web.conf
sudo ln -s /etc/nginx/sites-available/chabo-web.conf /etc/nginx/sites-enabled/chabo-web.conf
sudo nginx -t
sudo systemctl reload nginx
```

替换样例里的域名、证书路径和前端构建目录。

样例会做四件事：

- 80 端口跳转 HTTPS。
- `/api/health` 和 `/api/*` 反代到 `127.0.0.1:8081`。
- `/assets/*` 使用 30 天 immutable 缓存，SPA 页面本身 `no-store`。
- `/login`、`/admin`、`/advertiser`、`/publisher` 等路由 fallback 到 `index.html`。

## Readiness

负载均衡或监控读取：

```text
GET /api/health
```

返回 `ok=true` 表示 API 进程可用且 DB ping 成功；`ops` 字段包含待审订单、到期投放、open 争议、待审入账等运营摘要。部署脚本也应继续执行：

```bash
chabo preflight --host chabo.example
```

也可以用网页端一键验收命令串起 Python 回归、前端构建、preflight 和 health：

```bash
chabo verify-web --profile production \
  --host chabo.example \
  --health-url https://chabo.example/api/health \
  --audit-chain
```

这条命令是生产 gate：任一关键项失败都会返回非零退出码。它会跑 Python 编译、后端单测、前端构建、生产 preflight、审计链完整性校验和 `/api/health`。

上线后如需按时间窗复核审计链，可单独执行：

```bash
chabo verify-audit-chain --created-from "2026-05-02 00:00:00" --strict
```

管理端审计页也支持同一能力：先选择时间范围，再勾选“严格”，点击“校验链路”。严格模式会把窗口内旧的未签名审计行视为异常，适合生产切换点之后的验收。

本地开发验收可使用：

```bash
chabo verify-web --profile local --seed-demo
```

若 API 和 Vite/preview 均已启动，可追加 `--h5-smoke` 把 390px H5 视觉冒烟纳入同一次验收。

## H5 视觉冒烟

开发机或 CI 可以在 API 与 Vite/preview 都启动后跑：

```bash
cd /opt/chabo/web
CHABO_ADMIN_TOKEN=<强随机 token> npm run smoke:h5
```

脚本会用 390px 移动视口检查广告主计划摘要、管理端紧急处理和频道主频道配置，并把截图写到 `web/.runtime/h5-smoke/`。默认只验证“摘要可生成、提交按钮可用”，不会真的提交订单；如需在临时库里完整提交，设置 `CHABO_H5_SMOKE_SUBMIT=1`。

本地开发如果 API 已用 `CHABO_DEV_AUTH_BYPASS=1` 启动，可以不用管理 token：

```bash
cd web
CHABO_H5_SMOKE_AUTH_BYPASS=1 npm run smoke:h5
```

开发或演示库可以先补一组稳定数据，避免三端页面空白：

```bash
chabo seed-web-demo
```

该命令只用于本地/演示环境，会创建开发账号权限、演示频道、素材、待审订单和待审入账；不要对生产库执行。

## CI / 部署 Gate

GitHub Actions 已接入两层 gate：

- Push / PR：`Web verify (CI local gate)` 会执行 `chabo verify-web --profile local --skip-tests --skip-health`，覆盖前端构建和本地 preflight；Python 测试仍由 coverage 步骤执行。
- 手动 `workflow_dispatch`：填写 `production_host` 后，会执行 `chabo verify-web --profile production --host <host> --health-url <url>`。需要在 GitHub Secrets / Variables 配置 `CHABO_ADMIN_TOKEN`、`CHABO_WEBHOOK_SECRET`、`CHABO_API_SECRET_KEY`、`CHABO_BOT_TOKEN`、`CHABO_BOT_USERNAME`。

生产机上一键发布可使用：

```bash
cd /opt/chabo
./scripts/deploy-web-production.sh chabo.example https://chabo.example/api/health
```

脚本流程：

1. 读取 `/etc/chabo/env`。
2. `git pull --ff-only`。
3. 发布前执行 `chabo backup-db`，先留下旧 SQLite 快照；可用 `CHABO_DEPLOY_BACKUP_TARGET` 指定路径。
4. 安装 Python web 依赖与前端依赖。
5. `chabo verify-web --profile production --audit-chain --skip-health`，先挡住测试、构建、preflight 和审计链问题。
6. `systemctl restart chabo-api`。
7. `chabo verify-web --profile production --audit-chain --skip-tests --skip-build`，重启后再次跑 preflight、审计链和 `/api/health`。

如使用外部发布系统，可直接复用这两条 gate 命令，不一定使用脚本。

## 回滚演练

每次生产发布前至少 dry-run 一次回滚脚本，确认 DB 路径、服务名、health URL 都来自正确环境：

```bash
cd /opt/chabo
./scripts/rollback-web-production.sh --dry-run /var/backups/chabo/chabo-20260502-120000.sqlite3 chabo.example https://chabo.example/api/health
```

正式回滚命令：

```bash
cd /opt/chabo
./scripts/rollback-web-production.sh /var/backups/chabo/chabo-20260502-120000.sqlite3 chabo.example https://chabo.example/api/health
```

脚本会先把当前 SQLite 再备份一份到 `<db_dir>/backups/pre-rollback-YYYYMMDD-HHMMSS.sqlite3`，然后停止 `chabo-api`、恢复指定备份、启动服务，并执行 `verify-web --profile production --audit-chain --skip-tests --skip-build`。如果使用外部进程管理器，保留同样顺序：先备份当前库，再停服务、恢复、启动、跑 production gate。

临时 SQLite 副本恢复演练：

```bash
cd /opt/chabo
./scripts/rehearse-sqlite-restore.sh /var/backups/chabo/chabo-20260502-120000.sqlite3
```

该脚本会把当前库和备份库复制到临时目录，只在临时 `restored.sqlite3` 上跑迁移、审计链校验和 preflight，不会覆盖生产 DB。

## 审计报告留档

定期生成审计链完整性报告：

```bash
cd /opt/chabo
./scripts/export-audit-integrity-report.sh --created-from "2026-05-02 00:00:00" --strict
```

默认写入 `<db_dir>/audit-reports/audit-chain-YYYYMMDDTHHMMSSZ.json`，并生成同名 `.sha256`。生产 cron 建议每天在备份后执行一次，把 JSON 和 sha256 一起异机保存。

## 审计查询性能

当前 SQLite 索引清单：

- `idx_audit_logs_created(created_at)`：全量时间倒序分页。
- `idx_audit_logs_actor(actor_account_id, created_at)`：按操作者过滤。
- `idx_audit_logs_entity(entity_type, entity_id, created_at)`：按业务对象或目标账号过滤。
- `idx_audit_logs_action_created(action, created_at)`：权限调整、代看、管理员等级等分类筛选。
- `idx_audit_logs_hash(audit_hash)`：链路定位与导出签名核对。

审计页默认服务端分页，不允许一次性拉全表；CSV 导出仍受 `CHABO_AUDIT_EXPORT_MAX_ROWS` 限制。数据量到百万级后，应把同一索引设计迁移到 PostgreSQL，并用 `created_at DESC, id DESC` 或独立自增序列稳定分页。

SQLite 阶段不要直接删除 `audit_logs`。如需满足保留期要求，先做整库备份和审计报告留档；后续迁移 PostgreSQL 后，再以月分区或冷归档库执行物理归档。

## 生产安全项

- HTTPS 入口必须由 nginx 或负载均衡终止，API 只监听 `127.0.0.1:8081`。
- `CHABO_WEB_ALLOWED_ORIGINS` 只填写正式网页域名，不要在生产保留 `localhost`。
- `CHABO_SESSION_COOKIE_SECURE=1` 要和 HTTPS 同时启用；本地开发仍可设为 `0`。
- `CHABO_DEV_SESSION_ENABLED` 是隐藏开发授权表单开关，生产必须保持 `0`；`chabo preflight` 会把生产开启视为 critical 问题。
- `CHABO_DEV_AUTH_BYPASS` 是本地开发免登录开关，生产必须保持 `0`；`chabo preflight` 会把它开启视为 critical 问题。
- `/etc/chabo/env` 建议权限 `0640`、owner `root:chabo`，不要把 token 写入 systemd unit 或 nginx 配置。
- 日志用 journald 收集 `chabo-api.service`，上线后至少巡检 `journalctl -u chabo-api -p warning --since -30m`。
- SQLite 生产期每天至少一次 `chabo backup-db --target /var/backups/chabo/chabo-$(date +%Y%m%d-%H%M%S).sqlite3`，并定期做异机备份。

## PostgreSQL 迁移评估

当前 SQLite 可以支撑 MVP 和早期运营，但出现以下任一条件时应切 PostgreSQL：

- 多个 API / Bot / worker 进程需要同时高频写入。
- 订单、投放、审计日志超过百万级，运营检索明显变慢。
- 需要在线报表、复杂聚合或跨服务读取。
- 需要更细的备份恢复点、只读副本或云数据库托管。

迁移前置工作：把 SQL 查询从 SQLite 方言逐步收口到 repository/read model 层；为账本、订单、投放、审计表补索引清单；准备 `sqlite -> pg` 一次性导入脚本和只读校验脚本。

## 发布步骤

推荐使用脚本：

```bash
cd /opt/chabo
./scripts/deploy-web-production.sh chabo.example https://chabo.example/api/health
```

手工发布则按下面顺序：

1. `git pull --ff-only` 或部署新 release 到 `/opt/chabo`。
2. `pip install -e ".[web]"` 更新 Python 依赖。
3. `cd web && npm ci`。
4. `chabo verify-web --profile production --host chabo.example --health-url https://chabo.example/api/health --audit-chain --skip-health`。
5. `sudo systemctl restart chabo-api`。
6. `chabo verify-web --profile production --host chabo.example --health-url https://chabo.example/api/health --audit-chain --skip-tests --skip-build`。
7. 跑 `npm run smoke:h5` 或打开 `/login` / magic link，确认三端门户能进入；管理端使用隐藏 `/login/admin` 生成一次性登录。
