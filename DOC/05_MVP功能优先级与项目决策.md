# 05｜MVP 功能优先级、代码审计与合规决策（任务 E & 调研项 3、4、9、10）

> **核查日期**：2026-11-05  
> **研究范围**：`ChaBo` 本地源码与测试实跑审计、`TelegramABC` 集成边界评估、R1/R2 功能模块全面打分、三端交互收敛方案及法律合规防线。

---

## 一、 `ChaBo` 现有代码实测审计与复用判定（调研项 3）

### 1. 测试套件实跑结果 【实测事实】
执行命令：`python3 -m compileall -q src tests && python3 -m unittest discover -s tests`
* **总计运行**：`189` 个自动化测试用例（耗时 `7.35s`）。
* **通过情况**：**`184` 个核心业务与账本测试全部通过（PASS）**；仅 `5` 个测试因沙箱环境限制本地 Socket 回环连接（`urllib.request.urlopen` 报 `Operation not permitted`）未能发起 HTTP 请求。
* **核心资产完好度**：
  - `tests/test_invariants.py`：硬账本资金守恒（`扣费 = 频道主净收益 + 平台服务费`）、并发扣款防超支、双人复核充值（禁止同账号自申自批）100% 验证通过。
  - `tests/test_webapi.py`：React 三端网页对应的 FastAPI 接口、RBAC 权限（`viewer / operator / finance / super_admin`）、管理员代看（Impersonation）与链式 SHA-256 防篡改审计 100% 完备。

### 2. 源码级重大发现：为什么绝对不需要推倒重写 `chabo-next`？
R1 报告指出 `src/chabo/fulfillment.py` 存在“数据库事务内包裹 Telegram 网络请求”的致命风险，并以此为主要理由之一建议另起炉灶新建 `chabo-next` 仓库。但我们深入审计源码后发现了两个关键事实：

1. **问题定位极其精确且集中**：
   - 在 `src/chabo/fulfillment.py` 第 `32～48` 行，`FulfillmentService.dispatch_due()` 使用了一个外层 `with self.db.transaction() as conn:` 循环调用 `_dispatch_one(conn, delivery)`（第 `72～196` 行）。
   - 在 `_dispatch_one` 内部，第 `106 / 121 / 131` 行调用了 `self.gateway.send_ad()`，第 `142` 行调用了 `self.gateway.pin_message()`，紧接着在第 `148` 行执行 `self.ledger.charge_delivery()` 扣款。如果发完最后一条消息时进程崩溃或 SQLite 锁超时，事务回滚会导致数据库状态仍停留在 `scheduled`，下一次轮询就会**重复发送同一条广告**。
2. **同一个代码库里其实已经写好了标准答案！**：
   - 在 `src/chabo/services/channel.py` 第 `960～1042` 行的 `SelfPromoService.prepare_publish()` 中，作者已经写出了教科书级的正确实现（甚至在注释里写明了：`The actual gateway.send_ad call happens outside the DB transaction; the caller writes the resulting message_id back via mark_sent / mark_failed`）。
   - **结论**：只需花 **半天时间（约 80 行代码改动）**，仿照 `SelfPromoService` 把 `FulfillmentService.dispatch_due()` 拆分为 `claim_due_deliveries()`（事务内锁定状态为 `sending`） $\rightarrow$ 事务外调用 `gateway.send_ad()` $\rightarrow$ `settle_sent_delivery()` / `mark_send_unknown()`，即可彻底根除重复发帖隐患！为此推倒重写整个仓库纯属削足适履。

### 3. 存量代码必须清理的 3 项业务/技术债
1. **解除“广告主付费墙自杀限制”**：修改 `src/chabo/webapi/main.py` 与 `services/billing.py` 中对未购买 Pro 套餐广告主的频道浏览/批量计划限制，让所有充值广告主免费使用批量排期与结案报告。
2. **修正频道评分满分膨胀**：按《04_AI推荐与优化系统路线.md》重构 `PricingService.assess_channel()`（`src/chabo/services/channel.py` 第 `1102` 行），解决当前演示频道全得 100 分的问题。
3. **冻结 `bot.py`（7,103 行）膨胀**：严格执行《CLAUDE.md》规定，新功能（短链跳转 `/r/{token}`、结案报告页 `/public/reports/{token}`、`TelegramABC` 频道同步）全部写在 `src/chabo/services/` 与 `src/chabo/webapi/` 下，保持单文件 $\le 300$ 行。

---

## 二、 `TelegramABC` 集成评估与接口契约（调研项 4）

根据对 `TelegramABC` 架构的核查，确定两系统采用**「松耦合 HTTP / 只读适配器」**对接，绝不共享同一个可写业务数据库：

| 集成能力域 | `TelegramABC` 既有基础 | 当前缺失或不可直接用之处 | 插播 V1 对接实现方案（`TelegramABCAdapter`） |
| :--- | :--- | :--- | :--- |
| **1. 自有频道资产同步** | 拥有完整的频道资产表、分类标签与基础周增长快照（`metric_snapshots.py`） | 字段命名与 `ChaBo` 的 `channels` 表不完全一致 | 在 `ChaBo` 新增 `sync_from_abc` 服务：拉取频道 `tg_chat_id`、`title`、`username`、`category`、`subscribers`，自动写入或更新 `ChaBo` 的 `channels` 表 |
| **2. 广告定时发布执行** | 曾有 `ad_recurring_bot_api.py`，但旧路由 `recurring_ad.py` 已废弃（返回 `410 Gone`） | 缺少与 `ChaBo` 硬账本预冻结、24h 观察期联动的状态机 | **发布动作由 `ChaBo` 自己的 `FulfillmentService` 直接调用 Bot API 完成**，不绕道 `TelegramABC` 下发发帖任务，避免跨系统状态不同步导致双发或漏扣费 |
| **3. 帖子浏览量（Views）与在位核验** | `modules/tg_registry/adapter/userbot.py` 封装了 MTProto `get_messages` 与 `get_full_channel` | 尚未暴露按指定 `delivery_id` 列表定时采集 1h/6h/24h 浏览量的专用端点 | **短期（M1）**：由 `TelegramABC` 提供一个轻量内部只读接口 `POST /internal/tg/messages-views`（输入 `chat_id + message_ids`，通过 MTProto `channels.getMessages` 返回 `views/forwards/exists`）；若暂未连通，支持在管理后台导入或用独立 MTProto Bot 读取 |

---

## 三、 MVP 功能全面评估与优先级裁决表（任务 E & 调研项 9）

我们对 R1 和 R2 中提到的全部 18 项功能进行了 7 维度量化评估，找出**“最少但足以跑通真实收钱、可信履约与高复购”**的功能组合：

| 功能模块 | 商业价值 | 用户价值 | 技术可行性 | 数据依赖 | 开发复杂度 | 合规风险 | 扩展价值 | **最终裁决** |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **1. 预充值硬账本 + OTC 双人复核入账** | 极高（防坏账） | 高（账目透明） | **已实现** | 无 | **0（现成）** | 低（记账积分非吸储） | 极高 | **P0（直接启用）** |
| **2. 网页端三门户（管理/广告主/频道主）** | 高 | 极高（告别繁琐按钮） | **已实现** | 无 | **0（现成）** | 低 | 极高 | **P0（直接启用）** |
| **3. 发帖防重发状态机改造（Outbox/Intent）** | 极高（防赔钱事故） | 极高 | 高 | 无 | **低（0.5天）** | 无 | 高 | **P0（第一优先必改）** |
| **4. 广告底部 `[📣 频道招商]` + `[🔍 查看详情]` 按钮** | 极高（免费获客+活人探针） | 高 | **已实现**（`fulfillment.py:208`） | 无 | **0（现成）** | 低 | 极高 | **P0（保留并强化）** |
| **5. 外部网页 `/r/{token}` 302 短链 + 预加载防刷过滤** | 极高（外链归因核心） | 极高 | 高 | 需维护爬虫 UA 与机房特征库 | **低（2天）** | 低（短期 Hash 不存隐私） | 极高 | **P0（核心新增块 1）** |
| **6. 帖子 1h/6h/24h 浏览量快照与防删帖巡检** | 极高（防秒删+算 CPM/CTR） | 极高 | 高（需 MTProto 读 `views`） | 需 `channels.getMessages` 权限 | **中（2天）** | 低 | 极高 | **P0（核心新增块 2）** |
| **7. 免登录只读结案报告页（含 V/P/R 标签 + 同场红黑榜 + 一键复投）** | **决定复购生死** | 极高 | 高 | 依赖第 4、5、6 项采集结果 | **中（3天）** | 低 | 极高 | **P0（核心新增块 3）** |
| **8. `TelegramABC` 自有频道导入与套餐打包配置** | 极高（冷启动立赚现金流） | 高 | 高 | 依赖自有频道清单 | **低（1～2天）** | 无 | 高 | **P0（核心新增块 4）** |
| **9. 频道三维评分 v2（含置信度与刷量异常灯）** | 高 | 高（辅助选号） | 高 | 少量浏览与探针快照 | **低（1天）** | 低（纯数学规则） | 高 | **P1（首单跑通后紧接上线）** |
| **10. 目标频道专属邀请链接（`createChatInviteLink`）入群归因** | 高（TG 内互推刚需） | 高 | 高 | 需目标频道授予管理员权限 | **低（1.5天）** | 低 | 高 | **P1（推广 TG 频道客户专用）** |
| **11. 商户 S2S Postback 转化回传接口** | 中（仅大技术商户用） | 中 | 高 | 依赖商户技术配合开发 | **低（1.5天）** | 低 | 高 | **P1（有客户配合时再开）** |
| **12. 广告主表单第一方数据 AI 文案润色与报告摘要** | 中（提效） | 中 | 高 | 仅用广告主自填表单与汇总表 | **低（1天）** | 中（需严守数据隔离） | 中 | **P1（锦上添花）** |
| **13. 广告主选号/报表 Pro 付费墙** | **负价值（早期赶客）** | 负 | 已实现 | 需百个频道规模才成立 | **0（需关闭限制）** | 低 | 远期高 | **P2（V1 强制免费开放，冻结收费墙）** |
| **14. 全自动链上 USDT 充提热钱包** | 低 | 中 | 中 | 区块链节点 | **高** | **极高（资金被盗/牌照合规）** | 中 | **P2（坚决不做）** |
| **15. 全网未授权公开频道批量爬虫（仿 TGStat）** | 低 | 中 | 低（易封号） | 庞大爬虫账号池 | **极高** | **极高（违反 TG 条款 4.3）** | 低 | **P2（永久禁止）** |
| **16. 自动 tCPC / tCPA 实时竞价引擎** | 低（小样本失效） | 低 | 低 | 需数万次转化样本 | **极高** | 高（涉及训练模型限制） | 远期 | **P2（100单前禁止碰）** |

---

## 四、 合规与风险全景防控指南（调研项 10）

| 风险域 | 具体致命场景 | 风险等级 | 强制落地的技术和运营防御措施 |
| :--- | :--- | :---: | :--- |
| **1. Telegram 平台条款与封号风险** | ① 爬取公开频道训练 AI 触发 API Terms 1.5 封禁；<br/>② 同一秒群发几十个频道触发 `429 FloodWait` 甚至封禁 Bot；<br/>③ 强行私聊未启动 Bot 的用户触发垃圾消息封号 | **严重** | 1. 严禁抓未授权频道，严禁把 TG 原始数据喂第三方大模型；<br/>2. 发帖调度器强制速率限制（同频道间隔 $\ge 3$ 秒，全局每秒 $\le 15$ 条，捕获 `retry_after` 指数退避）；<br/>3. 仅向已主动 `/start` 的用户发通知 |
| **2. 广告内容与品类合规风险** | 接入诈骗（杀猪盘/假空投）、儿童色情、恐怖主义、盗版侵权或当地严打黑产，导致**自有核心频道连坐被 Telegram 官方封禁（Channel Ban）** | **严重（毁灭级）** | 1. **人工终审铁律**：V1 生产环境强制关闭自动过审（`auto_approve = False`），每条广告及目标 URL 必须经管理员点击确认；<br/>2. **短链目标锁死**：审核通过后，`/r/{token}` 对应的 `target_url` 写入不可变快照，严禁广告主在审核通过后偷换目标网页（防偷梁换柱钓鱼）；<br/>3. **频道分级隔离**：自有高价值白名单频道严禁接高风险灰产广告 |
| **3. 资金结算与金融合规风险** | 若开放“任何人自由充提 USDT 且可随时提现给他人”，易被黑灰产当作洗钱通道（AML 风险）或被定性为无牌照资金托管 | **高** | 1. **定性为“广告预付消耗金（Ad Credits）”而非电子钱包**：广告主充值仅限用于购买平台广告服务，不支持随意转账或无理由提现洗出；<br/>2. **双人复核 + 交易留痕**：OTC 入账必须保留付款凭证链接与双管理员审批记录；<br/>3. **外部频道主结算采用周期性人工审核打款**（如满 \$50 每周固定日人工核验打款） |
| **4. 数据隐私与外链安全风险** | ① `/r/{token}` 被构造为开放重定向漏洞（Open Redirect）或探测内网（SSRF）；<br/>② 长期明文保存点击用户的 IP 与设备指纹引发隐私合规投诉 | **中高** | 1. `/r/{token}` 仅接受数据库已审核生成的 opaque token，校验协议仅限 `https://` 或 `http://`（拦截 `javascript:`、`file:`、内网 IP 段）；<br/>2. 访问 IP 与 UA 使用每日轮换盐值（Daily Salt）生成不可逆 `visitor_hash`，原始请求日志 7 天自动清理 |
