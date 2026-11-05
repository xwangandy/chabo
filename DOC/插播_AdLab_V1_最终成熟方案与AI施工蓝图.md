# 插播 Ad Lab V1：终极合流裁决、阶段落地路线与 AI 工程施工蓝图（Final Master Plan）

> **一句话结论（TL;DR）**：
> 我们已将云端最新拉取的 6 份代码审计报告（[AUDIT_00](file:///Users/lanjinglive618/ChaBo/audit/AUDIT_00_负责人摘要.md)～[AUDIT_05](file:///Users/lanjinglive618/ChaBo/audit/AUDIT_05_开发阶段与验收建议.md)）、我方完成的 6 份行业与归因深度调研（[00_负责人决策摘要.md](file:///Users/lanjinglive618/ChaBo/DOC/00_负责人决策摘要.md)～[05_MVP功能优先级与项目决策.md](file:///Users/lanjinglive618/ChaBo/DOC/05_MVP功能优先级与项目决策.md)）以及原始 [R1 企划案](file:///Users/lanjinglive618/ChaBo/插播_AdLab_V1_研究与设计报告.docx) 与真实源码（[src/chabo/](file:///Users/lanjinglive618/ChaBo/src/chabo)）进行了逐行交叉核验。
> **三方结论高度收敛，调研阶段正式收官！** 本方案为您提供：**① 终极合流裁决结论、② 进入开发阶段前只需拍板的 5 项准入清单、③ 从 0 到 100 单的分阶段落地流程与未来商业演进指引、④ 严格遵循 [CLAUDE.md](file:///Users/lanjinglive618/ChaBo/CLAUDE.md) 铁律的 12 个可直接喂给 AI 执行的工程施工切片（S01～S12）。**

---

## 一、 三方报告（R1 + R2.1 深度调研 + 云端 AUDIT 00-05）终极合流裁决

### 1.1 100% 达成共识并锁定的四大核心战略

| 决策维度 | 最终锁定方案（已无需再争论） | 核心依据（源码 + 云端审计 + 行业调研交叉印证） |
| :--- | :--- | :--- |
| **1. 工程演进路线** | **锁定“路线 B”**：保留现有 `ChaBo` 仓库（75% 成熟骨架），做**局部靶向重构**；严禁新建 `chabo-next` 重写（拒斥路线 C），严禁带病直接堆新功能（拒斥路线 A）。 | 现有代码已跑通 189 个单测，具备完整的四权分立账本、托管担保、45 张表结构与前后端三角色路由（见 [AUDIT_03_架构路线对比.md](file:///Users/lanjinglive618/ChaBo/audit/AUDIT_03_架构路线对比.md)）。推倒重写将白白浪费 4～6 周并丢失业务边界细节。 |
| **2. 数据库选型** | **V1 阶段继续使用 SQLite WAL**（需修复连接关闭与长事务锁），不迁移 PostgreSQL。 | [AUDIT_03](file:///Users/lanjinglive618/ChaBo/audit/AUDIT_03_架构路线对比.md) 证实：此前偶发的锁等待并非 SQLite 性能瓶颈，而是 [`FulfillmentService.dispatch_due`](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py#L29-L48) 在一把数据库写锁（`BEGIN IMMEDIATE`，好比把整个账房大门反锁）里串行等待 20 次外部 Telegram 网络请求（最长卡死 300 秒）。拆开长事务后，SQLite WAL 轻松支撑日均万单，且天然保证审计哈希链（`audit_logs`）单链不分裂。 |
| **3. `TelegramABC` 与 `TGWorker` 协同边界** | **只读网关协同，物理隔离 `.session` 与发帖**：<br>① 通过 `TelegramABC` 已有的只读网关（`GET /api/plugins/assets/channels`）同步自有频道名录及 1h/6h/24h/48h 帖子真实浏览量（`views`）；<br>② **广告发帖、置顶、撤回必须由 `ChaBo` 自己的 [`BotApiClient`](file:///Users/lanjinglive618/ChaBo/src/chabo/telegram_api.py) 直接执行**。 | `TelegramABC` 底层使用 `session_lock.py` 独占锁，跨进程共享 `.session` 文件会触发 Telegram 官方 `AuthKeyDuplicatedError`（直接封禁协议号）；而 `TelegramABC` 的通用发帖任务不支持 `ChaBo` 独有的“三按钮探针深链 + 托管账本原子划转”（见 [AUDIT_03](file:///Users/lanjinglive618/ChaBo/audit/AUDIT_03_架构路线对比.md#L79-L115)）。 |
| **4. 冷启动与智能推荐演进** | **先做“确定性可解释规则 + 自有频道套餐破局”，积累 50～200 单后再上重算法**：<br>V1 用“贝叶斯平滑评分 + 置信度标签 + 规则四象限建议 + 一键复投”替代黑盒机器学习。 | 当前 [`chabo.sqlite3`](file:///Users/lanjinglive618/ChaBo/chabo.sqlite3) 真实已交付订单为 `0` 笔（仅 6 笔测试待审单）。在零样本期强上 Bandit/协同过滤纯属伪科学（见 [04_AI推荐与优化系统路线.md](file:///Users/lanjinglive618/ChaBo/DOC/04_AI推荐与优化系统路线.md)）。 |

---

### 1.2 云端 AUDIT 与我方 R2.1 调研的“互补盲区修正”（极关键！）

把云端 [AUDIT_01](file:///Users/lanjinglive618/ChaBo/audit/AUDIT_01_系统现状与测试结果.md)～[AUDIT_05](file:///Users/lanjinglive618/ChaBo/audit/AUDIT_05_开发阶段与验收建议.md) 与我方 [01](file:///Users/lanjinglive618/ChaBo/DOC/01_竞品能力与差异化矩阵.md)～[05](file:///Users/lanjinglive618/ChaBo/DOC/05_MVP功能优先级与项目决策.md) 报告合流后，我们既拿到了云端审计精准定位的 **16 个源码级缺陷（`DEF-01`～`DEF-16`）**，又用我方商业与归因调研修正了云端审计的 **5 个盲区与笔误**，确保后续 AI 施工零踩坑：

1. **【修正跳转归因盲区】严禁将 `t.me` 链接无脑套入 `/r/{token}` 外部短链**：
   - **问题**：云端 [AUDIT_04](file:///Users/lanjinglive618/ChaBo/audit/AUDIT_04_MVP功能差距与依赖.md) 建议将所有广告的 `target_url` 统一替换为 `https://<domain>/r/{token}` 302 短链。
   - **修正**：我方在 [02_Telegram数据与归因可行性.md](file:///Users/lanjinglive618/ChaBo/DOC/02_Telegram数据与归因可行性.md) 中证实——如果广告主推广的是 **Telegram 频道、群组或 Bot（`t.me/...`）**，套用外部 HTTPS 短链会在手机端弹出“即将离开 Telegram 打开外部链接”警告框并唤醒系统浏览器，导致转化率暴跌 **30%～50%**！
   - **终极合流规则**：系统自动识别 `target_url` 类型：
     - **站外网页（`http/https` 非 `t.me`）** $\rightarrow$ 自动包裹 `/r/{token}` 短链，并执行四层防刷过滤（拦截 `TelegramBot (like TwitterBot)` 预加载爬虫、ヘッドレス UA、IP 频控、5 分钟去重）。
     - **站内目标（`t.me/...` 或 `@username`）** $\rightarrow$ **保持原生 `t.me` 丝滑跳转**，优先使用专属邀请链接（`createChatInviteLink`）、合作 Bot `?start=` 参数，或通过广告下方的 `[🔍 查看详情]` 站内探针按钮完成免弹框二跳归因。
2. **【修正探针防刷盲区】站内探针（`light_probes`）不能只靠 `UNIQUE(order_id, user_id)` 判别 100% 真人**：
   - **问题**：R2 初稿声称站内探针能识别 100% 真人，云端 [AUDIT_02](file:///Users/lanjinglive618/ChaBo/audit/AUDIT_02_功能复用与缺陷清单.md) 指出其当前未回写 `deliveries.click_count`。
   - **修正**：黑产可用 MTProto 协议号（Userbot）批量调用 `GetBotCallbackAnswer` 刷回调。修复时不仅要将探针去重人数回写到 [`deliveries.click_count`](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py#L298-L315)，还必须在 [`LightProbeService.record_probe`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py#L462-L526) 中加入**四维真人校验（过滤 `is_bot`、检测 User ID 集群连号段、发布后 $<1.5\text{s}$ 毫秒级规律点击降权、检验是否伴随二跳动作）**。
3. **【修正商业转化盲区】必须立刻拆除早期对广告主的 SaaS 订阅付费墙**：
   - **问题**：源码 [`AdvertiserService.report`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/billing.py#L973-L975) 将分频道明细挡在付费墙后（`by_channel if entitlements["full_report"] else []`），且 [`create_batch_orders`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/billing.py#L994) 拦截未买月卡的广告主批量下单。
   - **修正**：在平台冷启动期，让广告主“先交月租才能看报表和批量买广告”会直接把客户推向免费看表的 Telega.io。**阶段 0/1 必须向所有广告主免费开放完整投放报告、分频道明细与购物车批量下单**。
4. **【修正评分失真盲区】修复 [`PricingService.assess_channel`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/channel.py#L1102-L1247) 全员 100 分 S 级膨胀**：
   - **问题**：云端 [AUDIT_02](file:///Users/lanjinglive618/ChaBo/audit/AUDIT_02_功能复用与缺陷清单.md) 认为现有五维评分可直接使用；但我方实测 [`chabo.sqlite3`](file:///Users/lanjinglive618/ChaBo/chabo.sqlite3) 发现，因公式 `min(100, int((14000*0.4 + 11500*0.3 + 10000*0.3)/100))` 放大系数过高，库内所有频道清一色拿到 **100 分（S 级）**，丧失区分度。
   - **修正**：在阶段 2 将其改造为基于**贝叶斯平滑 CTR + 触达率稳定性 + 样本置信度 $C \in [0,1]$** 的真实梯队评分，且当历史完结单 $< 5$ 单时强制标注 `[🌱 新晋测品 · 低置信度]`。
5. **【修正工程表名与路径对齐】杜绝 AI 改代码时找错表和目录**：
   - 云端审计报告中使用了部分简写名称，我们在本施工蓝图中已全部校准为 [`src/chabo/db.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py)（共 45 张表）与 [`web/src/routes/`](file:///Users/lanjinglive618/ChaBo/web/src/routes) 中的**真实物理名称**：
     - 账本流水表：真实表名为 `ledger_transactions`（非 `ledger_entries`）
     - 人工充值审批表：真实表名为 `topup_requests`（非 `topup_approval_requests`）
     - Stars 支付表：真实表名为 `stars_payment_intents`（非 `stars_payments`）
     - 前端页面目录：真实目录为 `web/src/routes/{admin,advertiser,publisher,auth}/`（非 `web/src/pages/`）

---

## 二、 进入下一阶段（工程开发期）前的 5 项拍板准入清单

**我们不再需要做任何外部调研。** 要正式启动代码改造，您只需确认以下 **5 项预设拍板决定**（若无异议，即按默认推荐方案直接执行）：

1. **【架构拍板】采用路线 B（基于现有 `ChaBo` 靶向修复 + SQLite WAL + `TelegramABC` 只读网关）**
   - **默认执行**：保留现有仓库与 189 个单测基线，先花 5～7 人日完成“阶段 0 止血”，绝不带病上线。
2. **【资金与抽佣拍板】全库统一以“美分（USD Cents，`*_cents`）”计价，打通“去尾标服务费”与“留尾标免佣裂变”**
   - **默认执行**：
     - $1.00\text{ USD} = 100\text{ cents}$（前端展示折算为 `$XX.XX`）。
     - 修复 [`OrderService._create_order_impl`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py#L878-L888) 中平台服务费恒为 0 的漏洞：频道主若开启 `[📣 频道招商]` 裂变尾标（`platform_promo_enabled=1`），平台免收或仅收极低通道费；若频道主去掉平台尾标且未买特权订阅，平台自动收取 **5%（`500 bps`）平台技术服务费**。
     - 自有频道（`TelegramABC` 矩阵）对外主打**“垂直行业联投套餐 + 15% 托管代投服务费”**。
3. **【归因路径拍板】执行“站外短链 + 站内原生深链/探针二跳”双轨制**
   - **默认执行**：站外网页走 `/r/{token}` 302 短链（含四层反爬虫过滤）；`t.me` 站内目标保持原生跳转 + `[🔍 查看详情]` 站内探针回写 `deliveries.click_count`。
4. **【产品权限拍板】拆除广告主早期付费墙，收紧新注册默认状态与自动审单开关**
   - **默认执行**：免费向所有广告主开放完整报表与批量下单；将新用户注册默认状态由死锁的 `'candidate'` 改为 `'active'`；将 [`CHABO_BOT_AUTO_APPROVE_ORDERS`](file:///Users/lanjinglive618/ChaBo/src/chabo/config.py#L46-L52) 生产默认值改为 `False`（冷启动期由人工/运营把关审核，防止违规广告炸群）。
5. **【基础设施准备】开发联调期间需准备的 3 项环境配置**（可在阶段 0 开发期间同步准备，不阻塞今日开工）：
   - 1 个用于真实沙盒发帖/置顶/撤回验收的 Telegram 测试频道及测试 Bot Token；
   - `TelegramABC` 本地或内网 API 地址及 `X-Plugin-Token`（用于阶段 1 联调频道同步与 `views` 回采）；
   - 首批 5～10 个 `TelegramABC` 自有种子频道的分类与刊例价清单（用于阶段 3 冷启动售卖）。

---

## 三、 项目分阶段目标、落地流程与商业演进路线图

我们将整个项目从“代码止血”到“商业规模化盈利”划分为 **4 个清晰阶段（阶段 0～阶段 3）**，每个阶段都有硬性的**门禁验收标准（Gate）**，未通过门禁绝不进入下一阶段：

```mermaid
flowchart LR
    P0["阶段 0：止血与账本加固<br/>(第 1 周 / 5~7 人日)"] -->|通过 Gate G1<br/>189+7 测试全绿| P1["阶段 1：MVP 投放与双轨采集闭环<br/>(第 2~3 周 / 8~10 人日)"]
    P1 -->|通过 Gate G2<br/>沙盒 10 步真实闭环| P2["阶段 2：可信报告与一键复投<br/>(第 4 周 / 5~7 人日)"]
    P2 -->|通过 Gate G3<br/>首单真实商业交付| P3["阶段 3：自有矩阵冷启动与裂变放量<br/>(M2~M6 商业扩张)"]
```

### 阶段 0：止血与账本加固期（第 1 周，约 5～7 人日）
- **核心业务目标**：彻底消灭会导致**“白嫖重复发帖、充值并发双倍入账、负数充值盗刷、暂停单 0 冻结白嫖、管理员伪装越权、报表翻倍失真”**的 7 个 P0 级与 9 个 P1 级致命缺陷。
- **关键动作**：
  1. 重构 [`FulfillmentService.dispatch_due`](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py#L29-L162) 为三段式状态机（先短事务锁单 `sending` $\rightarrow$ 释放锁调 Telegram API 发帖/置顶 $\rightarrow$ 短事务记账 `sent` 或异常挂起 `reconciliation_required`），并用 `try/except` 隔离预算告警私信（[`_maybe_notify_budget`](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py#L334-L350)）。
  2. 收口充值审批事务（[`approve_topup`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/wallet.py#L612-L654)）、封堵负数充值（[`manual_topup`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/wallet.py#L435-L482)）、禁止未重冻资金的 `paused` 订单直接审批通过（[`approve_order`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py#L1122-L1145)）、固化订单创建时的频道主快照 `publisher_account_id`。
  3. 强制管理员伪装会话（Impersonation）为只读模式，修复新用户 `'candidate'` 403 死锁，统一 [`webapi/main.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/main.py) 入口并补齐审计日志。
- **门禁 G1（进入阶段 1 的硬标准）**：189 个既有测试 + 新增 7 个 P0 攻防回归测试 100% 通过，SQLite 压测零 `database is locked`。

### 阶段 1：MVP 投放与双轨数据采集闭环（第 2～3 周，约 8～10 人日）
- **核心业务目标**：跑通从“选号下单 $\rightarrow$ 准点发帖与置顶 $\rightarrow$ 到期自动撤下 $\rightarrow$ 真实浏览量（`views`）与防刷点击量（`clicks`）自动回采 $\rightarrow$ T+24h 自动分账与 T+7d 尾款释放”的全自动化履约闭环。
- **关键动作**：
  1. 实现 `TelegramABC` 只读网关客户端，支持一键导入自有频道及按 1h/6h/24h/48h 窗口拉取帖子真实 `views` 写入 [`deliveries`](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py#L298-L315) 与 `delivery_metric_snapshots`。
  2. 实现站外 `/r/{token}` 302 防刷短链（含 `TelegramBot` 预加载过滤）与站内 `[🔍 查看详情]` 探针点击回写 [`deliveries.click_count`](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py#L298-L315)。
  3. 将批量提交计划（[`submit_plan`](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/plans.py#L172-L267)）改为单事务原子下单，补齐频控校验（每日上限、静默时段、黑名单）与置顶/保底时长到期自动 `unpublish_due`（自动 `unpinChatMessage` / `deleteMessage`）。
- **门禁 G2（进入阶段 2 的硬标准）**：在真实 Telegram 沙盒频道跑通“十步端到端测试”（含真实发帖、置顶、点击过滤、浏览量回填、T+24h 结算、到期取消置顶），全流程账平且零人工改库。

### 阶段 2：可信结案报告、可解释推荐与一键复投（第 4 周，约 5～7 人日）
- **核心业务目标**：向广告主交付競品（Telega.io）给不出的**“高可信结案战报 + 四象限优化处方 + 一键复投”**，建立复购飞轮。
- **关键动作**：
  1. 修复 [`AdvertiserService.report`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/billing.py#L946-L962) 因 `LEFT JOIN deliveries` 导致花费与订单数翻倍的 SQL 缺陷，并拆除 `entitlements["full_report"]` 付费墙。
  2. 升级 [`PricingService.assess_channel`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/channel.py#L1102-L1247) 为贝叶斯平滑评分 + 样本置信度 $C$ 标签；改造确定性四象限建议引擎（[**🚀 加大投放 `scale_up`** / **🎨 更换素材 `replace_creative`** / **⏳ 降低频次 `reduce_frequency`** / **🛑 暂停该号 `pause_channel`**]）。
  3. 打通前端 [`AdvertiserReportPage.tsx`](file:///Users/lanjinglive618/ChaBo/web/src/routes/advertiser/AdvertiserReportPage.tsx) 真实 API 渲染、免登录公开分享战报页 `/public/reports/:share_token` 以及 `[🚀 按优化建议一键复投]` 按钮。
- **门禁 G3（具备正式商业发售条件）**：广告主无需技术背景，3 分钟内可看懂结案报告并一键生成优化后的复投草稿单。

### 阶段 3：商业化冷启动与增长飞轮（M2～M6 未来演进指引）
- **第 1 步：自有频道打包破局（0 $\rightarrow$ 30 单，M1～M2）**
  - 利用 `TelegramABC` 管理的自有频道矩阵，封装 3 款标准化套餐（如 **“Web3/科技/出海 5 频道联投测试包 \$199”**，内含 10%～15% 新号探索预算）。
  - **财务安全垫**：我方 [03_商业模式与利润模拟.md](file:///Users/lanjinglive618/ChaBo/DOC/03_商业模式与利润模拟.md) 测算证明，纯靠第三方 8% 撮合抽佣需月均 **163 单（\$32,615 GMV）** 才能养活 1 名运营；而售卖**自有频道套餐（100% 媒体毛利）+ 15% 代投服务费**，每月仅需 **3.5 单（\$700 GMV）** 即可跨越盈亏平衡点！
- **第 2 步：尾标裂变吸纳第三方频道（30 $\rightarrow$ 200 单，M3～M4）**
  - 所有开启免费档的频道在发布广告时，底部自动挂载 `[📊 本广告由 插播 Ad Lab 托管验真 | 📣 我也要接单/投广告]` 按钮（带 `?start=ref_ch_{channel_id}` 归因参数）。
  - 广告主转发免登录战报页 `/public/reports/:token` 至同行群，实现零获客成本（CAC = \$0）的双向裂变。
- **第 3 步：数据智能与高毛利订阅变现（200 单+，M5～M6）**
  - 当单频道积累 $\ge 15$ 笔真实投放后，由“规则评分”平滑升级至“Contextual Thompson Sampling（上下文汤普森采样）+ 30% 冷启动探索护栏”，并在入驻活跃频道突破 100 个后开启频道主去尾标/优先排位特权订阅。

---

## 四、 可直接交给 AI 落地的标准化工程施工路径（12 个原子切片 S01～S12）

为确保 AI（Claude Code / Antigravity）在写代码时不破坏现有架构、不改出新 Bug，本工程路径严格受项目根目录 [CLAUDE.md](file:///Users/lanjinglive618/ChaBo/CLAUDE.md) 约束：
1. **单向依赖铁律**：`L0 (config/db/models)` $\leftarrow$ `L1 (telegram_api/gateway)` $\leftarrow$ `L2 (services/*)` $\leftarrow$ `L3 (fulfillment)` $\leftarrow$ `L4 (bot/webapi)` $\leftarrow$ `L5 (web/src)`，严禁逆向 import。
2. **微创切片铁律**：每个切片修改存量老文件 **不超过 3 个**，单文件代码行数保持 **$\le 300$ 行**（对超长老文件采用提取新辅助模块的方式减负）。
3. **计划前置（Plan-First）与 10 秒冒烟验证**：每完成一个切片，必须立刻运行下方指定的 `unittest` 冒烟命令，绿灯后方可进入下一切片。

---

### 📦 第一批次：阶段 0 止血与账本安全加固（Slices S01～S05）

#### 切片 S01：SQLite 连接安全关闭与 WAL 锁超时统一（修复 `DEF-10`）
- **目标**：消除连接句柄泄漏，确保并发锁等待超时（30 秒）真实生效。
- **涉及文件（仅 1 个老文件 + 1 个测试文件）**：
  - [src/chabo/db.py](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py)
  - [tests/test_db_concurrency.py](file:///Users/lanjinglive618/ChaBo/tests/test_db_concurrency.py)（新增）
- **精准改造指令**：
  1. 在 [`Database.connect()`](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py#L40-L60) 中使用 `@contextlib.contextmanager` 包装连接，在 `try...finally` 中显式执行 `conn.close()`（同时保留事务提交/回滚语义），防止 `with self.db.connect() as conn:` 退出后句柄残留。
  2. 将 `PRAGMA busy_timeout = 5000` 调整为 `PRAGMA busy_timeout = 30000`（与 `sqlite3.connect(..., timeout=30.0)` 保持一致）。
- **10 秒冒烟与验收命令**：
  ```bash
  python3 -m compileall -q src tests && python3 -m unittest discover -s tests -p "test_db*.py"
  ```

#### 切片 S02：账本充值防重入、负数校验与暂停单重审拦截（修复 `DEF-03`, `DEF-04`, `DEF-05`）
- **目标**：堵死并发审批双倍入账、负数充值盗刷及 `paused` 订单零冻结审核通过漏洞。
- **涉及文件（仅 2 个老文件 + 1 个测试文件）**：
  - [src/chabo/services/wallet.py](file:///Users/lanjinglive618/ChaBo/src/chabo/services/wallet.py)
  - [src/chabo/services/order.py](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py)
  - [tests/test_ledger_security.py](file:///Users/lanjinglive618/ChaBo/tests/test_ledger_security.py)（新增）
- **精准改造指令**：
  1. 在 [`LedgerService.manual_topup`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/wallet.py#L435-L482) 入口第一行增加 `if int(amount_cents) <= 0: raise ValueError("amount_cents must be positive")`。
  2. 重构 [`TopupApprovalService.approve_topup`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/wallet.py#L612-L654)：在同一个 `with self.db.connect() as conn:` 事务内首先执行原子条件更新 `UPDATE topup_requests SET status = 'approved', ... WHERE id = ? AND status = 'pending'`，检查 `cursor.rowcount == 1` 后再在同一 `conn` 内入账 `wallets` 与 `ledger_transactions`；若 `rowcount == 0` 直接抛异常拒绝重入。
  3. 在 [`OrderService.approve_order`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py#L1122-L1145) 中，将允许审批的状态从 `{"pending_review", "paused"}` 严格收窄为仅 `{"pending_review"}`（若需恢复 `paused` 订单，必须调用重新执行 `reserve_order_budget` 的专用恢复方法，且校验 `reserved_cents >= price_cents - spent_cents`）。
- **10 秒冒烟与验收命令**：
  ```bash
  python3 -m unittest discover -s tests -p "test_*wallet*.py" && python3 -m unittest discover -s tests -p "test_ledger_security.py"
  ```

#### 切片 S03：订单频道主快照固化、7 天尾款自动释放与服务费计费修复（修复 `DEF-06`, `DEF-07`, `DEF-13`, `DEF-14`）
- **目标**：防止频道管理员中途换绑导致 24h 后结算崩溃；修复 5% 平台服务费恒为 0 的 Bug；闭环 10% 质保尾款（`holdback`）7 天释放；对齐三档调价与 `rate_cards`。
- **涉及文件（仅 3 个老文件）**：
  - [src/chabo/db.py](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py)
  - [src/chabo/services/order.py](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py)
  - [src/chabo/services/channel.py](file:///Users/lanjinglive618/ChaBo/src/chabo/services/channel.py)
- **精准改造指令**：
  1. 在 [`db.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py) 的 `ad_orders` 表迁移中新增 `publisher_account_id INTEGER` 字段；在 [`OrderService._create_order_impl`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py#L850-L920) 下单时将当时 `channel["owner_account_id"]` 快照写入 `ad_orders.publisher_account_id`；后续冻结、结算（`settle_delivery`、`confirm_due_earnings`）一律读取订单快照上的 `publisher_account_id`；同时在 [`ChannelService.bind_channel`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/channel.py#L82-L90) 中禁止非原主人在存在未完结订单时静默覆盖 `owner_account_id`。
  2. 修复 [`OrderService._create_order_impl`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py#L878-L888) 与 `_effective_service_fee_bps`：未订阅频道默认开启平台招商尾标（`platform_promo_enabled=1`）时免服务费，若关闭尾标且无免佣权益则按 `default_service_fee_bps`（500 bps = 5%）计收。
  3. 在 [`ChannelService.set_format_policy`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/channel.py#L292-L348) 切换 `discount / standard / premium` 档位时，同步将计算出的 `effective_price_cents` 更新回 `rate_cards.unit_price_cents`，并在 Web API 端禁用绕过三档规则的任意金额覆盖。
  4. 新增 `release_due_holdbacks(now_iso)` 方法：对 `deliveries` 结算满 7 天且无 `open` 争议的订单，将 10% 尾款转入频道主 `available_cents`。
- **10 秒冒烟与验收命令**：
  ```bash
  python3 -m unittest discover -s tests -p "test_*order*.py"
  ```

#### 切片 S04：履约引擎 `dispatch_due` 三段式无锁外部调用重构（修复 `DEF-01`, `DEF-02`）
- **目标**：彻底根治“发帖置顶成功后因预算提醒私信报错而回滚整笔事务（导致无限重发白嫖）”以及“置顶失败丢弃 `message_id` 全额退款”两大 P0 级事故。
- **涉及文件（仅 2 个老文件 + 1 个测试文件）**：
  - [src/chabo/db.py](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py)
  - [src/chabo/fulfillment.py](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py)
  - [tests/test_fulfillment_resilience.py](file:///Users/lanjinglive618/ChaBo/tests/test_fulfillment_resilience.py)（新增）
- **精准改造指令**（参照项目中 [`SelfPromoService.prepare_publish`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/channel.py#L960-L1042) 已验证的优秀模式）：
  1. **阶段 A（短事务抢占）**：在独立短事务中查询到期 `approved` 订单，写入或更新 `deliveries.status = 'sending'` 并立即 `commit` 释放 SQLite 写锁。
  2. **阶段 B（无锁外部 I/O）**：在数据库事务外调用 `self.gateway.send_ad(...)`；拿到 `message_id` 后**立即用微事务持久化 `message_id`**；若需置顶再调用 `self.gateway.pin_message(...)`。
  3. **阶段 C（短事务结算或挂起对账）**：
     - 若发帖与置顶均成功：开启短事务执行 `settle_delivery` 划账，标记 `deliveries.status = 'sent'`。
     - 若 `send_ad` 成功但 `pin_message` 失败：**严禁清空 `message_id` 或自动全额退费**！保留 `message_id`，将状态标为 `reconciliation_required`（或仅退还置顶差价），供运营台一键核销或撤回。
     - **阶段 D（隔离通知）**：将 [`_maybe_notify_budget`](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py#L334-L350) 移出结算事务外，并用 `try ... except Exception:` 完整包裹，私信失败仅写日志，绝不回滚已发送的广告账本！
- **10 秒冒烟与验收命令**：
  ```bash
  python3 -m unittest discover -s tests -p "test_*fulfillment*.py"
  ```

#### 切片 S05：权限门禁、管理员伪装只读化与 FastAPI 入口统一（修复 `DEF-08`, `DEF-09`, `DEF-15`, `DEF-16`）
- **目标**：封堵管理员伪装（Impersonation）写操作与审计嫁祸漏洞；解决新用户注册 403 死锁；统一 `webapi/main.py` 路由与 `Dockerfile`。
- **涉及文件（仅 3 个老文件）**：
  - [src/chabo/webapi/auth.py](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/auth.py)
  - [src/chabo/webapi/main.py](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/main.py)
  - [src/chabo/bot.py](file:///Users/lanjinglive618/ChaBo/src/chabo/bot.py)
- **精准改造指令**：
  1. 在 [`auth.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/auth.py) 与中间件中：当 JWT 包含 `impersonator_user_id` 时，拦截一切 `POST / PUT / PATCH / DELETE` 请求返回 `403 Read-only in impersonation mode`；审计日志记录真实管理员 ID + `on_behalf_of`。
  2. 新用户通过 Telegram 登录自动开通 `advertiser` / `publisher` 门户时，默认状态设为 `'active'`（而非 `'candidate'`）；`admin_level()` 缺失时默认返回最低权限 `'operator'`（而非 `'super_admin'`）。
  3. 在 [`bot.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/bot.py) 顶部补齐 `logger = logging.getLogger(__name__)`；将 [`config.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/config.py#L46-L52) 中 `CHABO_BOT_AUTO_APPROVE_ORDERS` 默认值改为 `False`；在 [`webapi/main.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/main.py) 中挂载 `/telegram/webhook` 与内部定时触发端点，并修正 [`Dockerfile`](file:///Users/lanjinglive618/ChaBo/Dockerfile) 为 `pip install ".[web]"`。
- **10 秒冒烟与验收命令（同时执行 Gate G1 总验收）**：
  ```bash
  python3 -m compileall -q src tests && python3 -m unittest discover -s tests
  ```

---

### 📦 第二批次：阶段 1 MVP 投放闭环与双轨数据采集（Slices S06～S09）

#### 切片 S06：批量计划原子下单、频道防打扰频控与到期自动撤回（修复 `DEF-11` + `G-04`）
- **目标**：让广告主购物车批量下单在同一事务内原子完成、共享同一个 `campaign_id`、透传 `pin_enabled`，并支持置顶/保底时长到期自动取消置顶或删帖。
- **涉及文件（仅 3 个文件）**：
  - [src/chabo/webapi/plans.py](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/plans.py)
  - [src/chabo/services/order.py](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py)
  - [src/chabo/fulfillment.py](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py)
- **精准改造指令**：
  1. 在 [`OrderService`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py) 中提供单事务批量下单方法（拆除 `create_batch_orders` 的 Pro 订阅拦截），供 [`submit_plan`](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/plans.py#L172-L267) 调用，任一子单余额不足或频控冲突则整批回滚。
  2. 在下单与排期时校验 `channel_configs` 的 `daily_ad_limit`、禁投静默时段与敏感词黑名单；修复 `maybe_schedule_next` 按 `24 / frequency_per_day` 小时推进排期。
  3. 在 `deliveries` 新增 `unpin_at` / `delete_at` 字段，在 [`FulfillmentService`](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py) 新增 `unpublish_due(now_iso)` 定时调用 `unpinChatMessage` / `deleteMessage`。
- **10 秒冒烟与验收命令**：
  ```bash
  python3 -m unittest discover -s tests -p "test_*plan*.py"
  ```

#### 切片 S07：双轨点击归因——站外 `/r/{token}` 四层防刷短链 + 站内探针回写（实现 `G-01`）
- **目标**：实现真实有效的点击追踪，同时遵守“站外链接走 `/r/{token}` 防刷短链、`t.me` 站内链接走原生深链/探针二跳”的铁律。
- **涉及文件（新建 1 个独立路由模块 + 修改 2 个老文件）**：
  - `src/chabo/webapi/redirect.py`（新建，保持 `< 200` 行）
  - [src/chabo/db.py](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py)
  - [src/chabo/services/order.py](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py)
- **精准改造指令**：
  1. 在 [`db.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py) 新增 `click_events` 表（记录 `order_id, delivery_id, token, ip_hash, ua_hash, is_bot_prefetch, is_unique, created_at`）。
  2. 新建 `src/chabo/webapi/redirect.py` 暴露 `GET /r/{token}`：
     - **第 1 层**：识别 `TelegramBot (like TwitterBot)`、`curl`、`python-requests`、`HeadlessChrome` 等预览/爬虫 UA $\rightarrow$ 标记 `is_bot_prefetch=1`，返回 302 跳转但**严禁计入有效点击**；
     - **第 2 层**：验证点击时间 $\ge$ 广告实际发送时间 `sent_at`（且对 $<1.5\text{s}$ 规律并发打标）；
     - **第 3 层**：同一 `(order_id, ip_hash, ua_hash)` 5 分钟内仅计 1 次有效 UV，原子累加 `deliveries.click_count`。
  3. 在 [`FulfillmentService`](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py) 构造按钮时：仅当 `target_url` 为非 `t.me` 的外部网页时自动包裹 `/r/{token}`；若为 `t.me/...` 则保留原生直跳，并在 [`LightProbeService.record_probe`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py#L462-L526) 中通过四维真人校验后同步累加 `deliveries.click_count`。
- **10 秒冒烟与验收命令**：
  ```bash
  python3 -m unittest discover -s tests -p "test_*redirect*.py"
  ```

#### 切片 S08：`TelegramABC` 只读网关对接——自有频道同步与 1h/6h/24h/48h 浏览量采样（实现 `G-02`, `G-03`）
- **目标**：零 `.session` 冲突地从 `TelegramABC` 同步自有频道资产，并自动回填已发布广告帖的真实 MTProto `views`。
- **涉及文件（新建 1 个网关客户端 + 修改 2 个老文件）**：
  - `src/chabo/telegram_abc_client.py`（新建 L1 层适配器，`< 200` 行）
  - [src/chabo/db.py](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py)
  - [src/chabo/fulfillment.py](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py)
- **精准改造指令**：
  1. 在 [`db.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/db.py) 新增 `delivery_metric_snapshots` 表（`delivery_id, checkpoint_label, views_count, forwards_count, sampled_at`）。
  2. 新建 `src/chabo/telegram_abc_client.py`：通过 HTTP Header `X-Plugin-Token` 调用 `TelegramABC` 的 `GET /api/plugins/assets/channels` 与帖子浏览量批量查询接口（并支持在无 `TelegramABC` 时优雅降级为 Bot API / 手工校准）。
  3. 在 [`FulfillmentService`](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py) 新增 `sync_delivery_metrics(now_iso)`：对已发送满 1h、6h、24h、48h 的 `deliveries` 采集 `views`，更新 `deliveries.impression_count` 并写入快照表。
- **10 秒冒烟与验收命令**：
  ```bash
  python3 -m unittest discover -s tests -p "test_*abc*.py"
  ```

#### 切片 S09：轻量级内置调度器（Lifespan Background Scheduler）
- **目标**：无需额外部署 Celery/Redis，在 FastAPI 进程内自动按分钟驱动发帖、撤回、浏览量回采、24h 结算与 7d 尾款释放。
- **涉及文件（新建 1 个调度模块 + 修改 1 个老文件）**：
  - `src/chabo/webapi/scheduler.py`（新建，`< 150` 行）
  - [src/chabo/webapi/main.py](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/main.py)
- **精准改造指令**：
  1. 新建 `src/chabo/webapi/scheduler.py`，使用 `asyncio.create_task` 在 FastAPI `lifespan` 启动时（可通过环境变量 `CHABO_ENABLE_SCHEDULER=1` 控制，单测时默认关闭）定期轮询执行：`dispatch_due`（每 30 秒）、`unpublish_due`（每 60 秒）、`sync_delivery_metrics`（每 10 分钟）、`confirm_due_earnings`（每 15 分钟）、`release_due_holdbacks`（每 1 小时）。
- **10 秒冒烟与验收命令（同时执行 Gate G2 沙盒十步验收）**：
  ```bash
  python3 -m unittest discover -s tests
  ```

---

### 📦 第三批次：阶段 2 可信结案报告、可解释评分与一键复投（Slices S10～S12）

#### 切片 S10：修复报表 SQL 翻倍 Bug、拆除广告主付费墙与免登录战报分享 API（修复 `DEF-12` + `G-05`）
- **目标**：让广告主看到 100% 精准的财务与转化报表，并支持生成免登录分享链接（`/public/reports/:share_token`）实现社交裂变。
- **涉及文件（仅 2 个老文件）**：
  - [src/chabo/services/billing.py](file:///Users/lanjinglive618/ChaBo/src/chabo/services/billing.py)
  - [src/chabo/webapi/advertiser.py](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/advertiser.py)
- **精准改造指令**：
  1. 重写 [`AdvertiserService.report`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/billing.py#L946-L978) 中的分频道聚合 SQL：先对 `deliveries` 按 `order_id` 子查询汇总 `SUM(impression_count)` 与 `SUM(click_count)`，再与 `ad_orders` 关联计算 `SUM(o.spent_cents)` 和 `COUNT(o.id)`，彻底消除 `LEFT JOIN` 一对多膨胀；同时删除 `if entitlements["full_report"] else []` 限制，向全体广告主返回 `by_channel` 明细与 `1h/6h/24h/48h` 曝光曲线。
  2. 新增签名分享令牌接口 `POST /api/advertiser/reports/share-token` 与公开只读查询接口 `GET /api/public/reports/{share_token}`（自动脱敏账户余额，保留频道表现、CPM/CPC 与平台验真水印）。
- **10 秒冒烟与验收命令**：
  ```bash
  python3 -m unittest discover -s tests -p "test_*report*.py"
  ```

#### 切片 S11：贝叶斯平滑评分、样本置信度标签与四象限复投建议引擎（修复 100 分膨胀 + `G-06`）
- **目标**：修正全员 100 分 S 级的失真算法，输出带置信度标签（如 `[🌱 新晋测品]` vs `[💎 20单验证]`）的 S/A/B/C/D 评级，并在结案时产出可解释的四象限优化动作与一键复投草稿接口。
- **涉及文件（仅 2 个老文件）**：
  - [src/chabo/services/channel.py](file:///Users/lanjinglive618/ChaBo/src/chabo/services/channel.py)
  - [src/chabo/services/billing.py](file:///Users/lanjinglive618/ChaBo/src/chabo/services/billing.py)
- **精准改造指令**：
  1. 在 [`PricingService.assess_channel`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/channel.py#L1102-L1247) 中引入样本置信度 $C = 1 - \exp(-n_{\text{orders}}/5)$ 与贝叶斯先验平滑 CTR，将得分拉开至真实正态分布（S $\ge 85$，A $70\sim 84$，B $55\sim 69$，C $40\sim 54$，D $<40$），并在返回结构中附带 `confidence_level`（`low / medium / high`）与 `verified_orders_count`。
  2. 在 [`RecommendationService.generate_for_advertiser`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/billing.py#L720-L810) 中，基于真实 `views` 达成率与有效 `CTR` 中位数对比，输出四象限建议（`scale_up` 加量复投 / `replace_creative` 保留频道换素材 / `reduce_frequency` 降频 / `pause_channel` 淘汰并推荐同分类高分替补频道），并提供 `POST /api/advertiser/campaigns/{id}/rebook` 一键生成复投草稿单。
- **10 秒冒烟与验收命令**：
  ```bash
  python3 -m unittest discover -s tests -p "test_*pricing*.py"
  ```

#### 切片 S12：前端报告页、订单进度时间轴与管理台人工对账视图闭环（实现 `G-05`, `G-06` 前端）
- **目标**：消除前端占位符（Stub），让广告主在 Web 端直观查看结案报表、复制免登录战报链接、点击一键复投，并让管理员能处理 `reconciliation_required` 异常单。
- **涉及文件（仅 3 个前端文件）**：
  - [web/src/routes/advertiser/AdvertiserReportPage.tsx](file:///Users/lanjinglive618/ChaBo/web/src/routes/advertiser/AdvertiserReportPage.tsx)
  - [web/src/routes/advertiser/AdvertiserOrdersPage.tsx](file:///Users/lanjinglive618/ChaBo/web/src/routes/advertiser/AdvertiserOrdersPage.tsx)
  - [web/src/routes/admin/AdminDeliveriesPage.tsx](file:///Users/lanjinglive618/ChaBo/web/src/routes/admin/AdminDeliveriesPage.tsx)
- **精准改造指令**：
  1. 在 [`AdvertiserReportPage.tsx`](file:///Users/lanjinglive618/ChaBo/web/src/routes/advertiser/AdvertiserReportPage.tsx) 对接真实 `/api/advertiser/report` 数据，展示总花费、真实总曝光、防刷有效点击、平均 CPM/CPC、分频道四象限建议卡片、`[🔗 复制验真战报链接]` 与 `[🚀 一键采纳并生成复投单]` 按钮。
  2. 在 [`AdvertiserOrdersPage.tsx`](file:///Users/lanjinglive618/ChaBo/web/src/routes/advertiser/AdvertiserOrdersPage.tsx) 展示每笔订单的履约状态轴（`待审核 -> 已排期 -> 发送中 -> 已发送(含 1h/6h/24h 浏览量) -> 已结算`）及补齐取消订单入口。
  3. 在 [`AdminDeliveriesPage.tsx`](file:///Users/lanjinglive618/ChaBo/web/src/routes/admin/AdminDeliveriesPage.tsx) 增加 `reconciliation_required` 筛选与 `[确认已发帖入账]` / `[确认未发帖退回冻结]` 人工核销按钮。
- **10 秒冒烟与验收命令（同时执行 Gate G3 总验收）**：
  ```bash
  cd web && npm run build && cd .. && python3 -m unittest discover -s tests
  ```

---

## 五、 可直接复制给 AI 执行的开工指令（Prompt 模板）

当您准备启动代码改造时，只需在当前对话或 Claude Code 中发送以下任意一句指令，AI 就会严格按 [CLAUDE.md](file:///Users/lanjinglive618/ChaBo/CLAUDE.md) 规范逐个切片执行并汇报：

> **开工指令示例（可直接复制发送）**：
> “我已确认《插播 Ad Lab V1 最终成熟方案与 AI 施工蓝图》的全部 5 项拍板决定。现在请严格遵循 `CLAUDE.md` 与该蓝图中的**第一批次（阶段 0 止血：切片 S01～S05）**开始执行。请先从 **切片 S01（SQLite 连接安全关闭与 WAL 锁超时统一）** 开始，每完成一个切片都运行对应的 unittest 冒烟测试，测试全绿后向我汇报并继续下一个切片。”
