# 插播（广告插播）MVP

这是“插播”Telegram 频道广告可信履约平台的一期工程骨架。当前版本聚焦固定价/按时间/按广告位的闭环：频道接入、来源归因、广告订单、预算冻结、发布成功扣费、频道主收益记录、服务费、证据快照和基础争议处理。

## 项目准绳

产品、定价、账务、风控和后续开发的唯一主施工图是：[DOC/插播_项目施工图.md](DOC/插播_项目施工图.md)。

如果施工图、README、代码实现之间出现冲突，以施工图为准；如果施工图本身需要改变，先修改施工图，再修改代码和测试。`DOC/原始需求文档/` 中的资料只作为历史背景和研究参考。

## 已实现

- 单业务 Bot 架构，不拆独立钱包 Bot。
- SQLite 硬账本：广告主 `available/reserved/spent`，频道主 `pending/confirmed/releasable`。
- 频道 `ref_token` 与按钮链接：`https://t.me/<bot>?start=<channel_token>`。
- 自动给频道新帖追加“在本频道插播广告”按钮，并保留原有 inline buttons。
- 广告主从 deep link 进入后建立来源归因 session。
- 固定刊例价订单；旧版广告位仍兼容，v1 交互以“文字/标准/定制 + 置顶/发布周期”的投放矩阵为准。
- 预算预冻结，发布成功后扣费；发布或置顶失败会暂停订单并释放剩余预算。
- 运营 Admin 最小闭环：审核通过、审核拒绝释放预算、已扣费投放全额退款、争议列表和人工裁决。
- 平台服务费按规则生效：保留推广按钮或开通频道高级订阅可免服务费，否则可收默认 5%；默认 10%/7 天收益保留金参数已入库。
- 证据链：素材快照、频道配置快照、价格快照、发送日志、账务流水。
- Stars 发票、pre-checkout 校验、`successful_payment` 幂等履约，以及人工入账 CLI。
- Bot polling 运行器：支持真实 `getUpdates`、offset 持久化、callback query 和菜单按钮。
- Bot v1 投放配置器：频道 deep link 会直接进入“给当前频道投放广告”的状态卡，可配置展示形态、发布周期、广告素材和费用确认；旧版预算输入表单仍保留为兼容路径。
- Bot 频道主接入向导：频道主转发频道消息后，系统检查频道主/Bot 权限，绑定频道，展示插播入口、默认报价和展示形态开关。
- 内部三种展示形态字段：`light_tail`、`standard_card`、`strong_post`；用户界面应显示“文字插播、标准插播、定制插播”，`pin24h`、`loop_daily` 只作为投放设置兼容字段。
- 频道定价评估、低/中/高价格档、广告主砍价报价。
- 频道主高级订阅：按频道订阅人数计算月费，控制关闭推广按钮/自定义价格等高级功能。
- 频道主接受砍价后，系统自动创建待审核订单并冻结广告主预算。
- 文字插播探针：频道新帖自动追加低打扰探针按钮，统计点击和独立点击用户。
- 广告主高级服务：优质频道发现、频道收藏、新频道提醒、批量投放、投放报表、套餐权限和到期限制。
- 同一 Telegram 用户可同时作为广告主和频道主，账户角色会合并为 `mixed`。
- 真实频道联测已验证：频道帖自动追加入口、deep link 归因、自助下单、审核、发布、扣费、低预算提醒、广告详情点击归因、争议和退款。
- 广告库服务层：广告素材（creatives）按广告主独立归属、按形态（文字/标准/定制）分类、可归档；`MaterialService` 是 Bot、CLI、Admin 和未来 AI 助理共用的素材入口，所有方法都自带归属校验。下单、批量下单、砍价成交都已经走同一套素材创建路径。
- 人工入账双人复核：`request-topup` 写一行 pending 不动账本；`approve-topup` 必须由不同账号执行才会调 `manual_topup` 入账；`reject-topup` 不动钱。`/admin` 顶部"待审入账"一栏、CLI 列表都能看到全部待审请求；所有动作都进 `tool_call_logs` 审计。
- 生产部署样例：`DOC/部署/systemd-chabo.service`、`DOC/部署/nginx-chabo.conf`、`DOC/部署/token-rotation.md` 给出 systemd 服务 / nginx 反向代理 / admin & webhook secret 轮换的完整样板和应急清单。

## 快速开始

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
chabo init-db
```

生产部署前自检（CI / 部署脚本可 gate）：

```bash
# 任一 critical 项失败时退出码非零
chabo preflight --host <对外 host>
```

回归清单（人工 + 自动）见 [DOC/部署/regression-checklist.md](DOC/部署/regression-checklist.md)；端到端自动化测试见 `tests/test_chabo_mvp.py::test_phase_one_golden_path_end_to_end`。

定期备份（推荐 cron）：

```bash
# 自动写到 <db_dir>/backups/chabo-YYYYMMDD-HHMMSS.sqlite3
chabo backup-db
# 或显式路径
chabo backup-db --target /var/backups/chabo/snapshot.sqlite3
```

创建广告主并人工入账（开发 / 单人入账走 `topup`；生产推荐走双人复核）：

```bash
chabo topup --telegram-user-id 10001 --display-name "广告主A" --amount 100
```

生产环境的人工入账走双人复核，运营 A 提交、运营 B 审批：

```bash
chabo request-topup \
  --recipient-telegram-user-id 10001 \
  --amount 100 \
  --reason "OTC 收到 100 USDT，转入插播余额" \
  --requester-telegram-user-id <运营A_TG_ID> \
  --evidence-url "https://evidence.example/screenshot.png"

chabo list-topup-requests --status pending

chabo approve-topup \
  --request-id <treq_id> \
  --approver-telegram-user-id <运营B_TG_ID> \
  --note "对账已核"
# approve 调用必须由不同的运营员执行；同一账号 approve 会被服务层拒绝。

chabo reject-topup \
  --request-id <treq_id> \
  --approver-telegram-user-id <运营B_TG_ID> \
  --note "金额对不上凭证"
```

绑定频道并查看插播入口：

```bash
chabo bind-channel --telegram-chat-id -100123456 --title "示例频道" --username example_channel --owner-telegram-user-id 20001
```

生成频道定价评估，并把三种展示形态的报价写入刊例价：

```bash
chabo assess-channel \
  --channel <ref_token> \
  --category software \
  --median-24h-views 20000 \
  --subscribers 50000 \
  --light-unique-clickers-30d 120 \
  --repeat-purchase-count 2

chabo quote-channel --channel <ref_token> --slot-type standard_card
chabo apply-pricing --channel <ref_token>
```

频道主可开关展示形态，或选择低/中/高档：

```bash
chabo set-format-policy --channel <ref_token> --format-type strong_post --no-enabled
chabo set-format-policy --channel <ref_token> --format-type standard_card --owner-price-band high
```

广告主可发起砍价报价，频道主再接受或拒绝：

```bash
chabo make-offer \
  --advertiser-telegram-user-id 10001 \
  --channel <ref_token> \
  --slot-type standard_card \
  --amount 3 \
  --budget 3 \
  --text "这里是砍价插播广告文案" \
  --target-url "https://example.com" \
  --message "三美金我马上投"

chabo respond-offer --offer-id <offer_id> --accept
```

频道主接受砍价后会立即生成 `pending_review` 订单并冻结广告主预算；如果广告主余额不足，报价会保持 `pending`，不会生成订单。

频道主高级订阅按频道订阅人数计费：

```bash
chabo quote-subscription --subscribers 10001
chabo topup --telegram-user-id <channel_owner_user_id> --amount 10 --display-name "频道主"
chabo purchase-subscription --channel <ref_token> --subscribers 10001 --months 1
chabo show-subscription --channel <ref_token>
```

`activate-subscription` 保留为运营后台手动开通入口；正式购买路径使用 `purchase-subscription`，会从频道主余额扣款并把收入写入平台账本。

创建文字插播探针并查看点击统计：

```bash
chabo create-probe \
  --channel <ref_token> \
  --short-text "想在这个频道投广告？" \
  --detail-text "这里是文字插播详情页文案" \
  --target-url "https://example.com" \
  --button-text "想投广告？"

chabo probe-stats --channel <ref_token>
chabo pause-probe --probe-id <probe_id>
```

广告主高级服务：

```bash
chabo quote-advertiser-plan --plan pro
chabo topup --telegram-user-id 10001 --amount 50 --display-name "广告主"
chabo purchase-advertiser-plan --advertiser-telegram-user-id 10001 --plan pro --months 1
chabo show-advertiser-plan --advertiser-telegram-user-id 10001

chabo discover-channels \
  --advertiser-telegram-user-id 10001 \
  --category software \
  --min-score 70 \
  --max-risk-level normal \
  --slot-type standard_card

chabo save-channel --advertiser-telegram-user-id 10001 --channel <ref_token> --note "优先测试"
chabo list-saved-channels --advertiser-telegram-user-id 10001

chabo create-alert-rule --advertiser-telegram-user-id 10001 --category software --min-score 70
chabo scan-alerts --advertiser-telegram-user-id 10001
chabo list-alerts --advertiser-telegram-user-id 10001

chabo batch-orders \
  --advertiser-telegram-user-id 10001 \
  --channel-tokens <ref_token_1>,<ref_token_2> \
  --slot-type standard_card \
  --text "批量插播广告文案" \
  --target-url "https://example.com" \
  --budget 10

chabo advertiser-report --advertiser-telegram-user-id 10001
```

未购买广告主高级服务时，频道发现只返回免费额度；频道提醒、批量投放和完整报表需要 Pro 或 Enterprise 套餐。当前套餐会从广告主插播余额扣款，后续接 Stars 支付时复用同一套订阅和权限逻辑。

发送 Stars 发票：

```bash
chabo send-stars-topup-invoice --telegram-user-id 10001 --stars 500 --display-name "广告主"

chabo send-publisher-subscription-invoice \
  --channel <ref_token> \
  --subscribers 10001 \
  --months 1

chabo send-advertiser-plan-invoice \
  --advertiser-telegram-user-id 10001 \
  --plan pro \
  --months 1

chabo show-stars-payment-intent <intent_id_or_payload>
```

Stars 支付会先创建 `stars_payment_intents`，再发送 `currency=XTR` 的发票；Bot 收到 `pre_checkout_query` 时校验用户、金额和状态，收到 `successful_payment` 后按支付意图充值余额或开通订阅。`CHABO_STAR_CREDIT_CENTS` 用于配置 1 Star 折算多少内部余额 cents，默认 1。

把素材保存到广告库并复用：

```bash
chabo create-material \
  --advertiser-telegram-user-id 10001 \
  --format-type standard_card \
  --text "标准插播文案 v1" \
  --target-url "https://example.com"

chabo create-material \
  --advertiser-telegram-user-id 10001 \
  --format-type light_tail \
  --light-short-text "想投这里？" \
  --text "完整文字插播详情文案" \
  --target-url "https://example.com"

chabo list-materials --advertiser-telegram-user-id 10001
chabo show-material --material-id <material_id> --advertiser-telegram-user-id 10001
chabo archive-material --material-id <material_id> --advertiser-telegram-user-id 10001
```

`MaterialService` 是 Bot、CLI、Admin 和未来 AI 助理共用的素材入口，所有方法都按 `advertiser_telegram_user_id` 校验归属。文字插播必须配 2-15 字短入口；标准/定制插播不带短入口。归档后的素材不能再用于新订单，但仍能通过 `--include-archived` 看到。

创建订单、审核并调度：

```bash
# 直接复用广告库素材
chabo create-order \
  --advertiser-telegram-user-id 10001 \
  --channel-token <ref_token> \
  --slot-type standard_card \
  --material-id <material_id> \
  --budget 20

# 仍兼容内联文案路径；新建的素材会自动进入广告库
chabo create-order \
  --advertiser-telegram-user-id 10001 \
  --channel-token <ref_token> \
  --slot-type standard_card \
  --text "这里是插播广告文案" \
  --target-url "https://example.com" \
  --budget 20

chabo approve-order --order-id <order_id>
chabo dispatch-due
```

运营审核、拒绝、退款和争议裁决：

```bash
chabo reject-order --order-id <order_id> --reason "素材不符合插播规范"
chabo refund-delivery --delivery-id <delivery_id> --reason "频道主提前删除广告"
chabo refund-delivery --delivery-id <delivery_id> --amount 3.50 --reason "部分补偿广告主"
chabo list-disputes --status open
chabo resolve-dispute --dispute-id <dispute_id> --resolution "证据不足，恢复投放记录"
```

## HTTP Webhook 与运营后台

插播现在内置一个标准库 HTTP 服务，不额外引入 Web 框架。它提供：

- `GET /health`：健康检查；带 DB ping 和运营计数（`pending_review_orders / running_orders / sent_today / scheduled_due / open_disputes / failed_recent`）。可被 LB 或监控系统直接用作 readiness 信号。
- `POST /telegram/webhook/<secret>`：Telegram webhook update 入口。
- `GET /admin?token=<admin_token>`：轻量运营后台页面，顶部有六张运营摘要卡片（与 `/health` 同一组数据），告警计数会高亮成红色。
- `GET /admin/orders|disputes|deliveries`：运营 JSON 查询。
- `GET /admin/orders/<order_id>|deliveries/<delivery_id>|disputes/<dispute_id>`：详情页，包含关联对象、证据链和账本流水。
- `POST /admin/orders/<order_id>/approve|reject`、`POST /admin/deliveries/<delivery_id>/refund`、`POST /admin/dispatch-due`、`POST /admin/confirm-earnings`：运营动作。

`run-web` 启动时会跑 `check_token_strength`：loopback 绑定可零配置；外部 IP 上缺 `CHABO_ADMIN_TOKEN` / `CHABO_WEBHOOK_SECRET`，或 token 短于 16 字符或包含 `test/demo/changeme/secret` 等弱关键字，启动日志会打印明确告警，提示生产前换强随机值。

投放退款支持两种方式：不传 `amount` 时退还该投放剩余可退金额；传 `amount` 时执行部分退款，并按比例回滚频道主待确认收益和平台服务费。

本地启动：

```bash
CHABO_ADMIN_TOKEN=<admin_token> \
CHABO_WEBHOOK_SECRET=<webhook_secret> \
chabo run-web --host 127.0.0.1 --port 8080
```

健康检查和后台：

```bash
curl http://127.0.0.1:8080/health
open "http://127.0.0.1:8080/admin?token=<admin_token>"
```

如果有公网 HTTPS 地址，可以把 Telegram webhook 指向该服务：

```bash
chabo set-webhook \
  --url "https://your-domain.example/telegram/webhook/<webhook_secret>" \
  --secret "<webhook_secret>"
```

后台接口也支持 `Authorization: Bearer <admin_token>` 或 `X-Chabo-Admin-Token: <admin_token>`。生产部署时必须配置强随机 `CHABO_ADMIN_TOKEN` 和 `CHABO_WEBHOOK_SECRET`。

本地真实 Bot 测试可以先用 polling：

```bash
CHABO_BOT_TOKEN=<test_bot_token> \
CHABO_BOT_USERNAME=ChaBoADBot \
chabo run-polling
```

测试拉一轮 update：

```bash
CHABO_BOT_TOKEN=<test_bot_token> \
CHABO_BOT_USERNAME=ChaBoADBot \
chabo run-polling --once --timeout 1
```

Bot 支持 `/menu`，会展示双身份“📌 插播工作台”：顶部是“➕ 添加频道”，下面是“📺 频道管理 / 📣 我的广告”“💸 我的收益 / 💰 广告钱包”“💵 定价规则 / 🌐 时区”。核心菜单使用短文案和 emoji 引导；主动作整行展示，次级动作两列并排。点击 inline 按钮后会先返回“处理中...”，并优先原地更新当前 Bot 消息，减少刷屏。频道 deep link 进入后，会直接进入“投放配置器”，先完成“给当前频道投广告”：展示设置、发布设置、广告素材、费用确认。钱包和广告库不应抢在第一屏，只有选择素材或余额不足时才进入对应流程。

频道主点击“🔌 手动接入”后会进入接入向导：先把 Bot 加为频道管理员并授予发消息、编辑消息权限，再从频道转发任意消息给 Bot。Bot 会识别频道、检查频道主与 Bot 权限，成功后绑定频道并展示“展示形态”配置入口。

首次 `/start` 会先确认时区，默认 `Asia/Shanghai`；用户可输入北京、Manila、Asia/Tokyo、Europe/Rome 等城市或 IANA 时区名。频道主资产识别：Bot 的 polling/webhook 会接收 `my_chat_member` 和 `chat_member` update。Bot 被加入频道或升为管理员后，会自动绑定频道资产、同步频道管理员列表，并尝试通知已经能私聊 Bot 的管理员。频道主无参数 `/start` 时，如果当前账号是已接入频道管理员，会直接进入“📺 频道管理”并列出频道资产；点击频道可查看入口链接、中文展示形态价格、权限状态和展示形态配置。

`CHABO_TELEGRAM_HTTP_BACKEND=auto` 会优先使用 Python `urllib`，遇到本机证书链问题时自动退回系统 `curl`。

## 支付策略

Telegram 内数字服务建议使用 Stars。当前代码支持 Stars 支付意图、发票发送、pre-checkout 校验和成功支付后的余额充值/频道订阅/广告主套餐履约。旧版 `topup:<account_id>` payload 仍保留兼容，但正式路径应使用 `stars_payment_intents`。USDT 一期建议只做站外人工收款，然后使用 `chabo topup` 人工入账，不在 Bot 内做钱包充值/提现闭环。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

Bot 交互功能完成后，还要用真实测试 Bot 验证菜单、按钮、状态流转和错误提示。涉及 Stars 付款、真实频道权限、真实发频道消息等动作时，只做非资金验收或等用户确认后再操作。

## 多窗口协作

主 Codex 窗口负责需求、拆分、验收和合并；执行 Codex 窗口在独立 git worktree 中按任务包开发。协作准绳见 [DOC/Codex_多窗口协作工作流.md](DOC/Codex_多窗口协作工作流.md)。当前目录若尚未初始化 git，需要先建立基线提交，再使用 worktree 并行开发。
