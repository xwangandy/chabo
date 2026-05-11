# Staging 真实环境 UAT 清单

更新时间：2026-05-11

适用范围：正式生产发布前，在 staging 域名、测试 Telegram Bot、测试频道和临时 SQLite/备份库上做真实 webhook + Bot + 网页端验收。

目标：上线前证明“代码、生产型配置、Telegram webhook、Bot 广告主路径、网页端 health/preflight”在同一套真实 HTTP 环境里一起工作。任何阻塞项都先回到 PR 修复，不带病上线。

## 0. Go / No-Go 规则

- 必须使用测试 Bot token 和测试频道，不使用生产 Bot 和生产频道做 UAT。
- staging 配置尽量等同生产：HTTPS、强 token、`CHABO_DEV_AUTH_BYPASS=0`、`CHABO_DEV_SESSION_ENABLED=0`、`CHABO_SESSION_COOKIE_SECURE=1`。
- 只有在临时调试环境允许 `--allow-weak-tokens` 或 `--allow-dev-auth-bypass`；这类放行不能作为生产发布依据。
- 机器 gate 任一步失败、webhook 不通、真实 Bot 路径任一步卡住，都判定 No-Go。
- UAT 记录必须写下 commit SHA、域名、Bot username、测试频道、测试账号、GitHub Actions run URL 和异常处理结论。

## 1. 环境准备

记录本次验收对象：

```text
commit SHA:
staging host:
Bot username:
广告主测试 Telegram user id:
频道主测试 Telegram user id:
测试频道 A:
测试频道 B:
```

后续命令可先设置这些本地变量，避免把占位符直接粘进 shell：

```bash
export STAGING_HOST=staging.chabo.example
export ADVERTISER_USER_ID=10001
export PUBLISHER_USER_ID=20001
export CHANNEL_A=staging_channel_a
export CHANNEL_B=staging_channel_b
```

更新代码并确认 staging 跑的是同一个 commit：

```bash
cd /opt/chabo
git fetch origin
git checkout main
git pull --ff-only
git rev-parse HEAD
```

确认 `/etc/chabo/env` 至少包含：

```bash
CHABO_ENV=production
CHABO_PUBLIC_HOST=<staging host>
CHABO_PUBLIC_BASE_URL=https://<staging host>
CHABO_DB_PATH=/var/lib/chabo/chabo-staging.sqlite3
CHABO_ADMIN_TOKEN=<强随机 token>
CHABO_WEBHOOK_SECRET=<强随机 secret>
CHABO_API_SECRET_KEY=<强随机 secret>
CHABO_BOT_TOKEN=<测试 Bot token>
CHABO_BOT_USERNAME=<测试 Bot username>
CHABO_SESSION_COOKIE_SECURE=1
CHABO_WEB_ALLOWED_ORIGINS=https://<staging host>
CHABO_DEV_SESSION_ENABLED=0
CHABO_DEV_AUTH_BYPASS=0
```

## 2. 机器 Gate

先跑广告主 Bot 路径自动化验收，确认 #5 基线仍在：

```bash
cd /opt/chabo
python3 -m unittest tests.test_chabo_mvp.ChaboMvpTest.test_bot_acceptance_advertiser_real_path_from_discover_to_plan_invoice
```

再跑本地 production profile。它覆盖 Python 编译、单测、前端 build、preflight、审计链和 `/api/health`。

```bash
cd /opt/chabo
chabo verify-web --profile production \
  --host "$STAGING_HOST" \
  --health-url "https://${STAGING_HOST}/api/health" \
  --audit-chain
```

再在 GitHub Actions 手动触发 `tests` workflow，填写：

```text
production_host=staging.chabo.example
production_health_url=https://staging.chabo.example/api/health
```

GitHub Secrets / Variables 需要使用 staging/test 值：

```text
CHABO_ADMIN_TOKEN
CHABO_WEBHOOK_SECRET
CHABO_API_SECRET_KEY
CHABO_BOT_TOKEN
CHABO_BOT_USERNAME
```

两个 gate 都通过后，继续 webhook 和手工 UAT。

## 3. Webhook 与服务连通

staging 同时需要两个本地服务：

- `chabo-api.service`：FastAPI 网页端 API，默认 `127.0.0.1:8081`，对外 `/api/*`。
- `chabo.service`：Telegram webhook / 旧 stdlib admin，默认 `127.0.0.1:8080`，对外 `/telegram/webhook/<secret>` 和 `/health`。

nginx 如果使用 `DOC/部署/nginx-chabo-web.conf`，还需要合并 `DOC/部署/nginx-chabo.conf` 里的 `/telegram/webhook/<secret>` location；否则网页端 health 会通，但 Telegram update 进不来。

检查服务：

```bash
sudo systemctl status chabo-api --no-pager
sudo systemctl status chabo --no-pager
curl -fsS "https://${STAGING_HOST}/api/health"
curl -fsS "https://${STAGING_HOST}/health"
```

设置测试 Bot webhook：

```bash
. /etc/chabo/env
chabo set-webhook \
  --url "https://${STAGING_HOST}/telegram/webhook/${CHABO_WEBHOOK_SECRET}" \
  --secret "${CHABO_WEBHOOK_SECRET}" \
  --drop-pending-updates
```

立刻在测试 Bot 私聊发送 `/start`。如果没有响应，先查：

```bash
journalctl -u chabo -n 200 --no-pager
journalctl -u nginx -n 100 --no-pager
```

## 4. 测试数据准备

把测试 Bot 加为两个测试频道的管理员，至少给“发消息、编辑消息、置顶消息”权限。然后让测试频道主在 Bot 内完成频道接入；如需 CLI 辅助，可使用：

```bash
chabo bind-channel \
  --telegram-chat-id <channel A chat id> \
  --title "Staging 测试频道 A" \
  --username "$CHANNEL_A" \
  --owner-telegram-user-id "$PUBLISHER_USER_ID"

chabo bind-channel \
  --telegram-chat-id <channel B chat id> \
  --title "Staging 测试频道 B" \
  --username "$CHANNEL_B" \
  --owner-telegram-user-id "$PUBLISHER_USER_ID"
```

让频道进入“找频道”和“批量投放”候选池：

```bash
for channel in "$CHANNEL_A" "$CHANNEL_B"; do
  chabo assess-channel \
    --channel "$channel" \
    --category software \
    --median-24h-views 20000 \
    --subscribers 50000 \
    --light-clicks-30d 180 \
    --light-unique-clickers-30d 120 \
    --repeat-purchase-count 2 \
    --dispute-count 0 \
    --risk-level normal
  chabo apply-pricing --channel "$channel"
done
```

给广告主测试账号准备余额和 Pro 权益，避免批量投放被余额或套餐挡住：

```bash
chabo topup \
  --telegram-user-id "$ADVERTISER_USER_ID" \
  --amount 1000 \
  --display-name "Staging 广告主" \
  --memo "staging UAT"

chabo purchase-advertiser-plan \
  --advertiser-telegram-user-id "$ADVERTISER_USER_ID" \
  --plan pro
```

## 5. 广告主真实 Bot 路径

用广告主测试账号在测试 Bot 私聊内完成以下路径。每一步都截图或记录消息时间点。

1. 找频道：进入广告主工作台，点“🔍 找频道”，应看到测试频道 A / B、分类、评分、风险和报价。
2. 收藏：在频道 A 点收藏；进入“⭐ 我的收藏”，应看到频道 A，可继续进入投放。
3. 广告库新建：进入“🗂 广告库” → “➕ 新建素材” → “标准插播”，输入文案和 `https://example.com/staging-uat`，保存后广告库出现该素材。
4. 广告库编辑：点素材的编辑按钮，修改文案；返回广告库后应显示新文案，目标链接保持不变。
5. 批量投放：点素材的“📡 批量投放”，设置单频道预算为 USD 150，勾选频道 A / B，提交后应显示 2 成功 / 0 失败。
6. 订单详情：进入“📋 投放订单”，打开其中一单，详情页应显示频道、状态、展示形态、预算 / 冻结 / 已花、投放记录。
7. 停止投放：在详情页点“⏸ 停止投放”并二次确认；订单应变为“已暂停”，冻结预算归零，并收到“投放已暂停”私信。
8. 申诉：用另一单或新建一单完成审核和 `chabo dispatch-due --limit 5` 后，回到订单详情点“🚩 申诉”，提交原因。详情页应回到订单，后台 `disputes` 出现 open 记录，delivery 状态变为 disputed。
9. 套餐升级发票：进入“📦 我的套餐”，当前应为 Pro；点 Enterprise 升级，应收到 Telegram Stars 发票。默认只验发票发送，不支付；如要验支付 fulfillment，需单独记录 Stars 支付流水并使用低额测试账号。

## 6. 网页端手工检查

用浏览器打开：

```text
https://staging.chabo.example/admin
https://staging.chabo.example/advertiser
https://staging.chabo.example/publisher
```

检查：

- 管理端 `/admin#settings` 发布准入为绿色，DB 路径指向 staging。
- 管理端 `/admin#audit` 可按 UAT 时间窗校验审计链，严格模式无断链。
- 管理端订单表能看到批量投放创建的订单、暂停订单和申诉订单。
- 广告主端素材、订单、钱包与 Bot 内操作结果一致。
- 频道主端能看到测试频道、价格档位、收益/投放记录。
- 390px 移动宽度下没有横向溢出、按钮遮挡或文字压出容器。

可补跑 H5 冒烟：

```bash
cd /opt/chabo/web
CHABO_ADMIN_TOKEN=<强随机 token> npm run smoke:h5
```

## 7. UAT 收尾

完成后记录：

```text
机器 gate:
GitHub Actions run:
webhook set time:
Bot 路径截图/消息时间:
发现问题:
修复 PR:
Go / No-Go:
```

如 staging 使用真实 Telegram Stars 发票，清理或标记相关 payment intent；如使用测试频道产生了公开帖子，验收完成后在频道里删除测试广告帖，并保留 Bot / 管理端截图作为发布证据。
