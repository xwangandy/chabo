# AUDIT_02：功能复用与缺陷清单

- **审计日期**：2026-11-05
- **审计范围**：`ChaBo`、`TelegramABC`、`TGWorker` 全量源码与核心业务链路

---

## 一、三项目功能现状、重复模块与技术债盘点

### 1.1 `ChaBo` 功能状态全景盘点 `[已验证]`

| 分类维度 | 模块 / 功能点 | 代码位置与行号证据 | 状态详情 |
| :--- | :--- | :--- | :--- |
| **已完成并可运行** | 整数美分账本与基础余额不变量 | [wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L221-L650)、[db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L12-L38) | 支持 `available` / `reserved` / `spent` 与频道主 `pending` / `confirmed` / `releasable` 六项非负整数余额及流水写入。 |
| **已完成并可运行** | Telegram Stars 支付幂等入账与频道订阅 | [billing.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/billing.py#L19-L260)、[#L409-L517](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/billing.py#L409-L517) | 基于 `telegram_payment_charge_id UNIQUE` 防重；支持 $50 / 2500 Stars 包月免佣订阅。 |
| **已完成并可运行** | 频道三维质量评估与动态刊例引擎 | [channel.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/channel.py#L1102-L1204) | `PricingService.assess_channel()` 基于规模（35%）、质量（40%）、互动（25%）评定 S/A/B/C/D 等级并计算 low/medium/high 三档价格。 |
| **已完成并可运行** | Bot 端深链追踪与顺风车尾注注入 | [bot.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L150-L256)、[#L634-L689](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L634-L689)、[growth.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/growth.py#L1-L778) | 支持 `/start ad_<id>`、`/start ch_<token>`、`/start probe_<id>` 归因，以及原生帖自动挂载招商尾注按钮。 |
| **已完成并可运行** | 三端 React 前端工作台与 Telegram 登录 | [router.tsx](file:///c:/Users/Andy/Antigravity/ChaBo/web/src/app/router.tsx#L28-L51)、[auth.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/auth.py#L14-L53) | `/admin`（10 面板）、`/advertiser`（5 面板）、`/publisher`（4 面板）均可编译运行，支持 Telegram Login Widget HMAC-SHA256 验签。 |
| **部分完成或带病运行** | 自动履约调度器 (`FulfillmentService`) | [fulfillment.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L29-L350) | 能调 Bot API 发帖/置顶/改尾注，但将网络 I/O 包在单个写事务内，且私信失败导致事务回滚与无限重发（**P0 缺陷**）；无到期自动取消置顶/删帖。 |
| **部分完成或带病运行** | 多频道排期计划 (`PlacementPlanService`) | [plans.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/plans.py#L172-L267) | 能建草稿并拆单，但 `submit_plan` 跨多事务非原子执行，忽略 `pin_enabled` 字段，且每条排期重复创建独立 `campaigns`。 |
| **部分完成或带病运行** | 双人充值审批 (`TopupApprovalService`) | [wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L964-L1197) | 虽有 `requester != approver` 校验，但 `approve_topup` 跨 3 个独立事务，存在并发双倍入账漏洞（**P0 缺陷**）。 |
| **部分完成或无法运行** | 频道主提现批次与 10% 质保金释放 | [db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L392-L400)、[wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L853-L889) | `payout_batches` 仅有空表无任何代码读写；`confirm_publisher_earning` 扣留的 10% `holdback_bps` 无释放逻辑，永久锁死。 |
| **已废弃功能** | 正文尾插广告形态 (`light_tail`) | [db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L1030-L1038)、[channel.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/channel.py#L12) | 迁移 `0013_disable_light_tail_format` 已将全库 `light_tail` 设为 `enabled=0`；`OrderService.create_order` 硬编码拒绝 `light_tail`（[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L860-L861)），但 `bot.py` 与 `fulfillment.py#L96-L108` 仍残留死分支。 |
| **已废弃功能** | 旧版单页静态后台 (`web.py`) | [web.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/web.py#L1-L935) | 被 `src/chabo/webapi/` + `web/` 取代，但因 `POST /telegram/webhook` 与 `/api/orders/dispatch-due` 仍滞留在 `web.py` 中，导致无法直接删除。 |

### 1.2 重复实现的模块与关键技术债 `[已验证]`

1. **双 Web 服务端并存与 SQL 逻辑大面积复制粘贴**：
   - `src/chabo/web.py`（935 行，`http.server` 端口 8080）与 `src/chabo/webapi/`（3,405 行，FastAPI 端口 8081）并存。
   - `_parse_json_dict`、`get_order_detail`、`list_audit_logs`、以及 6 表联合的订单时间线拼装逻辑在 [web.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/web.py#L539-L830) 与 [webapi/readmodels.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/readmodels.py#L13-L31)、[#L172-L367](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/readmodels.py#L172-L367)、[#L740-L811](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/readmodels.py#L740-L811) 中**逐行重复**。
   - 更严重的是：**Telegram Webhook 接收端点 `POST /telegram/webhook`（[web.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/web.py#L227-L246)）、自动履约触发端点 `POST /api/orders/dispatch-due`（[#L265](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/web.py#L265)）、收益确认端点 `POST /api/orders/confirm-earnings`（[#L269](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/web.py#L269)）仅存在于旧 `web.py` 中，新 `webapi/main.py` 完全缺失**。
2. **7,104 行 `bot.py` 巨石文件与 `NameError: logger` 崩溃隐患**：
   - [src/chabo/bot.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L1-L7104) 将路由分发、会话状态机、业务校验、键盘渲染全部耦合在单文件中。
   - 在 [#L1265](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L1265)、[#L1349](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L1349)、[#L1429](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L1429)、[#L1495](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L1495)、[#L3991](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L3991) 共 5 处异常分支调用了 `logger.warning(...)`，但全文件**从未 `import logging` 或定义 `logger`**，一旦触发上述异常分支将直接抛出 `NameError: name 'logger' is not defined` 导致 Webhook/轮询崩溃。
3. **两套广告素材表并存（`ad_materials` vs `creatives`）**：
   - 广告主素材库存储在 `ad_materials`（[db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L279-L296)），而每次下单时 `OrderService._create_order_impl`（[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L901-L914)）又把文案和按钮复制一份插入 `creatives` 表（[#L121-L131](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L121-L131)），且每下一单都新建一条孤立的 `campaigns` 记录（[#L892-L900](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L892-L900)），导致活动（Campaign）失去聚合多笔订单的业务意义。

### 1.3 `TelegramABC` 与 `TGWorker` 功能现状与可复用性 `[已验证]`

- **`TelegramABC`**：
  - **可复用服务与 API**：
    1. 只读频道资产网关 `GET /api/plugins/assets/channels`（[plugins.py](file:///c:/Users/Andy/Antigravity/TelegramABC/apps/api/routers/plugins.py#L285-L315)），底层由 [PluginGatewaySDK](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/plugin_gateway/sdk.py#L66-L390) 驱动。
    2. 频道权限矩阵 `AssetAccountPermission`（[permission.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/asset_center/models/permission.py#L19-L64)）与 `BotChannelBinding`（[binding.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/asset_center/models/binding.py#L148-L190)）。
    3. Userbot 真实频道人数与浏览量采集器 `DiscoveryScanner`（[scanner.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/discovery/scanner.py#L196-L238)）与快照表 `AssetMetricSnapshot`（[asset.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/asset_center/models/asset.py#L234-L281)）。
  - **已废弃模块**：`/api/recurring-ad` 所有端点均硬编码返回 `HTTP 410 Gone`（[recurring_ad.py](file:///c:/Users/Andy/Antigravity/TelegramABC/apps/api/routers/recurring_ad.py#L1-L32)），统一收敛至 `/api/tasks`。
- **`TGWorker`**：
  - **可复用模块（代码级提取，不运行时耦合）**：
    1. `FFmpegEngine`（[processing/ffmpeg.py](file:///c:/Users/Andy/Antigravity/TGWorker/processing/ffmpeg.py#L22-L929)）：视频广告素材合规转码、静音视频补音轨防变 GIF、自动水印。
    2. `ContentFilter` & `TextProcessor`（[engine/content_filter.py](file:///c:/Users/Andy/Antigravity/TGWorker/engine/content_filter.py#L1-L270)、[engine/text_processing.py](file:///c:/Users/Andy/Antigravity/TGWorker/engine/text_processing.py#L1-L676)）：广告主素材提交前的前置敏感词与违禁外链静态拦截。

---

## 二、`ChaBo` 四大核心业务域深度审计

### 2.1 三端网页（管理员端 / 广告主端 / 频道主端）审计 `[已验证]`

1. **三端实际存在性与路由**：
   - 前端路由定义于 [web/src/app/router.tsx](file:///c:/Users/Andy/Antigravity/ChaBo/web/src/app/router.tsx#L28-L51)，包含 `/login`、`/admin`、`/advertiser`、`/publisher` 四个一级路由；各端内部通过 URL Hash（[useHashTab.ts](file:///c:/Users/Andy/Antigravity/ChaBo/web/src/hooks/useHashTab.ts#L3-L14)）切换子面板。
2. **身份验证、权限隔离与安全漏洞**：
   - **[P1 高危] 管理员“代看”（Impersonation）具备完整写权限且伪造操作人审计日志**：
     - 管理员通过 `POST /api/admin/impersonate`（[main.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/main.py#L842-L868)）生成带 `impersonator_account_id` 的代看 Session。
     - 前端虽展示“管理员代看中”横幅，但后端 `require_portal`（[deps.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/deps.py#L93-L100)）及所有 `/api/advertiser/*`、`/api/publisher/*` 写接口**完全未拦截代看状态**，允许代看管理员直接创建/提交排期扣款、改刊例价、提工单。
     - 更危险的是，所有写接口均传入 `int(session["telegram_user_id"])`（即**被代看用户**的 Telegram ID，见 [main.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/main.py#L926)、[#L949](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/main.py#L949)、[#L1039](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/main.py#L1039)、[#L1084](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/main.py#L1084)），导致 `tool_call_logs` 将管理员的代操作完全记录在受害用户头上！
   - **[P1 中高危] 管理员隐式跨门户越权与默认最高权限**：
     - `require_portal`（[deps.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/deps.py#L95-L97)）规定只要 `"admin" in portals` 即可无条件访问 `/advertiser` 和 `/publisher` 接口（连只读管理员 `viewer` 也能调写接口）。
     - `admin_level()`（[auth.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/auth.py#L137-L138)）在 `metadata_json` 为空 `{}` 时**默认回退为 `"super_admin"`**，违背最小权限原则。
   - **[P1 中高危] 新注册用户 `candidate` 状态死锁（403 无法进门户）**：
     - 账号同步逻辑 `_sync_role_memberships`（[auth.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/auth.py#L143-L235)）将尚无已批准订单的广告主、尚无已发送订单的频道主标记为 `status = 'candidate'`。
     - 但 `active_portals()`（[#L94-L104](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/auth.py#L94-L104)）仅放行 `status == 'active'` 的角色！这造成“新广告主必须先有已批准订单才能进广告主后台下单”的死循环，首次通过 Telegram 登录的新用户会因 `active_portals == []` 被前端 [RequirePortal.tsx](file:///c:/Users/Andy/Antigravity/ChaBo/web/src/components/RequirePortal.tsx#L21-L27) 困死在 `/login`。
3. **Web 端业务能力缺口与口径冲突**：
   - **频道主自主改价违背“三档选择”规范，且改档位不生效**：
     - 规范要求频道主仅可在 `low`/`medium`/`high` 三档间切换，不可填任意金额；但 Web API 暴露了 `PATCH /api/publisher/channels/{id}/rate`（[main.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/main.py#L1121-L1135)）允许频道主直接把 `unit_price_cents` 改为 `1..10,000,000` 任意值。
     - 反过来，当频道主通过规范接口切换 `owner_price_band`（`set_format_policy`，[channel.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/channel.py#L292-L348)）时，代码**只改了 `channel_format_policies` 表，未同步更新 `rate_cards.unit_price_cents`**；而下单计费（`OrderService._create_order_impl`，[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L867)、[#L889](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L867)）读取的却是 `rate_cards.unit_price_cents`！
   - **Web 端缺失高频功能**：无频道绑定/接入入口；素材库仅支持新建纯文本+URL，不支持上传图片/视频或调用已有的 `update_material`（[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L247-L365)）；广告主订单页无暂停、取消、发起争议按钮；无效果报告页。

---

### 2.2 资金账本与财务合规审计 `[已验证]`

| 检查项 | 审计结论 | 代码证据与实测结果 |
| :--- | :--- | :--- |
| **充值申请与双人审批** | **存在并发双倍入账漏洞 (P0)** | `TopupApprovalService.approve_topup`（[wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L1021-L1063)）虽然检查了 `requester_telegram_user_id != approver_telegram_user_id`（`#L1035`），但将「读取 `status=='pending'`」「调用 `manual_topup` 入账」「更新 `status='approved'`」拆在 **3 个独立事务** 中。`test_01` 实测证实：两个管理员并发审批同一笔 5,000 cents 申请，余额直接变成 10,000 cents。 |
| **手工充值参数校验** | **允许负数充值扣减余额 (P0)** | `LedgerService.manual_topup`（[wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L435-L482)）缺少 `if amount_cents <= 0` 校验。`test_02` 实测证实：传入 `-3000` 直接将账户可用余额从 5,000 减至 2,000。 |
| **可用余额与冻结余额** | **暂停单重审可绕过资金冻结 (P0)** | 下单时 `reserve_order_budget`（[wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L510-L556)）正确将 `available` 转为 `reserved`。但当订单执行 `pause_and_release`（[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1490-L1509)）全额退回冻结款（`reserved_cents=0`）后，`approve_order`（[#L1122-L1145](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1122-L1145)）竟允许 `status in {"pending_review", "paused"}` 直接改为 `approved` 并生成待发排期，**全程未重新冻结任何资金**（`test_07a` 实测证实）。 |
| **执行成功扣款与平台抽佣** | **平台服务费在 100% 路径下恒为 0 (P0)** | 在 `OrderService._create_order_impl`（[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L878-L888)）与 `_effective_service_fee_bps`（[#L1855-L1865](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1855-L1865)）中：未买订阅的频道强制 `platform_promo_enabled = 1`（免服务费 `0 bps`），买了订阅的频道 `has_active_subscription = True`（亦免服务费 `0 bps`）。导致 5%（`500 bps`）平台服务费在任何可达状态下永远为 0！且下单时还会直接修改全局 `channel_configs.service_fee_bps`。 |
| **收益确认与频道重绑冲突** | **重绑频道导致全量结算崩溃 (P0)** | `charge_delivery`（[wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L791-L851)）将待确认收益记在发帖当时的 `channels.owner_account_id` 账上；若 24h 内频道被另一管理员通过 `bind_channel`（[channel.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/channel.py#L82-L90)）或 `my_chat_member`（[bot.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L593-L604)）覆盖了 `owner_account_id`，`confirm_due_earnings`（[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1217-L1254)）去扣新号 `pending_earnings_cents` 时必抛 `InvalidState("频道主待确认收益不足")`，并回滚整批结算事务（`test_07b` 实测证实）。 |
| **10% 质保金与提现结算** | **质保金永久锁死 & 提现未实现 (P1)** | `confirm_publisher_earning`（[wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L853-L889)）按 `settlement_policies.holdback_bps = 1000` 扣留 10% 不计入 `releasable_earnings_cents`，但全库**没有任何定时任务或方法在 7 天后释放该 10% 质保金**；`payout_batches` 亦无实现。 |
| **失败退款与争议退款** | **争议全额退款会透支已确认收益** | 发帖失败调用 `pause_and_release` 仅退未消费冻结款；但若在 `confirm_due_earnings`（24h 确认收益）**之后**管理员裁决争议全额退款（`resolve_dispute`，[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1406-L1488)），`refund_delivery`（[wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L891-L961)）在 `pending_earnings_cents` 已变为 0 时无法追回频道主已确认收益，却仍给广告主退全款，造成平台资金池净亏损。 |
| **防篡改审计日志 (`audit_logs`)** | **核心资金操作漏记哈希审计链 (P1)** | `insert_audit_log`（[audit.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/audit.py#L31-L71)）**从未在充值申请、充值审批、手工调账、Stars 支付、下单冻结、履约扣款中被调用**（上述操作仅写了无哈希的 `ledger_entries` 或 `tool_call_logs`）；且 `verify_audit_chain`（`#L74`）默认 `strict_unsigned=False`，跳过 `audit_hash IS NULL` 的伪造行。 |

---

### 2.3 Telegram 投放执行与调度幂等性审计 `[已验证]`

重点审查 [src/chabo/fulfillment.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L1-L351)、[src/chabo/webapi/plans.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/plans.py#L1-L348) 与 [src/chabo/telegram.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/telegram.py#L1-L461)：

1. **[P0 致命] 单事务包裹批量外部 HTTP 调用 + 私信未捕获异常 = 无限重复发帖 (`fulfillment.py#L29-L48, #L186-L196, #L334-L350`)**：
   - `dispatch_due()` 在第 33 行开启**单个** `with self.db.transaction() as conn:`（即 `BEGIN IMMEDIATE` 独占写锁），在循环内对最多 20 条到期 `deliveries` 依次执行 `_dispatch_one()`。
   - 每条投递在事务内同步发起 3～6 次 Telegram HTTP 请求（`sendMessage`/`sendPhoto`、`pinChatMessage`、频道主私信、广告主私信、预算告警私信），单次超时高达 20s + 25s `curl` 回退（[telegram.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/telegram.py#L151-L178)）。这意味着**单次 `dispatch_due` 可持有 SQLite 全局写锁长达数分钟**，阻塞所有 Web 与 Bot 写入！
   - 更致命的是：第 186～195 行给频道主和广告主发投递成功通知时包了 `try ... except TelegramError`，但紧接着第 196 行调用 `self._maybe_notify_budget(conn, updated_order)`（[#L334-L350](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L334-L350)）：当订单剩余冻结预算 `<= 20%`（**所有单次投放订单发完后剩余预算均为 0%，100% 触发此分支！**）时，第 345 行调用 `self.gateway.send_private_message(...)` **没有任何 `try/except`**！
   - 一旦广告主是通过 Web 登录但从未在 Telegram 私聊启动过 Bot、或拉黑了 Bot、或该次私信网络抖动，第 345 行抛出 `TelegramError`，直接穿透 `dispatch_due` 导致**整个 SQLite 事务回滚**——此时频道里广告已经发出并置顶，但数据库回滚回 `status='scheduled'`、扣费回滚为 `0`（不仅回滚当前订单，还会把同一批次前序已发出的所有订单全部回滚！），下一轮调度继续重复发帖（已在 `test_03` 中 100% 复现）。
2. **[P0 致命] 无 `sending` 预占态与 `reconciliation_required` 未知态，置顶失败或响应超时即白嫖 (`fulfillment.py#L72-L162`)**：
   - 状态机仅有 `scheduled -> sent / failed`。
   - 若 `send_ad` 请求在 Telegram 服务端已成功发出消息，但客户端读取响应超时，或 `pin24h` 格式在 `send_ad` 成功后执行 `pin_message` 失败（如 Bot 缺 `can_pin_messages` 权限，`#L140-L146`），异常均被外层 `except TelegramError` 捕获并调用 `_mark_delivery_failed`：将状态标为 `'failed'`，`message_id` 留空，并调用 `pause_and_release` **全额退回广告主冻结款**（已在 `test_04` 中 100% 复现）。
3. **[P1 高危] 无消息到期取消置顶 (`unpin`) 与自动删除 (`deleteMessage`) 机制**：
   - `pin24h` 广告置顶后，全库没有任何记录 `unpin_at` / `delete_at` 的定时任务，[telegram.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/telegram.py#L190-L408) 的 `BotApiClient` 甚至未封装 `unpinChatMessage` 和 `deleteMessage` 方法。
4. **[P1 高危] 多频道批量排期 (`submit_plan`) 非原子、忽略 `pin_enabled`、频次排期失灵**：
   - `submit_plan`（[plans.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/plans.py#L172-L267)）在循环中逐条调用 `app.orders.create_order`（每条各自独立事务），若第 K 条因频道下架或黑名单报错，前 K-1 条已扣款生成孤儿订单，而排期计划仍停留在 `draft`（`test_05a` 实测证实）。
   - `placement_plan_items.pin_enabled`（`#L153`）在 `submit_plan`（`#L222-L234`）生成订单时被完全忽略。
   - `maybe_schedule_next`（[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1690)）硬编码 `base_dt + timedelta(days=1)`，完全忽略了 `frequency_per_day`（当 `frequency_per_day=2` 时收了每天 2 次的钱，却仍按每天 1 次排期，`test_05b` 实测证实）。
   - `channel_configs` 中的 `daily_ad_limit`、`allowed_start_hour`、`allowed_end_hour`、`allow_pin`、`category_blocklist_json`（[db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L50-L62)）在下单和履约时**均未执行校验**。

---

### 2.4 数据采集与效果报告能力逐项核验 `[已验证]`

严格区分 **`[真实实现]`**、**`[Mock]`**、**`[预留接口]`** 与 **`[完全缺失]`**：

| 序号 | 数据采集能力项 | 审计判定状态 | 代码路径、行号与详细证据 |
| :--- | :--- | :--- | :--- |
| 1 | **广告浏览数据采样**<br>(`message_views`, 1h/6h/24h/48h 快照) | **`[预留接口]`** | `metric_snapshots` 表已建（[db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L363-L370)），但全库仅在用户点击 `/start ad_<id>` 时写入 `metric_type='bot_start'`（[bot.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L155-L160)）；Bot API 无法读取频道贴浏览量，且当前未接入 MTProto 采样任务，`message_views` 数据为 0。 |
| 2 | **Bot 深链追踪**<br>(`/start ad_*`, `ch_*`, `probe_*`, `sp_*`) | **`[真实实现]`** | [bot.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L150-L256) 完整实现了 4 类深链解析与落库：`ad_<delivery_id>` 记入 `metric_snapshots`；`ch_<ref_token>` 记入 `advertiser_sessions`；`probe_<id>` 记入 `light_probe_events`；`sp_<id>` 触发顺风车冠名购买。 |
| 3 | **站内交互事件**<br>(尾注按钮挂载、三按钮键盘、顺风车监听) | **`[真实实现]`** | `channel_post` 自动识别原生帖并调用 `edit_message_reply_markup` 挂载招商按钮（[bot.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L634-L689)）；`fulfillment.py` 支持 `button_tail` 挂载（[#L88-L113](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L88-L113)）及三按钮布局（`[📣 频道招商] [🔍 查看详情]` + `[广告主 CTA]`，[#L198-L213](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L198-L213)）。 |
| 4 | **HTTP 短链追踪**<br>(`/r/{token}` 重定向与点击去重) | **`[完全缺失]`** | [fulfillment.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L212) 将广告主原始 `creative["target_url"]` 直接作为 Inline Button URL 发出，用户点击外链直接跳转目标站，不经过 `ChaBo`，无点击日志表或 `/r/{token}` 路由。 |
| 5 | **频道统计**<br>(订阅数、24h 中位阅读、三维评分) | **`[真实实现]` (评分引擎)<br>`[Mock / 手工录入]` (基础指标)** | `PricingService.assess_channel`（[channel.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/channel.py#L1102-L1204)）算法为真实实现；但输入的 `subscribers` 与 `median_24h_views` 依赖 CLI 手工传入或绑定时的静态默认值（[telegram.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/telegram.py#L190-L408) 未调用 `getChatMemberCount`，也未接 `TelegramABC`）。 |
| 6 | **转化回传**<br>(Postback / Webhook 外部转化归因) | **`[完全缺失]`** | 全库无转化回传接口、无转化事件表、无 S2S Postback Token 机制。 |
| 7 | **效果报告**<br>(多维汇总、频道排行、分享页、复投建议) | **`[部分实现且含 Bug]` (服务层/Bot)<br>`[完全缺失]` (Web 端/分享页)** | `AdvertiserService.report`（[billing.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/billing.py#L907-L976)）在 CLI/Bot 中可用，但第 946～962 行 `ad_orders o LEFT JOIN deliveries d` 存在 **SQL JOIN 笛卡尔膨胀 Bug**（`test_06` 证实花费被放大 N 倍）；Web 端 `/advertiser` 无报告页，无免登录分享页 `/reports/:token`，无优化建议与一键复投。 |

---

## 三、全量缺陷主清单（按严重度 P0 / P1 / P2 排序）`[已验证]`

| 编号 | 级别 | 所属模块 | 缺陷标题与风险后果 | 精确文件路径与行号 | 验证状态 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **DEF-01** | **P0** | 履约执行 | `dispatch_due` 单事务包裹外部 HTTP 请求，且 `_maybe_notify_budget` 私信失败未捕获 `TelegramError`，导致事务回滚、无限重复向频道发帖且不扣款 | [fulfillment.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L29-L48), [#L186-L196](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L186-L196), [#L334-L350](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L334-L350) | `[已验证]` (`test_03`) |
| **DEF-02** | **P0** | 履约执行 | 缺少 `sending` 预占态与 `reconciliation_required` 状态；`send_ad` 超时或 `pin24h` 置顶失败会将已发出的广告标为 `failed`、丢弃 `message_id` 并全额退款 | [fulfillment.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L72-L162) | `[已验证]` (`test_04`) |
| **DEF-03** | **P0** | 资金账本 | `TopupApprovalService.approve_topup` 跨 3 个独立事务执行（读状态→入账→改状态），并发点击或中途崩溃导致同一笔充值双倍/多倍入账 | [wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L1021-L1063) | `[已验证]` (`test_01`) |
| **DEF-04** | **P0** | 资金账本 | `LedgerService.manual_topup` 未校验 `amount_cents > 0`，传入负数直接静默扣减账户可用余额 | [wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L435-L482) | `[已验证]` (`test_02`) |
| **DEF-05** | **P0** | 订单与账本 | `pause_and_release` 释放全部冻结款后，`approve_order` 仍允许将 `paused` 订单直接改回 `approved`（`reserved_cents = 0` 空头支票排期） | [order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1122-L1145), [#L1490-L1509](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1490-L1509) | `[已验证]` (`test_07a`) |
| **DEF-06** | **P0** | 结算与频道 | 频道被另一管理员重绑（`bind_channel` / `my_chat_member`）会直接覆盖 `channels.owner_account_id`，导致 24h 后 `confirm_due_earnings` 扣减新号待确认收益失败并阻断整批结算 | [channel.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/channel.py#L82-L90), [bot.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L593-L604), [wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L861-L867) | `[已验证]` (`test_07b`) |
| **DEF-07** | **P0** | 计费模型 | 未订阅频道强制开启引流免佣（`service_fee_bps=0`），已订阅频道亦免佣（`0`），导致 5% 平台服务费在 100% 可达路径下恒为 0 | [order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L878-L888), [#L1855-L1865](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1855-L1865) | `[已验证]` |
| **DEF-08** | **P1** | 权限与审计 | 管理员“代看”模式未限制写操作，且将 `session["telegram_user_id"]`（被代看用户 ID）写入业务表与 `tool_call_logs` 伪造操作人 | [auth.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/auth.py#L379-L429), [deps.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/deps.py#L93-L100), [main.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/main.py#L918-L1135) | `[已验证]` |
| **DEF-09** | **P1** | 门户鉴权 | 新用户默认 `status='candidate'`，被 `active_portals()` 排除，导致新广告主/频道主登录 Web 后被 403 死锁无法自助下单或绑频道；同时 `admin_level()` 空元数据默认回退 `super_admin` | [auth.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/auth.py#L94-L104), [#L137-L138](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/auth.py#L137-L138), [#L143-L235](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/auth.py#L143-L235) | `[已验证]` |
| **DEF-10** | **P1** | 数据库层 | `with self.db.connect() as conn:` 不关闭 SQLite 连接句柄，导致 Windows 下 110/189 个单测报 `WinError 32` 且长跑进程泄漏句柄；`PRAGMA busy_timeout = 5000` 覆盖了 30s timeout | [db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L666-L698), [readmodels.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/readmodels.py#L43) | `[已验证]` |
| **DEF-11** | **P1** | 排期与规则 | `submit_plan` 循环拆单非原子、忽略 `pin_enabled`；`maybe_schedule_next` 忽略 `frequency_per_day` 固定 `+1 day`；`channel_configs` 日限额/时段/黑名单未生效 | [plans.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/plans.py#L153), [#L172-L267](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/plans.py#L172-L267), [order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1690) | `[已验证]` (`test_05`) |
| **DEF-12** | **P1** | 效果报告 | `AdvertiserService.report` 中 `ad_orders LEFT JOIN deliveries` 未按订单聚合直接 `SUM(o.spent_cents)`，导致单频道花费和订单数按投递条数 N 倍膨胀 | [billing.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/billing.py#L946-L962) | `[已验证]` (`test_06`) |
| **DEF-13** | **P1** | 结算与审计 | 10% `holdback_bps` 质保金永久不释放；`payout_batches` 未实现；24h 确认收益后若发生争议全额退款会透支平台资金；核心资金操作未写入哈希链 `audit_logs` | [wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L853-L961), [audit.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/audit.py#L31-L153) | `[已验证]` |
| **DEF-14** | **P1** | 定价与刊例 | Web 暴露 `PATCH /api/publisher/channels/{id}/rate` 允许频道主填任意金额（违反三档制）；而修改 `owner_price_band` 又不同步更新 `rate_cards.unit_price_cents` | [main.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/main.py#L1121-L1135), [channel.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/channel.py#L292-L348) | `[已验证]` |
| **DEF-15** | **P1** | Bot 与路由 | `bot.py` 5 处异常捕获调用未定义的 `logger.warning` 抛 `NameError`；`webapi/main.py` 缺失 `/telegram/webhook` 与 `dispatch-due` 路由 | [bot.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L1265), [#L1349](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L1349), [#L1429](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L1429), [#L1495](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L1495), [#L3991](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/bot.py#L3991), [web.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/web.py#L227-L277) | `[已验证]` |
| **DEF-16** | **P2** | 部署与配置 | `Dockerfile` 执行 `pip install .` 漏装 `.[web]` 导致容器内无 FastAPI；`CHABO_BOT_AUTO_APPROVE_ORDERS` 默认 `True` 且未在 `.env.example` 与 `preflight` 中告警 | [Dockerfile](file:///c:/Users/Andy/Antigravity/ChaBo/Dockerfile#L21), [config.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/config.py#L40), [#L64](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/config.py#L64) | `[已验证]` |
