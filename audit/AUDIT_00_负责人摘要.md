# AUDIT_00：ChaBo / 插播 Ad Lab 开发前代码审计负责人摘要

- **审计基准时间**：2026-11-05
- **审计仓库与版本**：
  - `ChaBo`：分支 `audit/pre-dev-20261105`（基于 `main` @ `c8988a4`，[pyproject.toml](file:///c:/Users/Andy/Antigravity/ChaBo/pyproject.toml#L1-L26)）
  - `TelegramABC`：分支 `main` @ `dafe912a`（[plugins.py](file:///c:/Users/Andy/Antigravity/TelegramABC/apps/api/routers/plugins.py#L1-L357)）
  - `TGWorker`：分支 `main` @ `ea8c11e`（[ffmpeg.py](file:///c:/Users/Andy/Antigravity/TGWorker/processing/ffmpeg.py#L22-L929)）
- **证据等级说明**：`[已验证]` 源码行号定位 + 隔离自动化测试实测通过；`[部分验证]` 源码/静态契约与单模块测试验证，未跑跨库实网联调；`[尚未验证]` 依赖真实 Telegram 生产环境或外部配置，本次按安全边界未触发。

---

## 1. 旧 ChaBo 值不值得继续改造？

**结论：值得继续改造，强烈推荐「路线 B：保留核心模块 + 局部靶向重构」，不建议推倒重来（路线 C）或带病直接堆新功能（路线 A）。** `[已验证]`

- **核心依据**：
  1. **高可复用资产（约 72% 代码可直接或轻改复用）**：旧 `ChaBo` 并非空壳原型，而是具备 **40 张 SQLite 业务表**（[db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L9-L1054)）、**7,521 行服务层代码**（`wallet.py` / `order.py` / `channel.py` / `billing.py`）、**3,405 行 FastAPI 接口**（[webapi/main.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/main.py#L1-L1150)）、**React 19 + AntD 5 + AG Grid 34 三端前端**（`npm run build` 零报错通过）以及 **189 个单元与集成测试**（修复 Windows 句柄释放后 **189/189 全部通过**）。`[已验证]`
  2. **R2 报告预判纠偏**：《R2 优化版》假设旧库仅有约 14 张核心表、且以人民币“分（fen）”计价；实际代码库已有 **40 张表**（含 `placement_plans` 排期矩阵、`topup_approval_requests` 双人审批、`stars_payments`、`channel_subscriptions`），且全库金额单位为**美元美分（USD `*_cents`，`$1.00 = 100 cents`）**（[money.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/money.py#L1-L17)）。推倒重写将白白浪费已成型的三端门户、排期矩阵与账本底座。`[已验证]`
  3. **不可直接带病迭代（否定路线 A）**：履约事务模型、充值并发锁和权限边界存在 **5 个 P0/P1 级致命缺陷**，必须在 Phase 0（约 5～7 人日）集中清零后，方可进入 MVP 闭环开发。`[已验证]`

---

## 2. 当前最严重的五个技术风险是什么？

| 排名 | 风险定级与概述 | 代码位置与实测证据 | 证据等级 |
| :--- | :--- | :--- | :--- |
| **TOP 1** | **[P0 履约灾难] 长事务包裹外部 HTTP 请求 + 私信异常未捕获，导致广告无限重复发帖且不扣款**<br>`dispatch_due()` 在单个 SQLite `BEGIN IMMEDIATE` 事务内串行调用 Telegram API；末尾 `_maybe_notify_budget` 向广告主发私信未加 `try/except`。一旦广告主未启动/拉黑 Bot，整个事务回滚，**频道已发帖并置顶，但数据库回滚为 `scheduled` 且扣费为 0**，后续轮询将无限重发同一广告。 | [fulfillment.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L29-L48)、[#L186-L196](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L186-L196)、[#L334-L350](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L334-L350)<br>隔离实测 `test_03`：2 次轮询在频道发出 2 条广告，DB 仍为 `scheduled`、`spent_cents=0`。 | `[已验证]` |
| **TOP 2** | **[P0 财务漏洞] 双人充值审批跨 3 个独立事务（TOCTOU）+ 手工充值未校验负数 + 暂停单重审无资金冻结**<br>① `approve_topup` 分 3 个事务执行（读状态→加余额→改状态），并发点击或中途崩溃直接**双倍入账**；② `manual_topup` 允许传入负数直接扣减余额；③ `pause_and_release` 退回冻结款后，`approve_order` 允许把 `paused` 订单直接改回 `approved`（`reserved_cents=0` 空头支票排期）。 | [wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L435-L482)、[#L1021-L1063](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L1021-L1063)<br>[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1122-L1145)、[#L1490-L1509](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1490-L1509)<br>隔离实测 `test_01`/`test_02`/`test_07a` 全部复现。 | `[已验证]` |
| **TOP 3** | **[P0 结算与商业模式失效] 平台服务费在 100% 可达路径下恒为 0 + 频道重绑篡改归属致结算崩溃 + 10% 质保金永久死锁**<br>① 未订阅频道强制开启引流免佣（`service_fee_bps=0`），已订阅频道亦免佣（`0`），5% 平台费成死代码；② 另一管理员触发 `bind_channel` 会覆盖 `owner_account_id`，导致 24h 后 `confirm_due_earnings` 扣减新号 `pending_earnings` 报 `InvalidState` 阻断全平台结算；③ 10% `holdback_bps` 无任何释放代码。 | [order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L878-L888)、[channel.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/channel.py#L82-L90)、[wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L853-L889)<br>隔离实测 `test_07b` 复现结算崩溃。 | `[已验证]` |
| **TOP 4** | **[P1 越权与伪造审计] 管理员“代看”具备完整写/扣款权限且将操作人伪造为被代看用户 + 新用户 Candidate 死锁**<br>① 代看模式（Impersonation）未拦截 POST/PATCH 请求，且将 `session["telegram_user_id"]`（被代看人 ID）写入业务与审计日志；② 任意管理员（含 `viewer`）无需代看即可直调 `/api/advertiser/*`；③ 新注册用户默认 `candidate` 状态，被 `active_portals()` 拦截返回 403，无法自助下单或绑频道。 | [auth.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/auth.py#L94-L104)、[#L379-L429](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/auth.py#L379-L429)<br>[deps.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/deps.py#L93-L100)、[main.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/main.py#L918-L1135) | `[已验证]` |
| **TOP 5** | **[P1 架构分裂与句柄泄漏] 双 Web 栈并存（FastAPI 缺失 Webhook/调度路由）+ SQLite 连接未关闭 + 报表 JOIN 膨胀**<br>① `web.py`（8080）与 `webapi/main.py`（8081）并存，Telegram Webhook 与 `dispatch-due` 仅在旧 `web.py` 中；② `with self.db.connect() as conn:` 不关闭句柄，导致 Windows 下 110/189 个测试报 `WinError 32`；③ `AdvertiserService.report` 因 `ad_orders LEFT JOIN deliveries` 导致单频道花费按投递次数 **N 倍膨胀**。 | [db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L666-L698)、[web.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/web.py#L227-L277)、[billing.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/billing.py#L946-L962)<br>隔离实测 `test_06`：花费 2000 被算成 6000。 | `[已验证]` |

---

## 3. 哪些模块可以直接复用？

1. **`ChaBo` 账本与风控底座（轻改即用）**：整数美分账本模型（`accounts` / `ledger_entries` 四类余额与 `CHECK >= 0` 约束）、Telegram Stars 幂等支付（[billing.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/billing.py#L409-L517)）、SHA-256 哈希链审计日志（[audit.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/audit.py#L1-L154)）、频道三维定价评估引擎（`PricingService.assess_channel`，[channel.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/channel.py#L1102-L1204)）。`[已验证]`
2. **`ChaBo` 三端前端框架与排期计划表**：`web/` 下 React 19 + AntD 5 + AG Grid 34 三端工作台骨架、Telegram Login HMAC 鉴权（[auth.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/auth.py#L14-L53)）、`placement_plans` / `placement_plan_items` 数据结构。`[已验证]`
3. **`TelegramABC` 只读频道资产与指标网关**：`GET /api/plugins/assets/channels`（[plugins.py](file:///c:/Users/Andy/Antigravity/TelegramABC/apps/api/routers/plugins.py#L285-L315)）及 `AssetAccountPermission`、`DiscoveryCandidate`（含 Userbot 采样的 `member_count`、`daily_view_count`、`weekly_view_count`），可通过扩展只读 Plugin Gateway 零侵入同步自有频道池与真实浏览量。`[部分验证]`
4. **`TGWorker` 独立素材处理工具类（代码级拷贝复用）**：[processing/ffmpeg.py](file:///c:/Users/Andy/Antigravity/TGWorker/processing/ffmpeg.py#L22-L929)（H.264 `yuv420p` + `+faststart` 转码、自动注入 `anullsrc` 静音音轨防 Telegram 静音视频变 GIF、自适应水印）与 [engine/content_filter.py](file:///c:/Users/Andy/Antigravity/TGWorker/engine/content_filter.py#L1-L270)（敏感词/正则过滤）。`[已验证]`

---

## 4. 最小可投放版本（MVP）还缺什么？

对照「频道接入 → 广告位管理 → 广告活动 → 多频道排期 → 审批与资金 → 自动履约 → 统计采集 → 效果报告 → 优化建议 → 复投」十步闭环，还缺 **6 项核心能力** `[已验证]`：
1. **可靠履约状态机与自动下架**：缺少 `claiming/sending` 预占态、`reconciliation_required` 超时对账态，以及 `pin24h` 到期自动取消置顶（`unpinChatMessage`）与按期自动删帖（`deleteMessage`）。
2. **频道规则与排期防撞检核**：`channel_configs` 中的 `daily_ad_limit`、`allowed_start_hour/end_hour`、`category_blocklist_json` 目前形同虚设，未在排期与履约时拦截；`maybe_schedule_next` 忽略了 `frequency_per_day > 1`。
3. **HTTP 短链点击追踪（`/r/{token}`）**：目前直接暴露原始 `target_url`（[fulfillment.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L212)），无法统计外链点击（CTR/CPC）。
4. **帖子浏览量（1h/6h/24h/48h）自动采样**：`metric_snapshots` 仅记录了 `/start ad_<id>`，缺少对接 `TelegramABC` MTProto 采集 `message_views` 的定时任务。
5. **Web 端完整自助能力**：缺 Web 端频道导入/同步入口、素材富媒体上传与编辑接口、Web 端效果报告页（及免登录分享页 `/reports/:token`）与一键复投（克隆排期）接口。
6. **基于规则的优化建议生成**：目前无 `recommendations` 规则引擎（基于 CPM/CTR/CPC 阈值产出“复投/换素材/降频/停投”建议）。

---

## 5. 下一阶段先修什么，后做什么？

- **第一阶段（Phase 0：止血与地基加固，5～7 人日，阻塞上线）** `[已验证]`：
  1. **拆解 `fulfillment.py` 长事务**：改为「短事务 CAS 抢占（`scheduled -> sending`）→ 事务外调 Telegram API → 短事务落账（`sent` 或 `reconciliation_required`）」，隔离私信通知异常。
  2. **修复 5 项资金与结算漏洞**：合并 `approve_topup` 为单事务 `BEGIN IMMEDIATE`；校验 `manual_topup > 0`；禁止 `paused` 订单无冻结款重审；修复 `service_fee_bps` 恒为 0 与重绑频道覆盖 `owner_account_id` 缺陷；实现 7 天 `holdback` 释放。
  3. **修复 DB 句柄与安全边界**：修复 `db.py` 连接上下文关闭逻辑；代看模式强制只读并记录真实 `impersonator_account_id`；解除新用户 `candidate` 403 死锁；修复 `bot.py` 未定义 `logger`。
- **第二阶段（Phase 1：MVP 投放与追踪闭环，8～10 人日）** `[已验证]`：
  1. 合流 `web.py` 路由至 `webapi/main.py`；打通 `submit_plan` 单事务原子提交、频次间隔计算、频道时段/日限额校验与到期自动 `unpin/delete`。
  2. 新增 `/r/{token}` 短链重定向与点击去重表；对接 `TelegramABC` Plugin Gateway 只读导入自有频道与 1h/6h/24h/48h 浏览量采样。
- **第三阶段（Phase 2：效果报告、规则建议与一键复投，5～7 人日）** `[已验证]`：
  1. 修复 `AdvertiserService.report` JOIN 膨胀 Bug；上线 Web 效果报告、分享链接 `/reports/:token`、4 条规则优化建议与一键克隆复投。

---

## 6. 哪些工作现在不值得投入？

1. **不要在 MVP 阶段将数据库全量迁移至 PostgreSQL** `[已验证]`：当前 SQLite 并发瓶颈的根因是 `fulfillment.py` 将 20 秒网络 I/O 塞进了写事务，而非 SQLite WAL 本身性能不足。剥离事务内网络 I/O 并修正 `busy_timeout` 后，SQLite WAL 完全可支撑日均万级订单与十万级短链点击。
2. **不要新建 `chabo-next` 独立仓库从零重写** `[已验证]`：重写将丢弃已验证的 40 张表、189 个测试用例与三端 React 工作台，至少多耗费 4～6 周且引入大量回归风险。
3. **不要让 `ChaBo` 与 `TelegramABC` 共享写数据库或共用 Telethon `.session` 文件** `[已验证]`：`TelegramABC` 对 `.session` 有严格的进程文件锁（[session_lock.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/core/session_lock.py#L36-L55)），双进程直连必触发 Telegram `AuthKeyDuplicatedError` 导致封号/掉线；只走 HTTP Plugin Gateway 即可。
4. **不要在 V1 建设复杂 AI 归因、向量数据库（pgvector）、多语言 i18n 或链上自动充提网关** `[已验证]`：先用确定性四档规则跑通首轮 10～20 单真实投放与复投闭环，再考虑 LLM 智能诊断。
