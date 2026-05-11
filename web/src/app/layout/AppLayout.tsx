import {
  AppstoreOutlined,
  AuditOutlined,
  BankOutlined,
  DashboardOutlined,
  DownOutlined,
  FileTextOutlined,
  HistoryOutlined,
  LogoutOutlined,
  ProfileOutlined,
  RollbackOutlined,
  SettingOutlined,
  ShopOutlined,
  TeamOutlined,
  WalletOutlined
} from "@ant-design/icons";
import { Button, Dropdown, Layout, Menu, Space, Tag, Typography, type MenuProps } from "antd";
import type { ReactNode } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { useMe, useLogout, useStopImpersonation } from "../../shared/auth/session";
import type { Portal } from "../../shared/api/client";

const { Header, Sider, Content } = Layout;

const portalMeta: Record<Portal, { label: string; shortLabel: string; icon: ReactNode }> = {
  admin: { label: "管理端", shortLabel: "管理", icon: <AuditOutlined /> },
  advertiser: { label: "广告主端", shortLabel: "广告", icon: <AppstoreOutlined /> },
  publisher: { label: "频道主端", shortLabel: "频道", icon: <BankOutlined /> }
};

const workspaceNav: Record<Portal, Array<{ key: string; label: string; icon: ReactNode }>> = {
  admin: [
    { key: "orders", label: "订单处理", icon: <FileTextOutlined /> },
    { key: "topups", label: "入账审核", icon: <WalletOutlined /> },
    { key: "wallet", label: "钱包总览", icon: <WalletOutlined /> },
    { key: "deliveries", label: "投放记录", icon: <HistoryOutlined /> },
    { key: "disputes", label: "争议处理", icon: <AuditOutlined /> },
    { key: "accounts", label: "账户权限", icon: <TeamOutlined /> },
    { key: "channels", label: "频道管理", icon: <BankOutlined /> },
    { key: "audit", label: "审计日志", icon: <ProfileOutlined /> },
    { key: "settings", label: "上线设置", icon: <SettingOutlined /> }
  ],
  advertiser: [
    { key: "market", label: "频道市场", icon: <ShopOutlined /> },
    { key: "materials", label: "素材库", icon: <AppstoreOutlined /> },
    { key: "plans", label: "投放计划", icon: <DashboardOutlined /> },
    { key: "orders", label: "订单", icon: <FileTextOutlined /> },
    { key: "wallet", label: "钱包", icon: <WalletOutlined /> },
    { key: "settings", label: "设置", icon: <SettingOutlined /> }
  ],
  publisher: [
    { key: "channels", label: "频道管理", icon: <BankOutlined /> },
    { key: "earnings", label: "收益", icon: <WalletOutlined /> },
    { key: "settings", label: "设置", icon: <SettingOutlined /> }
  ]
};

export function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { data } = useMe();
  const logout = useLogout();
  const stopImpersonation = useStopImpersonation();
  const portals = data?.portals ?? [];
  const selected = (location.pathname.split("/")[1] || "advertiser") as Portal;
  const availablePortals = [
    portals.includes("admin")
      ? "admin"
      : null,
    portals.includes("advertiser") || portals.includes("admin")
      ? "advertiser"
      : null,
    portals.includes("publisher") || portals.includes("admin")
      ? "publisher"
      : null
  ].filter(Boolean) as Portal[];
  const currentPortal = availablePortals.includes(selected) ? selected : availablePortals[0] ?? "advertiser";
  const currentNav = workspaceNav[currentPortal];
  const currentHash = decodeURIComponent(location.hash.replace(/^#/, ""));
  const selectedNavKey = currentNav.some((item) => item.key === currentHash) ? currentHash : currentNav[0]?.key;
  const impersonationExpiresAt = data?.session_expires_at
    ? new Date(data.session_expires_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
    : null;
  const portalMenuItems: MenuProps["items"] = availablePortals.map((portal) => ({
    key: portal,
    icon: portalMeta[portal].icon,
    label: portalMeta[portal].label
  }));
  const workspaceItems: MenuProps["items"] = currentNav.map((item) => ({
    key: `${currentPortal}:${item.key}`,
    icon: item.icon,
    label: item.label
  }));

  return (
    <Layout className="app-shell">
      <Sider width={220} breakpoint="md" collapsedWidth={0}>
        <div className="brand">
          <span className="brand-name">插播</span>
          <Dropdown
            trigger={["click"]}
            menu={{
              items: portalMenuItems,
              selectedKeys: [currentPortal],
              onClick: ({ key }) => navigate(`/${key}`)
            }}
          >
            <Button className="portal-switch" type="text" size="small">
              {portalMeta[currentPortal].shortLabel}
              <DownOutlined />
            </Button>
          </Dropdown>
        </div>
        <div className="workspace-caption">{portalMeta[currentPortal].label}</div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[`${currentPortal}:${selectedNavKey}`]}
          items={workspaceItems}
          onClick={({ key }) => {
            const [portal, tab] = String(key).split(":");
            const defaultTab = workspaceNav[portal as Portal][0]?.key;
            navigate(`/${portal}${tab && tab !== defaultTab ? `#${tab}` : ""}`);
          }}
        />
      </Sider>
      <Layout>
        <Header className="topbar">
          <Space size={12} wrap>
            <Typography.Text strong>
              {data?.account.display_name || data?.account.telegram_user_id || "未登录"}
            </Typography.Text>
            <Tag>{portalMeta[currentPortal].label}</Tag>
            {data?.impersonator_account_id ? (
              <Tag color="gold">管理员代看中{impersonationExpiresAt ? ` · 至 ${impersonationExpiresAt}` : ""}</Tag>
            ) : null}
          </Space>
          <Space>
            {data?.impersonator_account_id ? (
              <Button
                icon={<RollbackOutlined />}
                onClick={() => stopImpersonation.mutate()}
                loading={stopImpersonation.isPending}
                size="small"
              >
                结束代看
              </Button>
            ) : null}
            <Button
              icon={<LogoutOutlined />}
              onClick={() => logout.mutate()}
              size="small"
            >
              退出
            </Button>
          </Space>
        </Header>
        <Content className="content">
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
