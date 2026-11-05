# AUDIT_01：系统现状与测试结果

- **审计日期**：2026-11-05
- **执行环境**：Windows 11 / PowerShell（隔离审计分支 `audit/pre-dev-20261105`，零外部副作用）
- **证据等级定义**：
  - `[已验证]`：通过源码逐行审查 + 本地隔离自动化测试或构建命令实测证实
  - `[部分验证]`：通过源码审查 + 静态契约/单测验证，未启动跨系统容器实网联调
  - `[尚未验证]`：受限于安全边界（禁止连真实 Telegram 发帖/删帖、禁止读真实 `.session`/生产库）未执行

---

## 一、三仓库 Git 状态与版本基线 `[已验证]`

| 仓库名称 | 本地路径 | 远端地址 | 当前审计分支 | 基准 Commit (`HEAD`) | 最新提交说明 | 远端活跃分支 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **ChaBo** | `c:\Users\Andy\Antigravity\ChaBo` | `https://github.com/xwangandy/chabo.git` | `audit/pre-dev-20261105`（从 `main` 切出） | `c8988a4` | `feat(bot): retire text insertion ad format (#8)` | `main`、`claude/intelligent-yonath-7ba06a`、`claude/sharp-northcutt-83ab82`、`claude/youthful-archimedes-18010b` |
| **TelegramABC** | `c:\Users\Andy\Antigravity\TelegramABC` | 本地主仓 | `main` | `dafe912a` | `refactor(architecture): align constitution, enforce router unplug topology, purge dead AI UI, and split asset router` | `main` |
| **TGWorker** | `c:\Users\Andy\Antigravity\TGWorker` | 本地主仓 | `main` | `ea8c11e` | `feat(worker): rebrand web UI to TGWorker, disable auto-open browser, and harden heartbeat session check` | `main` |

### `ChaBo` 近期演进轨迹（`git log -n 10`）`[已验证]`
1. `c8988a4` — `feat(bot): retire text insertion ad format (#8)`（下线正文尾插 `light_tail` 广告形态，新增迁移 `0013`）
2. `37a5b32` — `feat(bot): redesign bot menus and onboarding flows (#7)`
3. `625fb1e` — `feat(web): build three-portal web workspace and placement plans (#6)`（引入 `web/` React 三端门户、`src/chabo/webapi/` FastAPI 与排期计划 `placement_plans`）
4. `d981887` — `feat(billing): add channel subscriptions, stars payment, and dual-control topup (#5)`
5. `1cb70a3` — `feat(growth): add viral tail injection, referral tracking, and probe tasks (#4)`

---

## 二、目录结构与代码规模盘点 `[已验证]`

### 2.1 `ChaBo` 目录结构与模块规模

`ChaBo` 后端 Python 源码共 **21,158 行**（`src/chabo/`），测试代码 **4,974 行**（`tests/`），前端 TypeScript/React 源码共 **35 个组件/模块文件**（`web/src/`）：

```text
c:\Users\Andy\Antigravity\ChaBo\
├── pyproject.toml                 # Python 包定义与依赖配置 (>=3.11)
├── Dockerfile                     # 容器镜像构建定义（存在漏装 .[web] 缺陷）
├── src/chabo/
│   ├── app.py                     # (100行) ChaboApp 依赖装配容器
│   ├── config.py                  # (117行) 环境变量与 Settings 配置加载
│   ├── db.py                      # (1,054行) SQLite 40张表 DDL、13个迁移与事务封装
│   ├── money.py                   # (17行) 美元美分 (cents) 格式化与解析
│   ├── audit.py                   # (154行) SHA-256 哈希链防篡改审计日志
│   ├── fulfillment.py             # (351行) 到期广告投递调度、发帖、置顶、扣款与通知
│   ├── telegram.py                # (461行) Telegram Bot API 客户端 (urllib + curl 回退)
│   ├── bot.py                     # (7,104行) Telegram Bot 巨石交互状态机与 Webhook 分发
│   ├── cli.py                     # (1,626行) 45+ 子命令 CLI 运维与管理工具
│   ├── web.py                     # (935行) 第一代基于 http.server 的旧 Web 后台 + Webhook 入口
│   ├── services/                  # (7,521行) 核心领域服务层
│   │   ├── wallet.py              # (1,197行) 账户、四态余额账本、双人充值审批、收益确认
│   │   ├── order.py               # (2,163行) 广告素材、订单状态机、预算预占/释放、频次排期
│   │   ├── channel.py             # (1,599行) 频道绑定、刊例档位、三维质量评估与动态定价
│   │   ├── billing.py             # (1,069行) 频道订阅、Stars 支付、广告主/频道主统计报表
│   │   ├── growth.py              # (778行) 顺风车尾注、探针任务、归因会话与增长面板
│   │   └── admin.py               # (632行) 平台级 KPI、异常队列、财务健康度与对账
│   └── webapi/                    # (3,405行) 第二代 FastAPI 三端 Web API
│       ├── main.py                # (1,150行) 74个 HTTP 路由端点 (Admin / Advertiser / Publisher)
│       ├── readmodels.py          # (1,089行) 三端聚合只读查询模型 (存在与 web.py 重复代码)
│       ├── auth.py                # (466行) Telegram Login HMAC 校验、Session、代看 (Impersonation)
│       ├── plans.py               # (348行) 多频道排期计划 (placement_plans) CRUD 与批量提交
│       ├── schemas.py             # (244行) Pydantic 请求模型
│       └── deps.py                # (108行) FastAPI 依赖注入与门户权限守卫
├── web/                           # React 19 + Vite 7 + AntD 5 + AG Grid 34 三端前端
│   └── src/
│       ├── app/router.tsx         # /login, /admin, /advertiser, /publisher 路由定义
│       ├── pages/admin/           # 管理员端 10 个子面板 (Overview/Channels/Orders/Topups/Audit 等)
│       ├── pages/advertiser/      # 广告主端 5 个子面板 (Overview/Catalog/Plans/Materials/Orders)
│       └── pages/publisher/       # 频道主端 4 个子面板 (Overview/Channels/Calendar/Settlement)
└── tests/                         # 16 个测试文件，共 189 个 unittest 测试用例
```

### 2.2 `TelegramABC` 目录结构概要 `[已验证]`

遵循严格的 4 层单向 DAG 架构（`L0 Core` → `L1 Asset/Account` → `L2 Capabilities` → `L3 Orchestration/Routers`），已通过 [check_topology.py](file:///c:/Users/Andy/Antigravity/TelegramABC/scripts/check_topology.py#L1-L120) 架构拓扑强校验：
- `apps/api/routers/`：FastAPI 路由层，包含 [plugins.py](file:///c:/Users/Andy/Antigravity/TelegramABC/apps/api/routers/plugins.py#L244-L357)（对外 Plugin Gateway）、`asset.py`、`tasks.py`、`business_center.py`、以及已废弃返回 `410 Gone` 的 [recurring_ad.py](file:///c:/Users/Andy/Antigravity/TelegramABC/apps/api/routers/recurring_ad.py#L1-L32)。
- `modules/asset_center/`：管理频道资产（`Asset`）、账号权限矩阵（`AssetAccountPermission`）、Bot 绑定（`BotChannelBinding`）与指标快照（`AssetMetricSnapshot`）。
- `modules/discovery/`：通过 Userbot MTProto `get_messages` 采集频道近 7 天贴文浏览量并计算 `daily_view_count` / `weekly_view_count`（[scanner.py](file:///c:/Users/Andy/Antigravity/TelegramABC/modules/discovery/scanner.py#L196-L238)）。
- `modules/tasks/` & `modules/task_center/`：统一任务调度中心（支持 `ad_recurring` 周期性广告任务及 `MessageIdMapping` 消息 ID 映射回执）。

### 2.3 `TGWorker` 目录结构概要 `[已验证]`

定位为单机/边缘侧媒体搬运与处理工具（Hydrogram + FFmpeg + SQLite）：
- [processing/ffmpeg.py](file:///c:/Users/Andy/Antigravity/TGWorker/processing/ffmpeg.py#L22-L929)：`FFmpegEngine`，支持 NVENC/QSV/AMF 硬件加速、H.264 `yuv420p` + `+faststart` 标准化转码、静音视频自动注入 `anullsrc` 音轨（防止被 Telegram 客户端识别为无声 GIF 循环播放）、自适应比例图片/文字水印。
- [engine/text_processing.py](file:///c:/Users/Andy/Antigravity/TGWorker/engine/text_processing.py#L1-L676) & [engine/content_filter.py](file:///c:/Users/Andy/Antigravity/TGWorker/engine/content_filter.py#L1-L270)：文案清洗、正则替换、敏感词黑名单拦截。
- [engine/ad_scheduler.py](file:///c:/Users/Andy/Antigravity/TGWorker/engine/ad_scheduler.py#L1-L312)：本地轻量级定时发送器（功能远弱于 `ChaBo`，无需集成）。

---

## 三、运行环境、依赖与构建配置审计 `[已验证]`

### 3.1 本地审计环境实测版本
- **操作系统**：Windows (`win32`)
- **Python**：`Python 3.11.9`（全局已安装 `fastapi`、`httpx`、`uvicorn`、`pytest`）
- **Node.js / npm**：`Node v25.9.0` / `npm 11.12.1`

### 3.2 依赖配置与打包缺陷
1. **`ChaBo` 后端依赖（[pyproject.toml](file:///c:/Users/Andy/Antigravity/ChaBo/pyproject.toml#L9-L17)）**：
   - 核心依赖为空（`dependencies = []`），仅靠 Python 标准库（`sqlite3`、`urllib.request`、`http.server`、`hmac`、`hashlib`）即可运行 Bot 与 CLI。
   - Web API 依赖放在可选组 `[project.optional-dependencies] web = ["fastapi>=0.115", "httpx>=0.27", "uvicorn[standard]>=0.30"]`。
2. **`Dockerfile` 漏装 `[web]` 依赖缺陷（[Dockerfile](file:///c:/Users/Andy/Antigravity/ChaBo/Dockerfile#L21)）** `[已验证]`：
   - 第 21 行执行 `RUN pip install --user --no-cache-dir .`，未指定 `.[web]`，导致构建出的生产容器镜像内缺少 `fastapi` 和 `uvicorn`，无法启动 `chabo run-api`（三端 Web API 服务）。
3. **前端依赖安全审计（[web/package.json](file:///c:/Users/Andy/Antigravity/ChaBo/web/package.json#L13-L31)）** `[已验证]`：
   - 锁定 React `19.2.0`、Ant Design `5.27.6`、AG Grid `34.3.1`、React Router `7.9.4`、TanStack Query `5.90.5`。
   - 执行 `npm ci --ignore-scripts && npm run build` 耗时 `5.15s` **零报错通过**（产出 `web/dist/`）。
   - `npm audit` 检出 **10 个已知依赖漏洞**（2 Low, 1 Moderate, 7 High），主要集中在构建链与间接依赖，需在上线前执行依赖升级。
4. **高危默认配置缺陷（[config.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/config.py#L40)、[#L64](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/config.py#L64)）** `[已验证]`：
   - `CHABO_BOT_AUTO_APPROVE_ORDERS` 在 `Settings` 中默认值为 `True`（`"1"`），且未写入 [.env.example](file:///c:/Users/Andy/Antigravity/ChaBo/.env.example#L1-L35)，[cli.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/cli.py#L187-L341) 的 `chabo preflight` 亦未对其做生产告警。这意味着默认部署下，广告主在 Bot 提交订单会**跳过人工审核直接进入 `approved` 自动发帖状态**。

---

## 四、数据库架构深度审计 `[已验证]`

### 4.1 真实表规模与《R2 报告》的重要差异
- **表数量**：`ChaBo` 并非 R2 报告所称的“14 张核心表”，而是拥有 **40 张业务与系统表**（定义在 [src/chabo/db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L9-L1054)，含基础 `SCHEMA` 31 张表 + 迁移 `0009` 新增 8 张表 + `schema_migrations` 1 张表）。
- **计价货币**：全库所有金额字段均以 `*_cents` 命名，且 [src/chabo/money.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/money.py#L1-L17) 明确定义为 **美元美分（USD cents，`$1.00 = 100 cents`）**，而非 R2 报告假设的人民币“分（fen）”。

### 4.2 40 张数据库表全景分类清单（[src/chabo/db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L9-L1054)）

| 领域分组 | 表名（共 40 张） | 关键设计特征与审计发现 |
| :--- | :--- | :--- |
| **1. 账户与账本 (5)** | `accounts` (`#L12`)、`ledger_entries` (`#L95`)、`payout_batches` (`#L392`)、`settlement_policies` (`#L402`)、`topup_approval_requests` (`#L604`/`#L982`) | `accounts` 含 6 个非负 `CHECK` 余额字段（`available`/`reserved`/`spent` + `pending`/`confirmed`/`releasable`）；`payout_batches` 仅有空表无业务实现。 |
| **2. 频道与定价 (8)** | `channels` (`#L40`)、`channel_configs` (`#L50`)、`rate_cards` (`#L64`)、`channel_metrics` (`#L75`)、`channel_assessments` (`#L244`)、`channel_format_policies` (`#L262`)、`channel_schedule_blocks` (`#L529`/`#L934`)、`channel_subscriptions` (`#L557`/`#L948`) | 支持三维评分（S/A/B/C/D）、三种广告格式（`pin24h`/`push`/`button_tail`，`light_tail` 已由迁移 `0013` 禁用）与 $50/月免佣订阅。 |
| **3. 广告与排期 (9)** | `campaigns` (`#L112`)、`creatives` (`#L121`)、`ad_materials` (`#L279`)、`ad_orders` (`#L133`)、`deliveries` (`#L166`)、`placement_plans` (`#L502`/`#L890`)、`placement_plan_items` (`#L514`/`#L902`)、`placement_plan_orders` (`#L922`)、`order_reviews` (`#L156`) | 已具备多频道排期计划表（`placement_plans` + `items` + `orders`），但 `submit_plan` 为每条明细重复创建独立 `campaigns`。 |
| **4. 支付与转化 (5)** | `stars_payments` (`#L576`/`#L967`)、`metric_snapshots` (`#L363`)、`disputes` (`#L372`)、`advertiser_sessions` (`#L317`)、`sponsor_orders` (`#L473`) | `stars_payments` 通过 `telegram_payment_charge_id UNIQUE` 保证幂等；`metric_snapshots` 仅写入了 `bot_start`，无浏览量采样。 |
| **5. 增长与探针 (5)** | `organic_posts` (`#L298`)、`light_probe_tasks` (`#L334`)、`light_probe_events` (`#L351`)、`referral_rewards` (`#L454`)、`growth_settings` (`#L467`) | 顺风车原生帖尾注注入与 10U 探针任务模型完整。 |
| **6. 门户鉴权与审计 (8)** | `web_sessions` (`#L997`)、`admin_users` (`#L219`)、`advertiser_users` (`#L543`)、`publisher_users` (`#L550`)、`audit_logs` (`#L204`)、`tool_call_logs` (`#L192`)、`bot_user_states` (`#L228`)、`schema_migrations` (`#L661`) | `audit_logs` 支持 `prev_hash`/`audit_hash` 链，但核心资金操作均未调用 `insert_audit_log`。 |

### 4.3 数据库层 3 项结构性缺陷 `[已验证]`
1. **Windows 文件句柄泄漏（[db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L666-L698)）**：
   - `Database.transaction()` (`#L726-L737`) 在 `finally:` 中显式调用了 `conn.close()`；但 `Database.init()` (`#L680`)、`Database.backup_to()` (`#L696`)、[web.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/web.py#L545) 与 [webapi/readmodels.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/readmodels.py#L43) 大量使用 `with self.db.connect() as conn:`。Python 标准库 `sqlite3.Connection.__exit__` **只提交/回滚事务，不关闭连接句柄**，导致 Windows 下临时库无法清理（110/189 个测试因此报错）及长跑进程句柄累积。
2. **`busy_timeout` 被自我覆盖（[db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L670-L676)）**：
   - 第 670 行设置 `sqlite3.connect(..., timeout=30.0)`（30 秒），第 676 行紧接着执行 `PRAGMA busy_timeout = 5000;` 将锁等待超时缩减为 **5 秒**。
3. **基础 `SCHEMA` 与迁移 `0009` 重复建表（[db.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L502-L650) vs [#L888-L1008](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/db.py#L888-L1008)）**：
   - `placement_plans`、`channel_subscriptions`、`stars_payments`、`topup_approval_requests` 等 7 张表既写在基础 `SCHEMA` 中，又在 `_migration_0009_web_portals_and_plans` 中重复定义，存在 Schema 演进漂移隐患。

---

## 五、自动化测试执行结果汇总 `[已验证]`

### 5.1 各仓库原有测试套件与构建实测

| 被测仓库与项目 | 执行命令 | 实测结果 | 详细说明与根因定位 |
| :--- | :--- | :--- | :--- |
| **ChaBo 语法编译检查** | `python -m compileall -q src tests` | **PASSED** (`exit 0`) | 全部 Python 源码与测试文件语法合法。 |
| **ChaBo 原生单元测试（开箱直跑）** | `python -m unittest discover -s tests -v` | **FAILED** (`Ran 189 tests, errors=110`) | 110 个错误 **100% 发生在 `tearDown` 的 `TemporaryDirectory.cleanup()`**，报 `PermissionError: [WinError 32]`，根因即 `with db.connect() as conn:` 未关闭 SQLite 文件句柄。业务逻辑断言本身 0 失败。 |
| **ChaBo 原生单元测试（注入句柄关闭包装）** | 内存注入 `ClosingConn` 在 `__exit__` 关闭连接后运行 `unittest` | **PASSED** (`Ran 189 tests in 25.572s — OK`) | **189/189 全部通过（0 failures, 0 errors）**，涵盖 `test_invariants.py`、`test_web_portals.py`、`test_v4_billing.py`、`test_bot_flows.py` 等全部 16 个测试模块。 |
| **ChaBo 前端 TypeScript 编译与构建** | `cd web && npm ci --ignore-scripts && npm run build` | **PASSED** (`built in 5.15s`) | `tsc -b && vite build` 零错误通过，产出 `dist/index.html` 与 `dist/assets/`。 |
| **TelegramABC 架构拓扑守卫** | `python scripts/check_topology.py --enforce-all` | **PASSED** (`exit 0`) | `0 upward, 0 cross-leaf, 0 forbidden`，4 层 DAG 边界无违规。 |
| **TelegramABC 插件网关测试** | `pytest tests/plugin_gateway/ -q` | **PASSED** (`4 passed`) | 验证 Plugin Gateway SDK 挂载与频道列表只读导出契约通过。 |
| **TGWorker 核心处理单元测试** | `pytest tests/test_caption_template.py tests/test_path_resolver.py tests/test_cloud_adapter.py tests/test_dedup.py tests/test_sqlite_store.py -q` | **PASSED** (`100 passed in 1.61s`) | 文案模板、路径解析、去重与 SQLite 存储 100 个单测全部通过。 |

---

### 5.2 专项审计自动化验证套件（7 项核心业务与不变量攻防测试）`[已验证]`

为验证原有测试未能覆盖的并发竞态、异常回滚、状态机漏洞与报表失真，我们在隔离目录编写并执行了专项验证套件（`scratch/audit_verification_tests.py`，使用临时库与 Mock Gateway，零外部网络调用）：

```text
Ran 7 tests in 1.017s — OK (全部成功复现并证实以下 9 个高危缺陷证据)
```

| 测试编号与名称 | 验证目标 | 隔离实测输出证据（Stdout 原始记录） | 审计结论 |
| :--- | :--- | :--- | :--- |
| **`test_01`**<br>双人充值审批自审拦截与并发 TOCTOU 竞态 | 验证 `TopupApprovalService.approve_topup`（[wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L1021-L1063)）在双线程并发审批同一笔 $50 充值申请时的资金守恒 | `[AUDIT PROOF 1] Concurrent approve_topup ok_count=2, balance=10000 (expected 5000, actual 10000)` | **证实 P0 漏洞**：自审（`requester == approver`）虽被拦截，但因读状态、入账、改状态分属 3 个独立事务，两个管理员并发点击导致同一笔 5,000 cents 申请**双倍入账为 10,000 cents**。 |
| **`test_02`**<br>`manual_topup` 负数金额注入测试 | 验证 `LedgerService.manual_topup`（[wallet.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/wallet.py#L435-L482)）是否校验 `amount_cents > 0` | `[AUDIT PROOF 2] manual_topup(-3000) succeeded! Balance dropped from 5000 to 2000` | **证实 P0 漏洞**：传入 `-3000` 直接绕过校验，将可用余额从 5,000 静默扣减至 2,000。 |
| **`test_03`**<br>履约预算通知私信失败回滚测试 | 验证 `FulfillmentService.dispatch_due`（[fulfillment.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L29-L48)、[#L334-L350](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L334-L350)）在频道发帖成功后、给广告主发预算提醒私信失败（`Forbidden: bot was blocked`）时的幂等性 | `[AUDIT PROOF 3] Channel ads sent to Telegram=2, DB delivery.status=scheduled, DB order.spent_cents=0` | **证实 P0 灾难级漏洞**：`_maybe_notify_budget` 未捕获 `TelegramError`，导致整个外层 SQLite 事务回滚！2 次调度已在频道发出 **2 条真实广告并置顶**，但 DB 状态仍为 `scheduled` 且扣费为 `0`，造成无限重复发帖。 |
| **`test_04`**<br>`pin24h` 发帖成功但置顶失败测试 | 验证 `pin24h` 订单在 `send_ad` 成功后 `pin_message` 报错（[fulfillment.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/fulfillment.py#L140-L146)）时的账务与证据状态 | `[AUDIT PROOF 4] Pin failure: channel posts sent=1, DB delivery.status=failed, DB message_id=None, advertiser available_balance=5000 (full 5000 refunded!)` | **证实 P0 漏洞**：频道内广告已发出，但因置顶失败走入 `_mark_delivery_failed` 并调用 `pause_and_release` 全额退款，且丢失 `message_id` 无法撤回帖子。 |
| **`test_05`**<br>排期计划非原子提交与多频次排期失灵 | 验证 `submit_plan`（[plans.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/webapi/plans.py#L172-L267)）第 2 项失败时的事务原子性，以及 `maybe_schedule_next`（[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1690)）对 `frequency_per_day=2` 的计算 | `[AUDIT PROOF 5a] After submit_plan failure on item #2: plan.status=draft, orphan ad_orders created=1, placement_plan_orders linked=0`<br>`[AUDIT PROOF 5b] Order frequency_per_day=2, start_at=2026-11-05T09:26:10+00:00, next scheduled_at=2026-11-06T09:26:10+00:00 (+1 day instead of +12h!)` | **证实 2 项 P1 漏洞**：① `submit_plan` 中途失败会在数据库残留已扣款的孤儿订单且未关联到计划；② `frequency_per_day=2` 收取了双倍预算，但下次排期固定 `+1 day` 而非 `+12h`。 |
| **`test_06`**<br>广告主报表 SQL JOIN 笛卡尔膨胀测试 | 验证 `AdvertiserService.report`（[billing.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/billing.py#L946-L962)）在 1 笔订单含 3 条 `deliveries` 时的按频道统计准确性 | `[AUDIT PROOF 6] Actual order spent_cents=2000 (1 order, 3 deliveries total: 2 sent + 1 scheduled), but report.by_channel[0] says orders_count=3, spent_cents=6000 (3x inflated!)` | **证实 P1 报表失真漏洞**：`FROM ad_orders o LEFT JOIN deliveries d` 直接对 `o.spent_cents` 求 `SUM`，导致含 N 条投递记录的订单在频道报表中花费被 **放大 N 倍**（2,000 变 6,000）。 |
| **`test_07`**<br>暂停单无款重审与频道重绑劫持结算测试 | 验证：① `pause_and_release` 后再调 `approve_order`（[order.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/order.py#L1122-L1145)）；② 频道被另一管理员重新 `bind_channel`（[channel.py](file:///c:/Users/Andy/Antigravity/ChaBo/src/chabo/services/channel.py#L82-L90)）后执行 `confirm_due_earnings` | `[AUDIT PROOF 7a] Re-approved paused order status=approved, reserved_cents=0 (unfunded approved order!)`<br>`[AUDIT PROOF 7b] Channel re-bind hijacked owner_account_id and crashed confirm_due_earnings: 频道主待确认收益不足` | **证实 2 项 P0 漏洞**：① 已退回冻结款的 `paused` 订单被直接批准为 `approved`（`reserved_cents=0` 无资金裸跑）；② 频道重绑覆盖 `owner_account_id` 后，历史已发帖订单在 24h 确认收益时因新号无 `pending_earnings` 抛出异常，导致整批结算事务崩溃。 |

---

### 5.3 未执行测试项与边界说明 `[尚未验证]`

严格遵循审计安全边界（第 1～6 条），以下涉及外部真实副作用的测试本次**未执行，且未标记为通过**：
1. **真实 Telegram Bot API 发帖 / 置顶 / 删帖 / Webhook 回调联调**：需真实 Bot Token 与测试频道，本次全部通过隔离 `MockGateway` 模拟，待 Phase 0 修复完成后在沙盒测试频道验证。
2. **真实 Telegram Stars 支付回调与充值入账**：本地已通过 Mock Payload 验证幂等性，未触发真实 Stars 扣费。
3. **`TelegramABC` PostgreSQL + Redis + Celery + Telethon 实网容器联调**：`TelegramABC` 完整运行依赖外部 PostgreSQL 16、Redis 7 与真实 Telethon `.session` 文件；本次仅执行了无副作用的架构拓扑检查与 `plugin_gateway` 单元测试，未读取任何 `.session` 密钥或连接生产库。
