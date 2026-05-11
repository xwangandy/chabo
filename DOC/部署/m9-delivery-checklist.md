# M9 网页端上线交付清单

更新时间：2026-05-02

## 1. 交付范围

本批交付把插播从 Bot/CLI 为主推进到 React 网页端 + FastAPI API 的三端后台：

- 管理端：订单、入账、投放、争议、账户权限、频道管理、审计日志、钱包总览、上线设置。
- 广告主端：频道市场、素材库、投放计划、计划摘要确认、订单、钱包、设置。
- 频道主端：频道配置、广告位/价格/频控、投放记录、收益、设置。
- 认证权限：Telegram Mini App 登录、magic link、开发免登录、管理员隐藏入口、管理员等级、代看、手工授权/撤销。
- 安全审计：权限审计、审计详情、CSV 导出、链式 hash、时间窗/严格校验、审计报告留档。
- 部署验收：production verify gate、Nginx/systemd/env 模板、备份、回滚、临时 SQLite 恢复演练、生产 Runbook。

## 2. 文件归属

应纳入提交：

- `web/`：React + TypeScript + Vite 前端工程、H5 冒烟、权限 E2E。
- `src/chabo/webapi/`：FastAPI JSON API、认证权限、read models、投放计划 API。
- `src/chabo/audit.py`、`src/chabo/dev_seed.py`：审计链与网页端演示数据。
- `scripts/`：生产发布、回滚、审计报告留档、SQLite 恢复演练脚本。
- `DOC/部署/production-runbook.md`、`DOC/部署/m9-delivery-checklist.md` 和既有部署文档更新。
- `.env.production.example`、`web/.env.example`、`web/.env.production.example`：只含占位值的模板。
- `tests/test_webapi.py` 与既有测试更新。

不应纳入提交：

- `.runtime/`、`web/.runtime/`：本地 API pid、H5 截图、审计报告演练产物。
- `web/dist/`、`web/node_modules/`：前端构建产物和依赖目录。
- `*.sqlite3`、`*.sqlite3-shm`、`*.sqlite3-wal`：本地数据库和备份。
- `logs/`、`__pycache__/`、`*.egg-info/`、`.DS_Store`。

当前 `.gitignore` 已覆盖上述本地产物。`git ls-files --others --exclude-standard` 只应剩下新增源码、文档、脚本和 env 模板。

## 3. 敏感配置检查

检查结论：

- `.env.example` 和 `.env.production.example` 只包含 `replace_*` / example host / 本地开发值。
- `web/.env.example` 和 `web/.env.production.example` 不含密钥。
- 真实生产配置必须只写入 `/etc/chabo/env`，不要提交到 Git。
- 生产必须保持 `CHABO_DEV_AUTH_BYPASS=0` 和 `CHABO_DEV_SESSION_ENABLED=0`。

上线前再次执行：

```bash
git status --short
git ls-files --others --exclude-standard
git diff --check
```

## 4. 最终发布检查表

自动检查：

```bash
python3 -m unittest tests.test_webapi
python3 -m compileall -q src tests
python3 -m unittest discover -s tests
cd web && npm run build
cd web && npm run e2e:permissions
cd web && CHABO_H5_SMOKE_AUTH_BYPASS=1 npm run smoke:h5
python3 -m chabo.cli verify-audit-chain --created-from "2026-05-02 11:00:00" --strict
python3 -m chabo.cli verify-web --profile production --host chabo.example --health-url https://chabo.example/api/health --audit-chain --dry-run
bash -n scripts/deploy-web-production.sh scripts/rollback-web-production.sh scripts/export-audit-integrity-report.sh scripts/rehearse-sqlite-restore.sh
```

人工检查：

- 打开 `/admin#wallet`，确认资金总览、最近账本、资金账户榜。
- 打开 `/admin#settings`，确认发布准入、审计策略、数据库路径和最近备份。
- 打开 `/admin#audit`，选择上线时间窗，勾选“严格”，点击“校验链路”。
- 打开 `/advertiser`，手机宽度下生成计划摘要并确认按钮可用。
- 打开 `/publisher`，手机宽度下进入频道配置抽屉，确认无横向溢出。

## 5. 建议提交信息

```text
feat(web): add React portals and FastAPI web API

- add admin/advertiser/publisher React portal with dark H5-friendly UI
- add FastAPI session, magic link, Telegram WebApp auth, portal permissions and impersonation
- add advertiser planner/material/order/wallet APIs and publisher channel/earning APIs
- add admin operations for orders, topups, deliveries, disputes, accounts, channels, wallet and release settings
- add audit hash chain, CSV export signatures, verification API/CLI and audit report archival scripts
- add production deploy/rollback/runbook docs and web verification gates
```

## 6. PR / 合并说明模板

### Summary

- 新增 React + FastAPI 网页端三端后台，覆盖管理端、广告主端、频道主端。
- 完成生产登录权限、管理员等级、代看、审计链、部署 gate、回滚演练和 H5 冒烟。
- 补齐生产 Runbook、env 模板、Nginx/systemd 示例和最终发布检查表。

### Validation

- `python3 -m unittest discover -s tests`
- `npm run build`
- `npm run e2e:permissions`
- `CHABO_H5_SMOKE_AUTH_BYPASS=1 npm run smoke:h5`
- `python3 -m chabo.cli verify-web --profile production --host chabo.example --health-url https://chabo.example/api/health --audit-chain --dry-run`

### Deployment Notes

- 生产密钥只写 `/etc/chabo/env`。
- 发布前执行 `scripts/export-audit-integrity-report.sh` 和 `scripts/rehearse-sqlite-restore.sh`。
- 正式发布按 `DOC/部署/production-runbook.md` 执行。
- AG Grid Enterprise 商业授权仍需上线前确认。

## 7. M9 第二批：合并前 Git 策略

当前状态：

- 当前工作分支是 `main`，跟踪 `origin/main`，本地大批量网页端变更尚未提交。
- `origin` 指向 `https://github.com/xwangandy/chabo.git`。
- 本地已有 `codex/web-admin-react` 分支，当前指向与 `main / origin/main` 相同的基线提交。
- `git ls-files --others --exclude-standard` 只剩应交付的新增源码、测试、脚本、文档和 env 模板。
- 敏感扫描只命中变量名、占位说明和测试值，未发现真实生产密钥。

推荐策略：

- 优先使用 `codex/` 交付分支提交，再通过 PR 合并到云端 `main`。本批变更跨度很大，包含前端工程、FastAPI、DB 迁移、权限、安全审计、CI 和部署脚本，用 PR 更适合承载 CI、review、发布说明和回滚预案。
- 不建议直接在当前 `main` 上提交并推送，除非确认云端主分支没有保护规则，且团队接受跳过 PR review。
- 分支名建议使用 `codex/web-portals-m9`；如果希望沿用既有本地分支，也可以使用 `codex/web-admin-react`。

建议提交边界：

- 使用一个原子提交承载“插播 React 网页端 + FastAPI API + M6-M9 部署验收闭环”。
- 不拆成很多小提交，避免在大批量生成式开发历史里制造难以回滚的半成品节点。
- 不纳入 `.runtime/`、`web/.runtime/`、`web/dist/`、`web/node_modules/`、SQLite、日志、截图、审计报告演练产物。

确认提交策略后可执行：

```bash
git switch -c codex/web-portals-m9
git add .env.example .env.production.example .github/workflows/test.yml .gitignore README.md pyproject.toml DOC scripts src/chabo tests web
git status --short
git diff --cached --check
git commit -m "feat(web): add React portals and FastAPI web API"
git push -u origin codex/web-portals-m9
```

如果决定直接提交当前 `main`，也必须先执行同一组最终验证，再确认远程主分支策略允许直接 push。

## 8. 合并前最终确认项

- Git 策略：确认使用 PR 分支还是直接提交 `main`。
- 远程权限：确认当前 GitHub 账号有 push 权限。
- 商业授权：上线前确认 AG Grid Enterprise 授权。
- 生产凭证：正式发布前准备 `/etc/chabo/env`，不要把真实密钥写入仓库。
- 云服务器：准备部署目录、systemd 权限、Nginx 配置和 SQLite 备份目录。
