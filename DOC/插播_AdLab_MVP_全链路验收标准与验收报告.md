# 插播 Ad Lab MVP（V1 蓝图 S01～S12）全链路验收标准与实测验收报告

- **验收日期**：2026-11-05
- **验收范围**：阶段 0 止血与架构加固（`S01`～`S05`）、阶段 1 MVP 投放闭环与双轨归因（`S06`～`S09`）、阶段 2 透明战报、AI 推荐与三端 Web 驾驶舱（`S10`～`S12`）
- **自动化单元测试门禁**：`205 / 205 PASS`（`Ran 205 tests in 9.404s — OK`）
- **前端生产构建门禁**：`tsc -b && vite build` `0 报错 / 0 警告`
- **端到端 UAT 验收结果**：**12 项核心业务与安全标准 100% 通过（PASS）**

---

## 一、MVP 验收标准体系（4 大门禁、12 项硬指标）

### 门禁一（Gate G1）：资金账本安全、权限边界与并发韧性
| 编号 | 验收指标 | 合格判定标准（Pass Criteria） | 实测结果 | 实测证据摘要 |
| :--- | :--- | :--- | :--- | :--- |
| **G1-01** | **健康检查与基础网关连通** | `GET /health` 返回 `200 OK` 且 `{"ok": true}`；`POST /telegram/webhook` 正常挂载 | **PASS** | `status=200, ok=True`（[`main.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/main.py#L186-L202)） |
| **G1-02** | **充值负数拦截与双人复核防并发双花** | `amount_cents <= 0` 抛 `InvalidState`；同一笔 `topup_request` 并发或重复调用 `approve_topup` 仅入账 1 次 | **PASS** | 负数拒绝；二次审批拦截；`available=500000`（[`wallet.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/wallet.py#L755-L760)、[`test_ledger_security.py`](file:///Users/lanjinglive618/ChaBo/tests/test_ledger_security.py#L34-L89)） |
| **G1-03** | **非会员关闭推广尾巴 5% 服务费执行** | 未订阅高级版的频道主允许关闭 `platform_promo_enabled`，且频道服务费按 `500 bps`（5%）执行 | **PASS** | `service_fee_bps=500`（[`channel.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/channel.py#L323-L351)） |
| **G1-04** | **管理员代看（Impersonation）硬性只读拦截** | 管理员进入广告主/频道主代看会话后，任何 `POST/PUT/PATCH/DELETE` 写操作一律返回 `403 impersonation_read_only` | **PASS** | `status=403, detail=impersonation_read_only`（[`deps.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/deps.py#L14-L39)） |
| **G1-05** | **活跃订单期间防改绑串账与快照隔离** | 频道存在未完结订单时，第三方调用 `bind_channel` 不会篡改 `owner_account_id`；`ad_orders.publisher_account_id` 永久快照原频道主 | **PASS** | 抢绑后 `owner_account_id` 仍保持原频道主不变（[`channel.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/channel.py#L76-L95)） |
| **G1-06** | **24h 收益确认与 7 天质押尾款自动释放** | 发送满 24h 后 `pending_earnings_cents` 转入 `confirmed_earnings_cents`；满 7 天无争议自动转入 `releasable_earnings_cents` | **PASS** | 1000 分刊例价扣 5% 平台费后，`releasable_cents=950` 准时释放（[`fulfillment.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py#L84-L115)） |

---

### 门禁二（Gate G2）：多频道排期引擎、三段式履约与双轨防刷归因
| 编号 | 验收指标 | 合格判定标准（Pass Criteria） | 实测结果 | 实测证据摘要 |
| :--- | :--- | :--- | :--- | :--- |
| **G2-01** | **多频道计划单事务原子提交与同 Campaign 聚合** | 计划提交在单事务中完成（任一频道超日上限或余额不足则整单回滚），且拆出的多个子订单共享同一个 `campaign_id` | **PASS** | 2 个频道生成 2 笔订单，共享 `campaigns=1`（[`plans.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/plans.py#L194-L303)） |
| **G2-02** | **三段式隔离派发与高频间隔排期** | 外部 Telegram 发帖与 SQLite 账本事务完全解耦；`frequency_per_day=2` 时首场发帖成功后自动排期 12 小时后的下一场 | **PASS** | `deliveries=['sent', 'scheduled']`，下一场间隔 12h（[`fulfillment.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/fulfillment.py#L325-L505)、[`order.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/order.py#L1401-L1415)） |
| **G2-03** | **`/r/{token}` 四层防刷短链与去重点击归因** | 自动过滤 `TelegramBot` 预加载爬虫与 5 分钟内同 IP 重复点击（均正常 302 跳转但不计费/不计点击），真实用户点击准确累加 `click_count` | **PASS** | 爬虫 1 次 + 真人连点 2 次 → `click_count=1`（[`redirect.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/webapi/redirect.py#L37-L149)） |

---

### 门禁三（Gate G3）：透明效果战报、AI 五维评分与三端驾驶舱操作闭环
| 编号 | 验收指标 | 合格判定标准（Pass Criteria） | 实测结果 | 实测证据摘要 |
| :--- | :--- | :--- | :--- | :--- |
| **G3-01** | **频道五维评分等级与样本置信度徽章** | 频道评估自动输出 `S/A/B/C/D` 等级与三级置信度徽章（`🌱 新晋测品 / 🛡️ 实单验证 / 💎 高置信验证`），Web 选号表直接可见 | **PASS** | `grade=S, badge=💎 高置信验证`（[`channel.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/channel.py#L1217-L1278)） |
| **G3-02** | **战报多场次零虚增、曝光衰减曲线与 7 天免登录分享** | 1 笔订单包含多场 `deliveries` 时，`by_channel.orders_count` 绝不翻倍；免费返回 `1h/6h/24h/48h` 曝光曲线；公开分享链接隐去余额并带验真水印 | **PASS** | `orders_count=1, views=4500, clicks=1`，分享页含双轨验真水印（[`billing.py`](file:///Users/lanjinglive618/ChaBo/src/chabo/services/billing.py#L948-L1086)） |
| **G3-03** | **四象限复投建议、一键复投、自助停单退费与异常对账** | 支持投后四象限建议与 `POST /api/advertiser/rebook` 一键复投；广告主调用 `POST /api/advertiser/orders/{id}/pause` 立即停止并退回冻结余额；管理端支持 `POST /api/admin/deliveries/{id}/reconcile` 人工对账 | **PASS** | 一键复投成功；停止后 `status='paused', reserved_cents=0`；对账接口验证通过（[`ReportPanel.tsx`](file:///Users/lanjinglive618/ChaBo/web/src/routes/advertiser/ReportPanel.tsx)、[`OrdersPanel.tsx`](file:///Users/lanjinglive618/ChaBo/web/src/routes/advertiser/OrdersPanel.tsx)、[`AdminDashboard.tsx`](file:///Users/lanjinglive618/ChaBo/web/src/routes/admin/AdminDashboard.tsx)） |

---

## 二、人工浏览器巡检 3 分钟清单（随时可复核）

当你在本地启动前后端服务后，可在浏览器按以下 4 步完成人工肉眼巡检：

1. **登录与门户切换（`http://localhost:5173/login`）**
   - 在开发快捷登录卡片输入任意 Telegram ID，勾选 `admin / advertiser / publisher`，点击 **创建测试会话**。
   - 顶部导航栏可随时一键切换 **管理端 / 广告主端 / 频道主端**。
2. **频道主端（`/publisher`）**
   - 查看已绑定频道、调整三档报价或每日广告条数上限、查看待确认收益与可提现收益。
3. **广告主端（`/advertiser`）**
   - **频道市场**：查看频道 `S/A/B/C/D` 评分等级与 `💎 高置信验证 / 🛡️ 实单验证 / 🌱 新晋测品` 标签，勾选频道生成投放计划。
   - **订单**：查看 `1/4 审核中 → 2/4 已排期 → 3/4 投放采集中 → 4/4 已结算完结` 进度，点击 **一键复投** 或 **停止并退回余额**。
   - **效果战报**：查看总花费、真实采样浏览、去重有效点击、CPM/CPC、多时间点曝光曲线、AI 四象限复投建议，点击 **生成只读分享战报链接**。
4. **管理端（`/admin`）**
   - 顶部点击 **一键执行到期派发与收益结算** 触发调度；在 **订单 / 待审入账 / 投放记录 / 争议 / 审计** 标签页执行审批、对账与链路哈希校验。
