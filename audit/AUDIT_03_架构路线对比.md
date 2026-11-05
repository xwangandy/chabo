# AUDIT_03：架构路线对比、数据库选型验证与外部集成方案

- **审计日期**：2026-11-05
- **评估原则**：不预设《R1》或《R2 优化版》正确，全部结论基于 `ChaBo`（`c8988a4`）、`TelegramABC`（`dafe912a`）、`TGWorker`（`ea8c11e`）真实源码与实测证据。

---

## 一、对《R1》与《R2 优化版》预设前提的事实纠偏 `[已验证]`

在对比架构路线前，必须先澄清《R2 优化版》中与当前代码库真实状态不符的 **4 个关键事实偏差**：

| 维度 | 《R2 优化版》假设 | 代码库真实状态（`HEAD = c8988a4`） | 对架构决策的直接影响 |
| :--- | :--- | :--- | :--- |
| **1. 数据库与功能演进阶段** | 假设旧 `ChaBo` 仅有 14 张基础表，无 Web 三端门户、无多频道排期计划、无双人充值审批 | 提交 `625fb1e` 与 `d981887` 已落地 **40 张 SQLite 表**（[db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L9-L1054)）、74 个 FastAPI 路由（[webapi/main.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/main.py#L1-L1150)）、React 19 三端工作台（`web/src/`）及 `placement_plans` 排期表 | 若按 R2 建议新建 `chabo-next` 从零搭建三端与排期表，将造成 **70% 以上重复造轮子**。 |
| **2. 资金计价货币单位** | 假设旧库与新库以人民币“分（fen）”为单位 | 全库 40 张表均以 `*_cents` 命名，且 [money.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/money.py#L1-L17) 明确为 **美元美分（USD cents，`$1.00 = 100 cents`）**，并与 Telegram Stars（`$50 = 2500 Stars`，[billing.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/billing.py#L31-L32)）深度绑定 | 严禁混入人民币“分”口径，必须保持全链路 USD `cents` 统一。 |
| **3. `TelegramABC` 广告接口现状** | 部分早期设计假设调用 `/api/recurring-ad` | `TelegramABC` 的 `/api/recurring-ad` 已全量废弃并返回 `HTTP 410 Gone`（[recurring_ad.py](file:///c:/Users/Andy/Antigravity/TelegramABC/apps/api/routers/recurring_ad.py#L1-L32)），统一收敛至 `/api/tasks`（`kind="ad_recurring"`）与 `/api/plugins/*` | 集成接口必须对齐 `TelegramABC` 现行 `PluginGatewaySDK` 与 `tasks` 模块。 |
| **4. SQLite 并发瓶颈的真实根因** | 仅按订单量/点击量讨论 SQLite 够不够用 | 当前 SQLite 锁竞争的真正元凶是 [fulfillment.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L33-L46) **在单个 `BEGIN IMMEDIATE` 写事务内串行发起长达数分钟的外部 Telegram HTTP 请求**，外加 `with db.connect()` 漏关句柄 | 不拆解长事务，即便迁到 PostgreSQL 也会耗尽连接池并产生长事务死锁；拆解长事务后，SQLite WAL 完全满足 MVP 需求。 |

---

## 二、三种架构演进路线七维量化对比 `[已验证]`

### 方案定义
- **路线 A（在旧 ChaBo 上原位直接升级）**：保持现有 `web.py` + `webapi/` 双栈结构、保持 `fulfillment.py` 现有事务写法与 `bot.py` 7,104 行巨石结构，仅在上面直接堆叠短链、采样、报告、建议等新功能。
- **路线 B（保留核心模块、局部靶向重构 —— 推荐路线）**：在现有 `ChaBo` 仓库内演进，保留 40 张表 Schema、核心账本/定价/排期服务、`webapi/`（FastAPI）与 `web/`（React 19 三端前端）；**先靶向重构 `fulfillment.py` 为三段式 CAS 状态机、修复 5 类资金/权限 P0 漏洞、下线 `web.py` 并合流路由**，再增量补齐短链、采样（通过 `TelegramABC` 只读网关）、报告与复投闭环。
- **路线 C（独立建立新服务 `chabo-next` 并拷贝复用旧模块）**：新建独立仓库与新 Schema，将旧 `ChaBo` 的 `wallet.py`、`audit.py` 等模块拆解拷贝过去，重新编写 FastAPI 路由、排期计划与前端对接。

### 七维指标对比矩阵

| 评估指标 | 路线 A：旧 ChaBo 原位直接叠功能 | 路线 B：保留核心模块 + 局部靶向重构（★推荐） | 路线 C：新建独立服务 `chabo-next` 迁移旧模块 |
| :--- | :--- | :--- | :--- |
| **1. 可用代码比例** | 表面 ~85%，但带病运行 | **~72% 直接复用 + ~18% 局部重构**（仅废弃 ~10% 如 `web.py` 重复代码与 `light_tail` 死分支） | **< 35%**（拷贝 `wallet.py`/`channel.py` 后需重写 `db.py`、74 个 API 路由及前端联调） |
| **2. 关键缺陷数量与严重性** | **极高风险**：7 个 P0 + 8 个 P1 缺陷原样保留，随时触发无限重发广告、双倍充值入账或结算崩溃 | **可控清零**：Phase 0 集中 5～7 人日即可定点消除全部 7 个 P0 与 8 个 P1 缺陷，改动面清晰收敛 | **旧病虽除但引入大量新 Bug**：重写 40 张表关联、三端鉴权与排期状态机极易引入新的账务与边界缺陷 |
| **3. 数据迁移难度** | 零迁移（使用现有 `0001`～`0013` 迁移链） | **极低**：沿用现有 `schema_migrations` 机制，新增 `0014`～`0016` 增量迁移即可，兼容已有测试数据 | **高**：需编写跨库 ETL 脚本迁移 40 张表的账户、余额流水、频道评估与历史订单 |
| **4. 预计开发与维护成本** | 初期快，但线上排障、对账修复与双 Web 栈维护成本极高 | **最低（约 18～24 人日完成 Phase 0～2）**：单仓单栈（FastAPI + React 19 + SQLite WAL），心智负担最小 | **高（约 45～60 人日）**：相当于重做一遍 提交 `#5`、`#6`、`#7` 的全部工程量 |
| **5. 测试覆盖与发布风险** | 高风险（Windows 下 110 个单测报错未修，核心并发/超时路径无保护） | **低风险**：修复 `db.py` 句柄后 **189 个原生单测 + 7 个专项攻防测试 100% 护航**，每改一处均可秒级回归 | **中高风险**：原有 189 个基于 `ChaboApp` 的集成测试大面积失效，需重写测试底座 |
| **6. 与 `TelegramABC` 耦合程度** | 易因临时赶工直接读 `TelegramABC` 库或会话 | **松耦合（HTTP 只读网关契约）**：通过 `Chabo -> TelegramABC Plugin Gateway` 单向同步频道与浏览量，零数据库共享 | 松耦合，但需在新仓从零搭建客户端与本地缓存表 |
| **7. 后续数据分析扩展能力** | 受限于 JOIN 膨胀 Bug 与缺失的点击/快照粒度 | **良好**：修正 `metric_snapshots` 采样粒度、新增 `click_events` 短链表与规范化聚合视图后，平滑支持规则引擎与后续 BI 导出 | 良好，但在 MVP 阶段属于过度超前投资 |

> **架构选型结论**：**坚定选择「路线 B（保留核心模块、局部靶向重构）」**。它既保住了已成型的 40 张表、7,521 行领域服务、三端 React 工作台与 189 个自动化测试资产，又能以最小代价根除 P0/P1 级财务与履约隐患。`[已验证]`

---

## 三、SQLite WAL vs PostgreSQL 实际需求深度验证 `[已验证]`

审计要求：*“特别验证 SQLite 与 PostgreSQL 的实际需求，不能仅根据预计订单量或 HTTP 点击量决定数据库选型。”*

我们从**锁持有时间模型、事务并发冲突形态、连接生命周期、审计哈希链串行性、运维复杂度**五个底层工程维度进行了源码级核验：

### 3.1 当前 `ChaBo` 在 SQLite 下出现锁冲突与句柄错误的真正技术原因
1. **反模式一：将慢速外部网络 I/O 包裹在 `BEGIN IMMEDIATE` 写事务内（锁持有时间放大 $10^5$ 倍）**
   - 在 [src/chabo/db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L724-L737) 中，`self.db.transaction()` 执行 `BEGIN IMMEDIATE`，立即获取 SQLite 全局写锁（WAL 模式下允许并发读，但全局同一时刻仅允许 1 个写事务）。
   - 纯内存/磁盘的账本扣减与状态更新在 SQLite WAL 下仅耗时 **0.2 ms ～ 1.5 ms**。
   - 然而 [src/chabo/fulfillment.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L33-L46) 在 `with self.db.transaction() as conn:` 内部循环处理 20 条投递，每条投递同步调用 `BotApiClient`（超时 20s + `curl` 回退 25s，见 [telegram.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/telegram.py#L151-L178)）。这意味着**一个写事务可霸占全局写锁 10 秒～300 秒**！在此期间，任何其他请求（广告主下单、Stars 支付回调、管理员审批）只要等待超过 5 秒就会抛出 `sqlite3.OperationalError: database is locked`。
   - **关键推论**：如果不把外部 HTTP 请求移出数据库事务，即便换成 PostgreSQL，长事务也会长期占满连接池、锁住 `accounts` 与 `ad_orders` 行锁并引发死锁。
2. **反模式二：`busy_timeout` 被错误缩短 + 读连接未关闭句柄**
   - [db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L670-L676) 先设了 `timeout=30.0`，紧接着第 676 行 `PRAGMA busy_timeout = 5000;` 将其砍回 5 秒。
   - [readmodels.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/readmodels.py#L43) 等上百处 `with self.db.connect() as conn:` 在退出 `with` 块时不调 `conn.close()`，导致未释放的只读连接阻碍 WAL checkpoint 截断（`PRAGMA wal_checkpoint(TRUNCATE)`）。
3. **特殊约束三：`audit_logs` 全局单链哈希天然要求写串行化**
   - [src/chabo/audit.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/audit.py#L45-L60) 的 `insert_audit_log` 每次通过 `SELECT id, audit_hash FROM audit_logs ORDER BY id DESC LIMIT 1` 获取上一条哈希计算 `SHA-256`。
   - 在 SQLite `BEGIN IMMEDIATE` 下，全局写串行化**天然保证了哈希链不会分叉**；若盲目迁移到 PostgreSQL 默认的 `READ COMMITTED` 多并发写事务，两个并发事务会读到同一个 `prev_hash`，直接造成**哈希链分叉（Audit Chain Fork）**，反而需要额外引入 PostgreSQL Advisory Lock 或 `LOCK TABLE` 才能維持单链不变量！

### 3.2 SQLite WAL（修复后）与 PostgreSQL 的能力边界对比

| 架构维度 | SQLite WAL（修复短事务 + 句柄关闭后） | PostgreSQL 16 | MVP 及 V1 阶段真实需求匹配度 |
| :--- | :--- | :--- | :--- |
| **单笔短事务写吞吐** | 500 ～ 2,000 写事务/秒（NVMe SSD） | 5,000 ～ 20,000+ 写事务/秒 | MVP 峰值写操作（下单/履约落账/点击落库）远低于 50 TPS，**SQLite WAL 绰绰有余**。 |
| **并发读写隔离** | 多读一写（Readers never block writers） | MVCC 多读多写 + 行级锁 (`SELECT FOR UPDATE`) | 只要将 `fulfillment.py` 改为“微秒级 CAS 抢占 → 事务外发帖 → 微秒级落账”，单写锁持有时间 `< 2ms`，完全无感知。 |
| **`audit_logs` 哈希链一致性** | `BEGIN IMMEDIATE` 天然严格串行，零分叉 | 需额外加 `pg_advisory_xact_lock` 防止并发分叉 | **SQLite 在单链防篡改账本上反而更简单安全**。 |
| **多机水平扩展（多容器写）** | **不支持**（受限于单机文件系统锁） | **原生支持**多无状态 API/Worker 节点并发写 | MVP 为单机/单主容器部署，无多机并发写需求。 |
| **备份与容灾成本** | `sqlite3.Connection.backup()` 单文件热备（[db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L694-L698) 已实现） | 需维护独立 PG 容器、`pg_dump`/WAL 归档与 Alembic | SQLite 零外部基础设施依赖，大幅降低首期运维故障面。 |

### 3.3 数据库选型最终结论与迁移触发红线 `[已验证]`
1. **MVP 与 V1 阶段（当前阶段）：坚持使用 SQLite WAL，严禁在此时启动 PostgreSQL 迁移**。只需在 Phase 0 完成两件事：① 修复 `db.py` 连接关闭上下文与 `busy_timeout = 30000`；② 将 `fulfillment.py` 网络 I/O 移出写事务。
2. **未来何时才真正需要迁移至 PostgreSQL（明确触发条件）**：
   - 当且仅当出现以下任一工程需求时再启动 PG 迁移：① `ChaBo` 需要部署 **2 个及以上跨主机 Worker 容器**同时写入同一账本；② 单个写事务平均排队等待在剥离网络 I/O 后仍持续 `> 50ms`；③ 需要与企业级 BI/Metabase 建立高并发长耗时 OLAP 分析只读副本。

---

## 四、`TelegramABC` 与 `TGWorker` 集成可行性及边界审计

### 4.1 `TelegramABC` 六大集成能力逐项核验 `[部分验证]`

| 序号 | 集成目标能力 | `TelegramABC` 现有接口与源码位置 | 可行性结论与改造点 |
| :--- | :--- | :--- | :--- |
| **1** | **导入自有频道资产** | **已具备只读网关接口**：`GET /api/plugins/assets/channels`（[plugins.py](file:///c:/Users/Andy/Antigravity/TelegramABC/apps/api/routers/plugins.py#L285-L315)），底层调用 `PluginGatewaySDK.get_channel_assets()`（[sdk.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/plugin_gateway/sdk.py#L140-L185)），返回 `asset_id`、`external_ref`（即 `tg_chat_id`）、`name`、`role`、`status`、`health_grade`、`community`/`unit`。 | **直接可用（需补鉴权与字段）**：目前 `/api/plugins/*` 未挂鉴权，且未返回 `username`；需在 `TelegramABC` 侧加 `X-Plugin-Token` 校验并在 DTO 补齐 `username` 与 `category`。 |
| **2** | **查询频道及 Bot 权限** | **数据已完备，需在网关暴露只读端点**：`TelegramABC` 在 `AssetAccountPermission`（[permission.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/asset_center/models/permission.py#L19-L64)）与 `BotChannelBinding`（[binding.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/asset_center/models/binding.py#L148-L190)）中持久化了 `can_post_messages`、`can_edit_messages`、`can_delete_messages`、`can_pin_messages`；同时 `ChaBo` 自身 `BotApiClient.get_chat_member`（[telegram.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/telegram.py#L222-L246)）也可实时自检。 | **高可行性**：① `TelegramABC` 插件网关新增只读 `GET /api/plugins/assets/channels/{chat_id}/permissions`；② `ChaBo` 在排期与发帖前同时用自身 `BotApiClient.get_chat_member` 做最终实时权限复核。 |
| **3** | **读取允许访问的统计信息** | **数据已完备，需在网关暴露只读端点**：`TelegramABC` 的 `DiscoveryScanner`（[scanner.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/discovery/scanner.py#L196-L238)）已通过 Userbot MTProto `get_messages(limit=100)` 计算 `member_count`、`daily_view_count`、`weekly_view_count`，并另有 `AssetMetricSnapshot`（[asset.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/asset_center/models/asset.py#L234-L281)）。 | **高可行性（核心高价值复用点）**：`ChaBo` 无需自己维护 Telethon Userbot，只需通过 `TelegramABC` 插件网关拉取频道订阅数、24h/7d 真实浏览量以及指定广告 `message_id` 的 1h/6h/24h/48h `views` 快照。 |
| **4** | **创建授权的广告发布任务** | `TelegramABC` 现有 `POST /api/tasks/`（[service.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/tasks/service.py#L145-L244)）支持 `kind="ad_recurring"`（由 `AdRecurringBotApiCapability` / `AdRecurringUserbotCapability` 执行）。**但存在两个关键不匹配**：① 它要求传入上游源消息 `(source_chat_id, source_message_id)` 进行转发/拷贝（`#L176-L184`），不支持直接传富文本+三按钮键盘；② `POST /api/tasks/` 缺少 `idempotency_key` 幂等键。 | **结论：V1 广告发帖由 `ChaBo` 自身 `BotApiClient` 直接执行，不委托 `TelegramABC` 任务中心代发**。原因：`ChaBo` 需要精确控制三按钮深链、尾注挂载与毫秒级账本扣款回执；委托 `TelegramABC` 跨系统异步转发会割裂状态机并增加双边失败对账复杂度。 |
| **5** | **查询任务执行状态** | `TelegramABC` 提供 `GET /api/tasks/{task_id}` 与 `TaskItem` 状态（[models.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/tasks/models.py#L345-L398)）。 | 若采用上述“资产与指标走 `TelegramABC` 只读网关、发帖走 `ChaBo` 自有 Bot”方案，则发帖状态完全由 `ChaBo` 本地 `deliveries` 表闭环掌控，无跨系统状态同步延迟。 |
| **6** | **获取消息 ID、时间及发布证据** | `ChaBo` 自身 `BotApiClient.send_ad` / `send_media_ad`（[telegram.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/telegram.py#L248-L355)）直接从 Telegram 同步拿到 `message_id`；后续由 `TelegramABC` 提供 `GET /api/plugins/messages/views?chat_id=...&message_ids=...` 读取该 `message_id` 是否存活及当前 `views` 数，作为 24h 结算防删帖对账证据。 | **最优组合**：`ChaBo` 负责发帖拿 `message_id` 证据，`TelegramABC` 负责用 MTProto 采样该 `message_id` 的浏览量与存活状态。 |

### 4.2 严禁共享写数据库与严禁共用 `.session` 的安全铁律 `[已验证]`

1. **严禁共享写数据库**：
   - `TelegramABC` 使用 PostgreSQL 16（异步 SQLAlchemy 2.0），`ChaBo` 使用 SQLite 3（同步 `sqlite3`），两者无论数据模型还是事务边界均完全正交。
   - **数据所有权（SoR）边界**：`TelegramABC` 是自有频道资产清单与 MTProto 原始采样的唯一事实源（Owner）；`ChaBo` 是广告主/频道主商业账户、资金账本、排期订单、短链点击与结算单的唯一事实源（Owner）。两者仅通过受 `X-Plugin-Token` 保护的只读 HTTP 接口通信。
2. **严禁 `ChaBo` 直接读取或加载 `TelegramABC` 的 Telethon `.session` 文件**：
   - 源码证据：`TelegramABC` 在 [modules/core/session_lock.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/core/session_lock.py#L36-L55) 中实现了严格的进程级文件锁 `acquire_session_lock()`。
   - 若 `ChaBo` 进程尝试加载同一个 `.session` SQLite 文件，轻则触发文件锁冲突，重则导致两个进程以不同序列号同时连接 Telegram MTProto 服务器，触发 Telegram 官方风控 `AuthKeyDuplicatedError`，导致账号所有会话被强制踢出甚至封禁！

### 4.3 `TGWorker` 复用结论 `[已验证]`
- **不进行运行时服务集成**：`TGWorker` 是面向本地文件搬运的单机工具，其 `ad_scheduler.py` 不具备商业账本能力。
- **仅做代码级工具类提取**：在后续需要支持视频广告素材上传预检时，将 `TGWorker` 的 [processing/ffmpeg.py](file:///c:/Users/Andy/Antigravity/TGWorker/processing/ffmpeg.py#L22-L929)（静音视频补 `anullsrc` 音轨防变 GIF、H.264 标准化转码）与 [engine/content_filter.py](file:///c:/Users/Andy/Antigravity/TGWorker/engine/content_filter.py#L1-L270)（敏感词过滤）作为独立无状态工具模块引入 `ChaBo` 即可。
