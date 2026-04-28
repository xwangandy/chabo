# 插播 admin / webhook token 轮换流程

CHABO 的两个共享密钥都通过环境变量传入：

- `CHABO_ADMIN_TOKEN`：HTTP 运营后台的访问凭证
- `CHABO_WEBHOOK_SECRET`：Telegram webhook 路径里的 secret 段

**任何一个泄漏，都按下面步骤换新；不要等。**

## 准则

- 强随机：`python3 -c "import secrets; print(secrets.token_urlsafe(32))"` 至少 32 字节熵。
- 不入库：放 `/etc/chabo/env`，权限 0640，owner `root:chabo`；不要 commit 到 git。
- 不复用：admin token、webhook secret、Bot token 三者必须各不相同。
- 启动校验：`chabo run-web` 启动时跑 `check_token_strength`，含 `test/demo/changeme/secret/admin/token` 关键字或长度小于 16 的会在 stdout 给告警。

## 轮换 admin token

1. 生成新值：`secrets.token_urlsafe(32)`。
2. 编辑 `/etc/chabo/env`，把 `CHABO_ADMIN_TOKEN=` 改成新值。
3. `sudo systemctl restart chabo`。
4. 刷新 `/admin?token=<NEW>` 页面验证；旧 token 应立刻失效。
5. 如果有外部脚本（监控、备份验证）用了 admin token，同步更新它们的 secret 仓库。
6. 在 `audit_logs` / 工具调用日志里搜旧 token 的最后一次成功使用，确认没有未授权调用。

## 轮换 webhook secret

webhook secret 同时影响 nginx 路径和 Telegram 那边设定的 webhook URL，必须**先调 Telegram，再切环境变量**，否则会丢一段时间的更新。

1. 生成新值。
2. 应用层先把新值写进 `/etc/chabo/env`，但**先别重启服务**。
3. 用旧 token 跑 `chabo set-webhook --url "https://chabo.example/telegram/webhook/<NEW>" --secret "<NEW>"`。这把 Telegram 上的 webhook URL 切到新 secret，并更新 secret_token。
4. `sudo systemctl restart chabo`。新 webhook 路径开始接收 update。
5. 验证：`curl -s https://chabo.example/health` 返回 `db: ok`；让一个测试用户给 Bot 发条消息，确认 `private_messages` 表 / 日志里能看到。
6. 如果发现 Telegram 那边没更新（比如 set-webhook 401 或网络挂掉），先回滚 `/etc/chabo/env` 到旧 secret 并重启，再排查。

## 应急：怀疑 token 已泄漏

立即按上面的步骤轮换；同时：

- 跑 `chabo list-tool-calls --result-status success --limit 200` 看最近一段时间有没有外部访问留下的痕迹。
- 把疑似时间窗里 `audit_logs` 里所有 actor_account_id 列出，与已知运营员账号对照。
- 备份当前库一份用于事后取证：`chabo backup-db --target /var/backups/chabo/incident-$(date +%Y%m%d-%H%M%S).sqlite3`。
