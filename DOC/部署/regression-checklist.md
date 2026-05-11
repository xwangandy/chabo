# 一期上线前回归清单

每次正式上线前，按这张清单跑一遍。前两节是命令式自检（机器跑），后面是必须人工在真实测试 bot + 测试频道上验证的金线路径。

自检失败任一条都不能上线；金线路径任一步阻塞也必须先解决再继续。

## A. 自动化自检（在生产机上跑）

推荐先跑网页端一键验收，它会串起 Python 回归、前端构建、preflight 和 `/api/health`：

```bash
cd /opt/chabo
chabo verify-web --profile production \
  --host <你的对外 host> \
  --health-url https://<你的对外 host>/api/health \
  --audit-chain
```

如果是在生产机执行发布，可以直接跑部署脚本；它会先备份 SQLite，再做 `verify-web --profile production --audit-chain --skip-health`，重启 `chabo-api` 后再做 `verify-web --profile production --audit-chain --skip-tests --skip-build`：

```bash
cd /opt/chabo
./scripts/deploy-web-production.sh <你的对外 host> https://<你的对外 host>/api/health
```

GitHub Actions 手动触发 `workflow_dispatch` 并填写 `production_host` 时，也会跑同一条 production profile gate，适合在真正发布前做一次远端 health + 构建验证。

如果要分步骤排查，再按下面的拆分项逐个执行。

### 1. 服务层回归测试
```bash
cd /opt/chabo
python3 -m unittest discover -s tests
```
所有测试必须通过。最少要包含 `test_phase_one_golden_path_end_to_end` 这条端到端测试。

### 2. 生产配置预检
```bash
# 用真实 .env 跑；non-zero exit 说明阻塞
chabo preflight --host <你的对外 host>
```
检查：
- DB 可连、schema 已迁移
- `<db_dir>/backups/` 可写
- `CHABO_ADMIN_TOKEN` / `CHABO_WEBHOOK_SECRET` 长度 ≥16 且不含弱关键字
- `CHABO_BOT_TOKEN` / `CHABO_BOT_USERNAME` 同时设置
- 没有遗留待审、到期未发、open 争议堆积

### 3. 健康检查
```bash
chabo run-web --host 127.0.0.1 --port 8080 &
sleep 2
curl -s http://127.0.0.1:8080/health | jq .
kill %1
```
必须 `ok=true`，`db=ok`，`ops` 包含全部 6 个字段。

### 4. 备份命令
```bash
chabo backup-db --target /tmp/preflight-snapshot.sqlite3
sqlite3 /tmp/preflight-snapshot.sqlite3 ".tables"
rm /tmp/preflight-snapshot.sqlite3
```
能写 + 能读，至少看到 `accounts / channels / ad_orders / creatives / topup_requests / tool_call_logs` 等核心表。

### 5. 审计链完整性
```bash
chabo verify-audit-chain
```
必须 `ok=true`，`invalid_hashes=0`，`broken_links=0`。旧库里允许存在 M7 前的未签名审计行；新上线后若要强制所有新行均签名，可按时间窗配合 `--created-from` 检查。

```bash
chabo verify-audit-chain --created-from "2026-05-02 00:00:00" --strict
```

管理端审计页也要抽查一次：选择上线时间窗，勾选“严格”，点击“校验链路”，确认报告里的 `Hash 异常` 与 `断链` 均为 0。

### 6. 回滚 dry-run
```bash
./scripts/rollback-web-production.sh --dry-run /var/backups/chabo/chabo-YYYYMMDD-HHMMSS.sqlite3 chabo.example https://chabo.example/api/health
```
确认输出包含：备份当前 DB、停止服务、恢复指定 SQLite、启动服务、production verify-web gate。dry-run 不应真正覆盖 DB。

### 7. 恢复演练和审计留档
```bash
./scripts/rehearse-sqlite-restore.sh /var/backups/chabo/chabo-YYYYMMDD-HHMMSS.sqlite3
./scripts/export-audit-integrity-report.sh --created-from "2026-05-02 00:00:00" --strict
```
恢复演练必须只操作临时 SQLite 副本；审计留档必须同时生成 `.json` 和 `.sha256`。

### 8. 管理端运营入口
1. `/admin#wallet`：总余额、冻结预算、最近流水、资金账户榜正常展示。
2. `/admin#settings`：发布准入、审计记录、数据库路径、最近备份正常展示。

## B. 真实 Bot 金线路径（手工）

用一个**测试 bot** + **测试频道**做。**绝对不要在没跑过 A 节自检的环境上做这一节。**

### 1. 频道接入
1. 把测试 bot 加为测试频道管理员，授予「发消息 + 编辑消息 + 置顶」三个权限。
2. 在 Bot 私聊里 `/start`：
   - 时区确认页正常出现
   - 选时区后进入工作台首页
3. 频道接入向导：从「📺 频道管理」 → 「🔌 手动接入」，按提示从频道转发任一条消息给 Bot。
   - 应收到「✅ 频道已接入」通知，附权限三项 + 入口链接。

### 2. 频道详情配置
1. 频道管理 → 点击频道。
2. 状态卡 5 字段都正常显示：状态 / 权限 / 今日广告 / 可投形态 / 当前档位 / 待确认收益。
3. 8 格按钮渲染正常。
4. 进「💵 价格档位」，把 standard_card 改成「高档」，再切回「中档」。
5. 进「⏱ 频控时间」，把每日上限改成 5。
6. 返回详情页确认「当前档位 = 中档」「今日广告：0 / 5」。

### 3. 投放配置器
1. 在测试频道的任意带平台按钮的帖子上点击「在本频道插播广告」。
2. 进 Bot 后第一屏必须是「投放配置器」，不是工作台。
3. 「展示设置」：选「标准插播」。
4. 「广告素材」：选「➕ 新建标准插播素材」，按提示输入文案 + 链接。
5. 「费用确认」：金额合理，按「✅ 确认投放」。
6. 余额不足时应直接出现「💳 立即充值」按钮（点击后进入 Stars 充值 picker）。

### 4. 运营审核（双窗口）
1. 用浏览器打开 `https://<你的域名>/admin?token=<admin_token>`。
2. 顶部摘要看到「待审核订单 ≥ 1」红色告警。
3. 在「插播订单」表里点击「通过」，备注框填「人工核对通过」。
4. 详情页点开，时间线含 `order_approved` + 备注「人工核对通过」。

### 5. 发布到频道
```bash
chabo dispatch-due --limit 5
```
1. 测试频道里出现广告帖。
2. 帖子按钮：第一行「📣 频道招商 / 🔍 查看详情」，第二行 CTA 文字按广告主自定义。
3. 点击「🔍 查看详情」进 Bot：详情页含完整文案 + 来源频道 + CTA 按钮 + 「📣 我也想在这个频道投广告」。

### 6. 退款 + 备注
1. 在 `/admin` 投放表里点「退款」，金额留空（全退）；备注「频道主提前删除」。
2. 详情页时间线显示 `delivery_refunded` + 备注。
3. 广告主余额回滚正常；频道主待确认收益相应减少。

### 7. 自用发布
1. 在 Bot 内：频道管理 → 频道详情 → 「🪧 自用发布」。
2. 选一条已有的 standard_card 素材（或用 CLI 提前 `create-material`）。
3. 测试频道里出现自用帖，按钮和外部投放相同三按钮。
4. 「🔍 查看详情」走 `start=sp_<id>`，详情页正常渲染。

### 8. 收益结算
```bash
chabo confirm-earnings --observation-hours 0
```
（生产默认 24 小时；演练时用 0 立即确认。）

1. 频道主在 Bot 内「💸 我的收益」，「✅ 已确认 > 0」。
2. 「📊 频道分布」按频道展示金额。
3. 「📜 收益流水」只列 publisher 侧条目（不该出现 Stars 充值这类 wallet 条目）。

### 9. 双人入账复核
1. 用运营 A 账号：`chabo request-topup --recipient-... --amount 5 --reason "测试" --requester-... --evidence-url "..."`
2. `/admin` 顶部「待审入账」红色告警。
3. 用运营 B 账号 approve（用同一账号 approve 必须被服务层拒绝）。
4. 收款方余额正确增加；`chabo list-tool-calls --tool-name topup_approve` 看到一条 success。

## C. 上线后 30 分钟巡检

- `/health` 持续返回 `db: ok`
- `tail -f /var/log/chabo/app.log`：没有 ERROR 级别异常
- `chabo list-tool-calls --result-status error --limit 20`：除测试期间产生的预期错误外，没有新增 error
- 至少手工触发一次 dispatch-due 与 confirm-earnings，确认调度链路活的

任一点异常 → 立刻停服 → 还原到上一个 backup-db 快照 → 排查后再上线。
