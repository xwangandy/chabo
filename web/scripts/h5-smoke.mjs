import { spawn } from "node:child_process";
import { access, constants, mkdir, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";

const baseUrl = process.env.CHABO_H5_SMOKE_BASE_URL || "http://127.0.0.1:5173";
const apiUrl = process.env.CHABO_H5_SMOKE_API_URL || "http://127.0.0.1:8081";
const adminToken = process.env.CHABO_H5_SMOKE_ADMIN_TOKEN || process.env.CHABO_ADMIN_TOKEN;
const useAuthBypass = process.env.CHABO_H5_SMOKE_AUTH_BYPASS === "1";
const telegramUserId = process.env.CHABO_H5_SMOKE_USER_ID || "10001";
const displayName = process.env.CHABO_H5_SMOKE_DISPLAY_NAME || "H5 冒烟用户";
const shouldSubmitPlan = process.env.CHABO_H5_SMOKE_SUBMIT === "1";
const outputDir = resolve(process.env.CHABO_H5_SMOKE_OUTPUT_DIR || ".runtime/h5-smoke");
const width = Number(process.env.CHABO_H5_SMOKE_WIDTH || 390);
const height = Number(process.env.CHABO_H5_SMOKE_HEIGHT || 844);

if (!adminToken && !useAuthBypass) {
  throw new Error("CHABO_H5_SMOKE_ADMIN_TOKEN/CHABO_ADMIN_TOKEN is required unless CHABO_H5_SMOKE_AUTH_BYPASS=1");
}

const chromePath = await findChrome();
const userDataDir = `/tmp/chabo-h5-chrome-${Date.now()}`;
const debugPort = Number(process.env.CHABO_H5_SMOKE_DEBUG_PORT || 9339);
const delay = (ms) => new Promise((resolveDelay) => setTimeout(resolveDelay, ms));

await mkdir(outputDir, { recursive: true });

const session = useAuthBypass ? null : await createDevSession();
const chrome = spawn(
  chromePath,
  [
    "--headless=new",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${userDataDir}`,
    `--window-size=${width},${height}`,
    "about:blank",
  ],
  { stdio: ["ignore", "ignore", "pipe"] }
);

try {
  const pageWsUrl = await waitForPageWebSocketUrl();
  const ws = new WebSocket(pageWsUrl);
  await new Promise((resolveOpen, rejectOpen) => {
    ws.addEventListener("open", resolveOpen, { once: true });
    ws.addEventListener("error", rejectOpen, { once: true });
  });
  const cdp = createCdp(ws);
  const results = [];
  const screenshots = {};

  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Network.enable");
  await setMobileViewport(cdp);
  if (session) {
    await cdp.send("Network.setCookie", {
      name: "chabo_session",
      value: session.token,
      url: `${baseUrl}/`,
      path: "/",
      httpOnly: true,
      sameSite: "Lax",
    });
  }

  await navigate(cdp, `${baseUrl}/advertiser`);
  await waitExpr(cdp, `document.querySelector('[data-testid="mobile-channel-list"]')`);
  await waitExpr(cdp, `document.querySelector('[data-testid^="mobile-channel-toggle-"]')`);
  await evalValue(cdp, `document.querySelector('[data-testid^="mobile-channel-toggle-"]').click(); true`);
  await delay(350);
  await waitExpr(cdp, `!document.querySelector('[data-testid="planner-generate-plan"]').disabled`);
  await evalValue(cdp, `document.querySelector('[data-testid="planner-generate-plan"]').click(); true`);
  await waitExpr(cdp, `document.querySelector('[data-testid="mobile-plan-summary"]')`);
  await evalValue(cdp, `document.querySelector('[data-testid="mobile-plan-summary"]').scrollIntoView({ block: 'center' }); true`);
  await delay(500);
  await waitExpr(cdp, `document.querySelector('[data-testid="mobile-plan-submit"]') && !document.querySelector('[data-testid="mobile-plan-submit"]').disabled`);
  results.push(await layoutCheck(cdp, "advertiser-plan-summary"));
  screenshots.advertiser = await screenshot(cdp, "advertiser-plan-summary-390");
  if (shouldSubmitPlan) {
    await evalValue(cdp, `document.querySelector('[data-testid="mobile-plan-submit"]').click(); true`);
    await delay(1500);
  }

  await navigate(cdp, `${baseUrl}/admin`);
  await waitExpr(cdp, `document.querySelector('[data-testid="mobile-admin-ops-list"]')`);
  results.push(await layoutCheck(cdp, "admin-mobile-ops"));
  screenshots.admin = await screenshot(cdp, "admin-mobile-ops-390");

  await navigate(cdp, `${baseUrl}/admin#wallet`);
  await waitExpr(cdp, `document.querySelector('[data-testid="mobile-admin-ops-list"]')`);
  await scrollMobileSection(cdp, "钱包总览");
  results.push(await layoutCheck(cdp, "admin-wallet"));
  screenshots.adminWallet = await screenshot(cdp, "admin-wallet-390");

  await navigate(cdp, `${baseUrl}/admin#settings`);
  await waitExpr(cdp, `document.querySelector('[data-testid="mobile-admin-ops-list"]')`);
  await scrollMobileSection(cdp, "上线设置");
  results.push(await layoutCheck(cdp, "admin-settings"));
  screenshots.adminSettings = await screenshot(cdp, "admin-settings-390");

  await setMobileViewport(cdp);
  await navigate(cdp, `${baseUrl}/publisher`);
  await waitExpr(cdp, `document.querySelector('[data-testid="mobile-publisher-channel-list"]')`);
  await waitExpr(cdp, `document.querySelector('[data-testid="mobile-publisher-channel-card"] button')`);
  await evalValue(
    cdp,
    `(() => {
      const button = Array.from(document.querySelectorAll('[data-testid="mobile-publisher-channel-card"] button'))
        .find((item) => item.textContent.replace(/\\s/g, '').includes('配置'));
      if (!button) throw new Error('publisher config button missing');
      button.click();
      return true;
    })()`
  );
  await waitExpr(cdp, `document.querySelector('.channel-settings')`);
  await evalValue(cdp, `document.querySelector('.channel-settings').scrollIntoView({ block: 'center' }); true`);
  await delay(500);
  results.push(await layoutCheck(cdp, "publisher-channel-config"));
  screenshots.publisher = await screenshot(cdp, "publisher-channel-config-390");

  const failed = results.filter((item) => item.overflowX || item.offenders.length);
  const report = { outputDir, screenshots, results };
  await writeFile(join(outputDir, "results.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
  ws.close();
  if (failed.length) {
    throw new Error(`H5 smoke found horizontal overflow: ${failed.map((item) => item.name).join(", ")}`);
  }
} finally {
  chrome.kill("SIGTERM");
  await delay(500);
  await rm(userDataDir, { recursive: true, force: true });
}

async function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      await access(candidate, constants.X_OK);
      return candidate;
    } catch {
      // Keep scanning common locations.
    }
  }
  throw new Error("Chrome/Chromium not found. Set CHROME_PATH before running smoke:h5.");
}

async function createDevSession() {
  const response = await fetch(`${apiUrl}/api/auth/dev-session`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Chabo-Admin-Token": adminToken,
    },
    body: JSON.stringify({
      telegram_user_id: telegramUserId,
      display_name: displayName,
      portals: ["admin", "advertiser", "publisher"],
    }),
  });
  if (!response.ok) {
    throw new Error(
      `dev session failed: ${response.status} ${await response.text()}. ` +
        "Set CHABO_DEV_SESSION_ENABLED=1 on the API process or use CHABO_H5_SMOKE_AUTH_BYPASS=1."
    );
  }
  return response.json();
}

async function waitForPageWebSocketUrl() {
  for (let i = 0; i < 80; i += 1) {
    try {
      const pages = await fetch(`http://127.0.0.1:${debugPort}/json`).then((response) => response.json());
      const page = pages.find((candidate) => candidate.type === "page");
      if (page?.webSocketDebuggerUrl) {
        return page.webSocketDebuggerUrl;
      }
    } catch {
      // Chrome may still be booting.
    }
    await delay(250);
  }
  throw new Error("Chrome CDP page was not ready");
}

function createCdp(ws) {
  let id = 0;
  const pending = new Map();
  const waiters = new Map();
  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) {
        reject(new Error(`${msg.error.message}: ${msg.error.data || ""}`));
      } else {
        resolve(msg.result || {});
      }
      return;
    }
    const list = waiters.get(msg.method);
    if (list?.length) {
      list.splice(0).forEach((resolve) => resolve(msg.params || {}));
    }
  });
  return {
    send(method, params = {}) {
      return new Promise((resolve, reject) => {
        const callId = ++id;
        pending.set(callId, { resolve, reject });
        ws.send(JSON.stringify({ id: callId, method, params }));
      });
    },
    waitEvent(method, timeout = 10000) {
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`timeout waiting ${method}`)), timeout);
        const list = waiters.get(method) || [];
        list.push((params) => {
          clearTimeout(timer);
          resolve(params);
        });
        waiters.set(method, list);
      });
    },
  };
}

async function setMobileViewport(cdp) {
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 2,
    mobile: true,
  });
  await cdp.send("Emulation.setTouchEmulationEnabled", { enabled: true });
}

async function navigate(cdp, url) {
  const load = cdp.waitEvent("Page.loadEventFired", 10000).catch(() => null);
  await cdp.send("Page.navigate", { url });
  await load;
  await delay(900);
}

async function evalValue(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(JSON.stringify(result.exceptionDetails));
  }
  return result.result?.value;
}

async function waitExpr(cdp, expression, timeout = 10000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await evalValue(cdp, `Boolean(${expression})`)) {
      return;
    }
    await delay(250);
  }
  throw new Error(`timeout waiting expression: ${expression}`);
}

async function scrollMobileSection(cdp, title) {
  await evalValue(
    cdp,
    `(() => {
      const section = Array.from(document.querySelectorAll('[data-testid="mobile-admin-ops-section"]'))
        .find((item) => item.textContent.includes(${JSON.stringify(title)}));
      if (!section) throw new Error('mobile admin section missing: ${title.replace(/'/g, "\\'")}');
      section.scrollIntoView({ block: 'center' });
      return true;
    })()`
  );
  await delay(500);
}

async function screenshot(cdp, name) {
  const shot = await cdp.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  });
  const path = join(outputDir, `${name}.png`);
  await writeFile(path, Buffer.from(shot.data, "base64"));
  return path;
}

async function layoutCheck(cdp, name) {
  return evalValue(
    cdp,
    `(() => {
      const visible = (el) => {
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 1 && rect.height > 1;
      };
      const offenders = Array.from(document.querySelectorAll('body *'))
        .filter(visible)
        .map((el) => {
          const rect = el.getBoundingClientRect();
          return {
            tag: el.tagName.toLowerCase(),
            cls: String(el.className || ''),
            text: (el.textContent || '').trim().slice(0, 40),
            left: Math.round(rect.left),
            right: Math.round(rect.right),
            width: Math.round(rect.width)
          };
        })
        .filter((item) => item.left < -2 || item.right > window.innerWidth + 2)
        .filter((item) => !item.cls.includes('ant-layout-sider-zero-width-trigger'))
        .slice(0, 12);
      return {
        name: ${JSON.stringify(name)},
        width: window.innerWidth,
        height: window.innerHeight,
        bodyScrollWidth: document.body.scrollWidth,
        overflowX: document.body.scrollWidth > window.innerWidth + 2,
        offenders
      };
    })()`
  );
}
