# 02｜Telegram 数据能力与归因可行性核查（任务 B & 调研项 2、5）

> **核查日期**：2026-11-05  
> **研究目标**：从 Telegram 官方 API 文档、MTProto 协议规范及开发者条款出发，逐项核实浏览、点击、Bot 启动、入群、预加载、反作弊与 AI 条款边界；并**重点审计 R2 报告中“站内探针可 100% 识别真人”等关键断言的真伪**。

---

## 一、 R1 / R2 关键断言审计：站内探针真的能“100% 识别真人”吗？

### 【重点纠偏结论：不完全成立，R2 存在把“真实 TG 账号会话”等同于“人类生物真人”的逻辑漏洞】

* **R2 原文说法**：“点击广告进入插播详情页/福利页的真实 Telegram 独立用户数……100% 真人触发、零爬虫预加载污染，是评估频道含金量的黄金指标。”
* **事实核查与技术真相**：
  1. **做得到的部分（事实）**：当频道广告按钮使用 `https://t.me/<bot>?start=ad_<id>` 时，Telegram 的链接预览爬虫（`TelegramBot`）**绝对不会**触发向 Bot 发送 `/start` 消息；普通网页爬虫也无法通过 HTTP GET 触发 `/start`。只有拥有合法 Telegram 登录态（Session）的客户端点击按钮并按下“开始（Start）”时，Bot 才会收到带有真实 `from.id`（`telegram_user_id`）的 `Message` 更新。因此，它确实能 **100% 免疫 HTTP 预加载噪声，且能按 `telegram_user_id` 精确去重**。
  2. **做不到的部分（致命盲区）**：**真实的 `telegram_user_id` $\neq$ 真实的人类买家！**
     - 在 Telegram 灰黑产中，频道主或刷量工作室可以使用廉价注册的接码小号（Userbot 僵尸号矩阵），通过 Telethon / Pyrogram 调用 MTProto 方法 `messages.startBot(bot=..., peer=..., start_param='ad_xxx')`，**几行 Python 脚本就能用 500 个真实存在的 Telegram 僵尸账号自动触发 `/start` 事件**！
     - 此外，由于用户点击 `t.me/bot?start=xxx` 后，首次进入该 Bot 时还必须在底部再点一次「Start / 开始」按钮才会发出消息，这会产生 **30%～60% 的正常人类操作流失**（即：点开了 Bot 窗口看了一眼但没按 Start 的真用户，系统完全测不到）。
* **修正后的产品与防刷规范**：
  1. **严禁对外宣称“100% 真人”**，标准口径必须定为：**「站内实名账号去重互动数（Verified TG Account Starts）」**。
  2. **必须叠加 4 维反 Userbot 机刷校验**（仅通过校验的才计入有效探针点击）：
     - **信号 1（账号属性）**：检查 `from.is_bot == False`、是否存在 `username` / 头像 / `is_premium`（批量脚本新号往往无用户名、无头像、无 Premium、语言代码异常单调）。
     - **信号 2（时间分布熵）**：正常人类点击呈泊松分布且随阅读曲线自然衰减；脚本刷量往往在几分钟内以固定间隔或高并发集中爆发。
     - **信号 3（二跳行为率）**：进入 Bot 详情页后，是否继续点击了详情页内的目标外链（`/r/{token}`）？如果某频道有 200 个账号触发了 `/start`，但详情页里的目标外链点击为 0，可直接判定为“纯脚本刷 `/start`”。
     - **信号 4（跨频道黑名单库）**：若同一批 `telegram_user_id` 在多个不同品类的频道里高频点击所有广告探针，标记为“职业羊毛党/刷量农场账号”并予以剔除。

---

## 二、 Telegram 官方 API 数据获取能力逐项核查表

| 数据项 | 官方接口路径 | 调用身份与权限门槛 | 官方依据链接（2026-11-05 核查） | 可信等级与局限性 | 必须进行的真机实验（M0 Spike） |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. 频道订阅人数及变化** | Bot API: `getChatMemberCount`<br/>Bot API: `getChat` | Bot 只需能访问该公开频道或已在私有频道内 | https://core.telegram.org/bots/api#getchatmembercount | **V 级（计数）**：准实时总人数；但无法区分“今天新增 100 人同时退订 100 人”还是“0 变动”，也不能区分活人与死粉 | 定时（每 6h）快照拉取，验证被踢出频道时的错误码（`403 Forbidden`） |
| **2. 单帖浏览计数（Views）与转发计数（Forwards）** | **注意**：标准 HTTP Bot API 的 `Message` 文档**未公开保证**返回 `views` 字段；必须通过：<br/>① MTProto（Bot 身份或 User 身份）调用 `channels.getMessages` 读取 `message.views` / `message.forwards`<br/>② MTProto（仅限 User 身份）调用 `messages.getMessagesViews(increment=false)` | `channels.getMessages`：官方标注 **Both users and bots can use**；<br/>`messages.getMessagesViews`：官方标注 **Only users can use** | https://core.telegram.org/constructor/message<br/>https://core.telegram.org/method/channels.getMessages<br/>https://core.telegram.org/method/messages.getMessagesViews | **V 级（计数）**：代表帖子被滑过视口的累计次数（含同一用户跨天重复看及刷量脚本），**绝不等于独立覆盖人数（Reach）**。调用 `getMessagesViews` 时若误设 `increment=true` 会导致平台自己把浏览量刷高！ | **SP-01**：验证 `TelegramABC` 现有 MTProto 适配器用 Bot Token 与 User Session 分别调用 `channels.getMessages` 读取 `views` 的稳定性与限流阈值 |
| **3. 频道高级统计与单帖小时曲线** | MTProto: `stats.getBroadcastStats`<br/>MTProto: `stats.getMessageStats` | **极严苛**：官方明确标注 **Only users can use（Bot 禁用）**；且调用账号必须是该频道具备 `can_view_stats` 权限的管理员，频道订阅数需达官方阈值（通常 $\ge 50\sim500$ 人），且请求必须路由至 `channelFull.stats_dc` 指定的数据中心 | https://core.telegram.org/api/stats<br/>https://core.telegram.org/method/stats.getBroadcastStats<br/>https://core.telegram.org/method/stats.getMessageStats | **V 级（官方统计）**：能拿到真实语言分布、来源占比、小时级浏览曲线；但**绝对不能要求外部第三方频道主把自己的个人 Telegram 登录 Session 交给我们托管**（安全与合规风险极高） | 仅在**自有频道**（由 `TelegramABC` 受控 Userbot 担任管理员）测试拉取；第三方频道一律采用“平台每小时定时采样 `message.views` 做差分”来绘制小时曲线 |
| **4. 帖子表情反应（Reactions）** | Bot API: `message_reaction` / `message_reaction_count` 更新；或 MTProto `message.reactions` | Bot 必须是频道管理员，且在 `getUpdates` / `setWebhook` 的 `allowed_updates` 中显式声明订阅 | https://core.telegram.org/bots/api#messagereactioncountupdated | **V 级**：官方说明公开频道的 `message_reaction_count` 会**延迟数分钟并合并批量推送**；且禁止拉取频道帖具体是谁点了赞（会报 `BROADCAST_FORBIDDEN`） | 验证 `allowed_updates` 开启后对现有 `polling.py` 吞吐量的影响 |
| **5. Bot 深链与 `/start` 事件** | `https://t.me/<bot>?start=<payload>` $\rightarrow$ Bot 接收 `/start <payload>` 消息 | Payload 仅限 `A-Z, a-z, 0-9, _, -`，**最大长度 64 字符**；必须用户主动点击底部 Start 按钮才触发 | https://core.telegram.org/bots/features#deep-linking | **V 级（交互）**：能拿到 `from.id`、`language_code`、`is_premium`；但存在“点开窗口未按 Start”的漏斗损耗，且需防 Userbot 脚本群控刷量 | **SP-04**：实测新用户 vs 已用过該 Bot 的老用户点击深链时的界面差异（老用户是否仍需手动点 Start 按钮） |
| **6. 专属邀请链接与入群事件** | Bot API: `createChatInviteLink`<br/>Update: `chat_member`（读取 `invite_link` 字段） | Bot 必须是**目标被推广频道/群**的管理员，且拥有 `can_invite_users` 权限 | https://core.telegram.org/bots/api#createchatinvitelink<br/>https://core.telegram.org/bots/api#chatmemberupdated | **V 级（入群转化）**：能精确捕获谁通过哪条广告专属链接进群、随后是否退群（`left`）；但若用户看完广告后自己搜索公开 `@username` 进群，则无法归因到该链接 | **SP-05**：在测试频道实测 `chat_member` 更新在千人级频道中的推送完整性，并计算“24h 留存净入群数” |
| **7. 普通频道帖外链点击** | **官方无任何接口**；必须通过平台自有 HTTPS 短链网关 `GET /r/{token}` 302/307 跳转捕获 | 需自有备案/可信 HTTPS 域名，严禁开放重定向漏洞（Open Redirect） | 无官方点击回调 API（行业共识） | **V 级（HTTP 请求）**：只能证明某 IP/UA 请求了短链服务器，不能 100% 证明是人类点击，也无法获取点击者的 Telegram `user_id` | **SP-02 & SP-03**：测试 iOS / Android / Desktop 客户端外链弹窗表现与预加载请求特征 |
| **8. 跨频道受众重叠度（Audience Overlap）** | **官方无通用接口**（出于隐私保护，无法导出两个公开频道的全部订阅者 ID 列表做交集） | 即使管理员也只能拉取有限的 Recent/Search 成员（大频道限制返回上限），无法全量枚举 | https://core.telegram.org/method/channels.getParticipants | **不可实测全量订阅交集**；仅能通过**「站内探针点击用户 ID 交集」**做抽样推断（例如 A 频道和 B 频道点过探针的活跃用户中有 18% 重合） | 在报告中明确声明“无法测量全量潜伏订阅者重叠，仅提供活跃点击人群重合度参考” |

---

## 三、 三种点击追踪方案深度对比与防刷架构（调研项 5）

### 1. 三种跳转追踪方案优劣势全面对比

| 维度 | 方案 A：直接放广告主原始链接 | 方案 B：Bot 深链二跳（`t.me/bot?start=ad_x` $\rightarrow$ 详情页 $\rightarrow$ 外链） | 方案 C：HTTPS 短链 302 跳转（`https://域名/r/{token}`） |
| :--- | :--- | :--- | :--- |
| **用户体验与流失率** | **最优（0 额外摩擦）**：跳 TG 内部零弹窗，跳外部网页仅 1 次系统默认提示 | **中等（多 1～2 步点击）**：在 TG 内 0 安全警告弹窗，适合发放优惠码/长图文种草；但急性子用户可能在按 Start 前流失 | **跳外部网站时体验好，跳 `t.me` 时体验极差**：若用 HTTP 短链跳回 `t.me/xxx`，移动端会先弹外链警告、拉起内置浏览器再二次唤起 TG，流失率高达 30%～50%！ |
| **能拿到的数据粒度** | **0 数据（完全盲盒）**：平台完全不知道有没有人点 | **最丰富**：可拿到点击者 `telegram_user_id`、`is_premium`、语言代码 + 二跳外链点击，形成双层漏斗 | **中等**：能拿到请求时间、IP 归属地（国家/ASN）、设备类型（iOS/Android/Desktop）、Referer；**拿不到 `telegram_user_id`** |
| **机器预加载污染** | 无 | **0 预加载污染**（预览爬虫不会发 `/start`） | **极高污染**：发帖瞬间、消息被转发瞬间、客户端开启链接预览时，`TelegramBot (like TwitterBot)` 及安全扫描器会自动请求短链 |
| **防刷量难度** | 无法防刷 | 中高（可按 TG 账号画像、注册年限特征、二跳率交叉验真） | 中（无账号身份，若黑产使用住宅代理 IP 池模拟手机 UA 请求，单靠短链很难 100% 识别） |
| **插播 V1 最佳适用场景** | 仅当大广告主强烈拒绝任何跳转追踪、或已有自建追踪时作为对照组 | **1. 标准插播的 `[🔍 查看详情]` 副按钮**<br/>**2. 需要领券/看长文的重度种草广告**<br/>**3. 频道日常活人含金量探针** | **1. 广告主目的地是外部独立网站 / App 下载页时的 `[主 CTA]` 按钮**（**严禁**用于目的地本身就是 `t.me/...` 的主按钮！） |

> **【极其重要的产品架构铁律】**：  
> 当广告主的主按钮目的地是 **Telegram 内部地址（`t.me/频道`、`t.me/机器人`）** 时，主按钮**绝对禁止**套用方案 C（HTTPS `/r/{token}` 302 短链）！必须优先使用 **专属邀请链接（`createChatInviteLink`）** 或 **合作 Bot Start 参数**，或者走方案 B（站内二跳），否则会被客户端外链警告和浏览器二次唤起彻底毁掉转化率！

### 2. HTTP 短链（`/r/{token}`）四层反作弊清洗管线

当主按钮指向外部网页并使用 `/r/{token}` 时，每一条 HTTP 请求必须经过以下四层实时分类，分别存入 `click_requests`（原始全量）与 `click_events`（脱水有效）：

1. **第一层：官方预加载与已知爬虫剥离（Bot / Prefetch Filter）**
   - 拦截 User-Agent 包含 `TelegramBot (like TwitterBot)`、`Twitterbot`、`facebookexternalhit`、`Slackbot`、`WhatsApp`、`Discordbot`、`curl`、`python-requests`、`Go-http-client`、`HeadlessChrome` 等特征的请求，标记 `is_bot = 1, reject_reason = 'ua_bot_or_prefetch'`。
   - 拦截 HTTP `HEAD` 请求及发帖成功（`sent_at`）后 **前 5 秒内** 来自数据中心 IP 的自动探针请求，标记 `reject_reason = 'instant_prefetch'`。
2. **第二层：机房 IP（ASN）与异常网络剥离（Datacenter Filter）**
   - 识别 AWS、Google Cloud、DigitalOcean、Hetzner、OVH、阿里云、腾讯云等纯服务器机房 ASN。对于普通消费品广告，机房 IP 的密集请求标记为 `suspect_datacenter`（注意：考虑到中文用户常使用代理节点，不能一刀切把所有代理 IP 标为作弊，而是对**同一 IP 段在 60 秒内连续触发 >3 次**的请求标为 `rate_limited_burst`）。
3. **第三层：短期指纹去重（Deduplication Window）**
   - 在不长期保存用户隐私的前提下，计算短期不可逆摘要：`visitor_hash = HMAC_SHA256(daily_salt, IP + UserAgent + AcceptLanguage)`。
   - 同一 `delivery_id + cta_id + visitor_hash` 在 **24 小时窗口内** 的多次点击：第 1 次计入 `filtered_clicks` 和 `unique_click_estimate`，第 2 次及以后仅计入 `raw_redirect_hits`，标记 `reject_reason = 'duplicate_within_24h'`。
4. **第四层：分布异常告警（Anomaly Detection）**
   - 若某频道单帖的 `filtered_clicks / message_views_24h`（代理 CTR）异常高于 **15%**，或 80% 的点击集中在夜间非活跃时段的短短 3 分钟内，系统自动触发风控黄灯（`anomaly_flag = 'click_spike_suspect'`），提示运营人工复核。

---

## 四、 Telegram 数据保存与 AI / 大模型使用合规死线（调研项 2 & 10）

### 1. 官方条款原文核查（2026-11-05）
* **Telegram API Terms of Service Section 1.5**（https://core.telegram.org/api/terms）：
  - 明确规定开发者使用 Telegram API 受《Telegram 内容许可与 AI 抓取服务条款》约束，**严禁使用、访问或汇总从 Telegram 平台获取的数据来训练（train）、微调（fine-tune）或以其他方式开发、增强或部署人工智能、机器学习模型及类似技术**。
* **Telegram Bot Platform Developer Terms Section 4.3 & 4.4**（https://telegram.org/tos/bot-developers）：
  - **4.3 最小化采集与反抓取**：Bot 只能采集提供服务所必需的最少数据；**严禁利用 Bot 抓取（scrape）公开群组或频道内容以构建大型数据集或用于 AI 产品**。若用户自愿提供数据用于额外用途，必须获得其知情且可随时撤回的明确同意。
  - **4.4 数据安全与删除**：必须对存储的数据采取严格加密与访问控制；当用户撤回同意、删除与 Bot 的对话或请求删除数据时，必须及时销毁相关个人数据。

### 2. 对插播（ChaBo）架构的硬性约束（不可逾越）
1. **绝对禁止**：编写爬虫批量抓取未授权公开频道的历史帖子文本扔给 OpenAI / Claude / 本地大模型去做“全网频道自动分类画像”或微调模型。
2. **明确允许（安全区）**：
   - 广告主在我们的**独立网页后台（Web Portal）**自己手动输入或上传的产品介绍、推广目标、预算要求，属于**平台第一方商业表单数据**（非 Telegram API 抓取数据），经广告主同意后，完全可以合法调用 LLM 协助润色广告文案或生成投放计划说明。
   - 平台自身的财务账本、订单价格、脱敏后的聚合统计比值（如某广告活动的花费 \$100、有效点击 500 次、CPC \$0.20），用**传统确定性数学公式和统计规则（非 AI/ML 训练）**计算频道评分与预算分配建议，100% 合规且可解释。
3. **日志留存期（Retention Policy）**：
   - 短链访问的原始 IP 与 UA 仅允许在内存中计算短期 `visitor_hash` 或最多保留 7 天用于防刷排障，到期自动清零，只保留不含个人身份的聚合计数。
