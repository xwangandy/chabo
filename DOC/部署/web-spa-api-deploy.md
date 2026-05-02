# 插播 React 网页端部署说明

本文用于部署三端网页：管理端、广告主端、频道主端。Bot webhook 仍可继续跑旧的 `chabo run-web`；React 网页端使用独立的 `chabo run-api`。

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

API 默认监听 `127.0.0.1:8081`。生产环境变量放在 `/etc/chabo/env`，至少包含：

```bash
CHABO_DB_PATH=/var/lib/chabo/chabo.sqlite3
CHABO_ADMIN_TOKEN=<强随机 token>
CHABO_API_SECRET_KEY=<强随机 secret>
CHABO_BOT_TOKEN=<Telegram Bot token>
```

## 配置 nginx

```bash
sudo cp DOC/部署/nginx-chabo-web.conf /etc/nginx/sites-available/chabo-web.conf
sudo ln -s /etc/nginx/sites-available/chabo-web.conf /etc/nginx/sites-enabled/chabo-web.conf
sudo nginx -t
sudo systemctl reload nginx
```

替换样例里的域名、证书路径和前端构建目录。

## Readiness

负载均衡或监控读取：

```text
GET /api/health
```

返回 `ok=true` 表示 API 进程可用且 DB ping 成功；`ops` 字段包含待审订单、到期投放、open 争议、待审入账等运营摘要。部署脚本也应继续执行：

```bash
chabo preflight --host chabo.example
```

## 发布步骤

1. `git pull` 或部署新 release 到 `/opt/chabo`。
2. `pip install -e ".[web]"` 更新 Python 依赖。
3. `cd web && npm ci && npm run build` 更新 SPA 构建产物。
4. `sudo systemctl restart chabo-api`。
5. `curl -fsS https://chabo.example/api/health`。
6. 打开 `/login` 或 magic link，确认三端门户能进入。
