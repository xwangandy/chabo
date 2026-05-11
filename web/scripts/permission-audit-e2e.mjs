import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, expect } from "@playwright/test";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(scriptDir, "..");
const repoRoot = resolve(webRoot, "..");
const baseUrl = process.env.CHABO_E2E_BASE_URL || "http://127.0.0.1:5173";
const apiUrl = process.env.CHABO_E2E_API_URL || "http://127.0.0.1:8081";
const useAuthBypass = process.env.CHABO_E2E_AUTH_BYPASS !== "0";
const adminToken = process.env.CHABO_E2E_ADMIN_TOKEN || process.env.CHABO_ADMIN_TOKEN;
const targetUserId = process.env.CHABO_E2E_TARGET_USER_ID || `97${Date.now().toString().slice(-8)}`;
const targetDisplayName = process.env.CHABO_E2E_TARGET_DISPLAY_NAME || "E2E 权限审计用户";
const zhButton = (text) => new RegExp(text.split("").join("\\s*"));

await waitForHttp(`${apiUrl}/api/health`, "API");
await waitForHttp(baseUrl, "Vite app");
const target = createCleanTargetAccount();
const browser = await launchBrowser();

try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  if (!useAuthBypass) {
    const session = await createDevSession();
    await context.addCookies([
      {
        name: "chabo_session",
        value: session.token,
        url: `${baseUrl}/`,
        path: "/",
        httpOnly: true,
        sameSite: "Lax",
      },
    ]);
  }
  const page = await context.newPage();

  await page.goto(`${baseUrl}/admin`);
  await expect(page.getByRole("heading", { name: "管理端" })).toBeVisible();

  await page.getByRole("tab", { name: "钱包" }).click();
  await expect(page.getByTestId("admin-wallet-panel")).toBeVisible();
  await expect(page.getByText("全站可用余额")).toBeVisible();
  await expect(page.getByRole("heading", { name: "最近账本流水" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "资金账户榜" })).toBeVisible();

  await page.getByRole("tab", { name: "设置" }).click();
  await expect(page.getByTestId("admin-settings-panel")).toBeVisible();
  await expect(page.getByText("审计策略")).toBeVisible();
  await expect(page.getByRole("heading", { name: "发布准入" })).toBeVisible();

  await openPermissionDrawer(page, target);
  await fillPermissionReason(page, "E2E 开通广告主端");
  await drawerCard(page, "广告主端").getByRole("button", { name: zhButton("开通") }).click();
  await confirmPermissionModal(page);
  await expectPermissionDrawerClosed(page);
  await expect(accountRow(page, target)).toContainText("广告主 · active");

  await openPermissionDrawer(page, target);
  await fillPermissionReason(page, "E2E 设置管理员等级");
  await permissionDrawer(page).getByRole("button", { name: zhButton("只读") }).click();
  await confirmPermissionModal(page);
  await expectPermissionDrawerClosed(page);
  await expect(accountRow(page, target)).toContainText("管理 · viewer");

  await accountRow(page, target).getByRole("button", { name: /^广告主$/ }).click();
  await page.getByPlaceholder(/排查广告主计划提交异常/).fill("E2E 代看广告主端排查");
  await page.getByRole("button", { name: "进入代看" }).click();
  await expect(page).toHaveURL(/\/advertiser/);
  await expect(page.getByText("管理员代看中")).toBeVisible();
  await page.getByRole("button", { name: "结束代看" }).click();
  await expect(page).toHaveURL(/\/admin/);

  await openPermissionDrawer(page, target);
  await fillPermissionReason(page, "E2E 撤销广告主端");
  await drawerCard(page, "广告主端").getByRole("button", { name: zhButton("撤销") }).click();
  await confirmPermissionModal(page);
  await expectPermissionDrawerClosed(page);
  await expect(accountRow(page, target)).toContainText("广告主 · revoked");

  await page.getByRole("tab", { name: "审计" }).click();
  await page.getByPlaceholder("搜索动作 / 对象 ID / Payload").fill(target.account_id);
  await page.keyboard.press("Enter");

  await page.getByTestId("audit-category-filter").getByText("权限调整").click();
  const permissionAuditRow = page.locator("tr").filter({ hasText: "admin_portal_access_updated" }).first();
  await expect(permissionAuditRow).toBeVisible();
  await permissionAuditRow.getByRole("button", { name: zhButton("详情") }).click();
  const auditDrawer = page.locator(".ant-drawer-open").filter({ hasText: "审计详情" });
  await expect(auditDrawer.getByText("链式 Hash")).toBeVisible();
  await expect(auditDrawer.getByRole("heading", { name: "字段变更" })).toBeVisible();
  await auditDrawer.locator(".ant-drawer-close").click();
  await expect(auditDrawer).toHaveCount(0);
  await page.getByLabel("严格").check();
  await page.getByRole("button", { name: zhButton("校验链路") }).click();
  const integrityDrawer = page.locator(".ant-drawer-open").filter({ hasText: "审计链校验" });
  await expect(integrityDrawer.getByText("校验结果")).toBeVisible();
  await expect(integrityDrawer.getByText("当前链头")).toBeVisible();
  await expect(integrityDrawer.getByText("严格模式")).toBeVisible();
  await expect(integrityDrawer.getByText("开启")).toBeVisible();
  await integrityDrawer.locator(".ant-drawer-close").click();
  await expect(integrityDrawer).toHaveCount(0);

  await page.getByTestId("audit-category-filter").getByText("代看").click();
  await expect(page.locator("tr").filter({ hasText: "admin_impersonation_started" }).first()).toBeVisible();

  await page.getByTestId("audit-category-filter").getByText("管理员等级").click();
  await expect(page.locator("tr").filter({ hasText: "admin_portal_access_updated" }).first()).toBeVisible();

  console.log(
    JSON.stringify(
      {
        ok: true,
        target,
        flow: [
          "admin_wallet_settings",
          "grant_advertiser",
          "set_admin_level",
          "impersonate_advertiser",
          "return_admin",
          "revoke_advertiser",
          "audit_filters",
        ],
      },
      null,
      2
    )
  );
  await context.close();
} finally {
  await browser.close();
}

async function launchBrowser() {
  if (process.env.CHROME_PATH) {
    return chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true });
  }
  try {
    return await chromium.launch({ channel: process.env.PLAYWRIGHT_CHROME_CHANNEL || "chrome", headless: true });
  } catch {
    return chromium.launch({ headless: true });
  }
}

async function openPermissionDrawer(page, target) {
  await page.getByRole("tab", { name: "账户" }).click();
  const search = page.getByPlaceholder("搜索 Telegram ID / 昵称 / Account ID");
  await search.fill(target.telegram_user_id);
  await search.press("Enter");
  await expect(accountRow(page, target)).toBeVisible();
  await accountRow(page, target).getByRole("button", { name: zhButton("权限") }).click();
  await expect(permissionDrawer(page)).toBeVisible();
}

function accountRow(page, target) {
  return page.locator("tr").filter({ hasText: target.telegram_user_id }).first();
}

function permissionDrawer(page) {
  return page.locator(".ant-drawer-open").filter({ hasText: "权限管理" });
}

function drawerCard(page, label) {
  return permissionDrawer(page).locator(".mobile-action-card").filter({ hasText: label });
}

async function expectPermissionDrawerClosed(page) {
  await expect(permissionDrawer(page)).toHaveCount(0, { timeout: 10000 });
}

async function fillPermissionReason(page, reason) {
  await permissionDrawer(page).getByPlaceholder(/填写调整原因/).fill(reason);
  await permissionDrawer(page).getByPlaceholder(/高风险操作需输入/).fill("确认调整权限");
}

async function confirmPermissionModal(page) {
  const dialog = page.locator(".ant-modal").filter({ hasText: "确认权限调整" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: zhButton("确认执行") }).click();
}

async function createDevSession() {
  if (!adminToken) {
    throw new Error("CHABO_E2E_ADMIN_TOKEN/CHABO_ADMIN_TOKEN is required when CHABO_E2E_AUTH_BYPASS=0");
  }
  const response = await fetch(`${apiUrl}/api/auth/dev-session`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Chabo-Admin-Token": adminToken,
    },
    body: JSON.stringify({
      telegram_user_id: process.env.CHABO_E2E_ADMIN_USER_ID || "10001",
      display_name: process.env.CHABO_E2E_ADMIN_DISPLAY_NAME || "E2E 管理员",
      portals: ["admin", "advertiser", "publisher"],
    }),
  });
  if (!response.ok) {
    throw new Error(`dev session failed: ${response.status} ${await response.text()}`);
  }
  return response.json();
}

function createCleanTargetAccount() {
  const code = `
import json
import os
from chabo.app import create_app
from chabo.config import Settings

app = create_app(Settings.from_env())
telegram_user_id = os.environ["CHABO_E2E_TARGET_USER_ID"]
display_name = os.environ["CHABO_E2E_TARGET_DISPLAY_NAME"]
with app.db.transaction() as conn:
    account = app.ledger.accounts.get_or_create_by_telegram(conn, telegram_user_id, "mixed", display_name)
    conn.execute(
        "DELETE FROM portal_access WHERE account_id = ? AND portal IN ('admin', 'advertiser', 'publisher')",
        (account["id"],),
    )
    conn.execute(
        "UPDATE accounts SET display_name = ?, active_role = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (display_name, account["id"]),
    )
    print(json.dumps({"account_id": account["id"], "telegram_user_id": telegram_user_id, "display_name": display_name}, ensure_ascii=False))
`;
  const result = spawnSync("python3", ["-c", code], {
    cwd: repoRoot,
    env: {
      ...process.env,
      CHABO_E2E_TARGET_USER_ID: targetUserId,
      CHABO_E2E_TARGET_DISPLAY_NAME: targetDisplayName,
    },
    encoding: "utf-8",
  });
  if (result.status !== 0) {
    throw new Error(`target account setup failed: ${result.stderr || result.stdout}`);
  }
  return JSON.parse(result.stdout.trim());
}

async function waitForHttp(url, label) {
  for (let i = 0; i < 40; i += 1) {
    try {
      const response = await fetch(url);
      if (response.ok || response.status < 500) return;
    } catch {
      // Service may still be starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`${label} was not reachable at ${url}`);
}
