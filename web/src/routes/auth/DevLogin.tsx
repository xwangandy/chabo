import { KeyOutlined, LoginOutlined } from "@ant-design/icons";
import { Alert, App, Button, Card, Checkbox, Form, Input, Result, Space, Spin, Tag, Typography } from "antd";
import { useEffect, useMemo, useRef } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import type { Portal, PortalStatus } from "../../shared/api/client";
import {
  landingForPortals,
  useAuthConfig,
  useCreateMagicLink,
  useMe,
  useTelegramWebAppLogin
} from "../../shared/auth/session";

interface AdminLoginForm {
  telegram_user_id: string;
  display_name?: string;
  admin_token: string;
  portals: Portal[];
}

const portalLabels: Record<Portal, string> = {
  admin: "管理端",
  advertiser: "广告主端",
  publisher: "频道主端"
};

function portalStatusTone(status: PortalStatus["status"]) {
  if (status === "active") return "success";
  if (status === "candidate") return "processing";
  if (status === "suspended") return "warning";
  return "error";
}

function PortalStatusList({ statuses }: { statuses: PortalStatus[] }) {
  if (!statuses.length) return null;
  return (
    <Space size={[8, 8]} wrap>
      {statuses.map((item) => (
        <Tag key={item.portal} color={portalStatusTone(item.status)}>
          {portalLabels[item.portal]} · {item.status}
        </Tag>
      ))}
    </Space>
  );
}

export function Login() {
  const { message } = App.useApp();
  const me = useMe();
  const config = useAuthConfig();
  const telegramLogin = useTelegramWebAppLogin();
  const autoTried = useRef(false);
  const portals = me.data?.portals ?? [];
  const portalStatuses = me.data?.portal_statuses ?? [];
  const initData = window.Telegram?.WebApp?.initData || "";

  useEffect(() => {
    window.Telegram?.WebApp?.ready?.();
  }, []);

  useEffect(() => {
    if (initData && !autoTried.current && !telegramLogin.isPending && !telegramLogin.isSuccess) {
      autoTried.current = true;
      telegramLogin.mutate(initData, {
        onError: (error) => message.error(String(error.message || error))
      });
    }
  }, [initData, message, telegramLogin]);

  if (me.isLoading || config.isLoading) {
    return <Spin fullscreen />;
  }
  if (portals.length > 0) {
    return <Navigate to={landingForPortals(portals)} replace />;
  }

  const hasCandidate = portalStatuses.some((item) => item.status === "candidate");

  return (
    <div className="login-page">
      <Card className="login-card" title="插播工作台">
        <Space direction="vertical" size={14} className="full-width">
          {telegramLogin.isError ? (
            <Alert type="error" showIcon message={String(telegramLogin.error.message || telegramLogin.error)} />
          ) : null}
          {hasCandidate ? (
            <Alert
              type="info"
              showIcon
              message="门户待开通"
              description="广告主端会在首次投放审核通过后开通；频道主端会在频道产生真实投放后开通。"
            />
          ) : null}
          <PortalStatusList statuses={portalStatuses} />
          {initData ? (
            <Button
              type="primary"
              icon={<LoginOutlined />}
              loading={telegramLogin.isPending}
              onClick={() =>
                telegramLogin.mutate(initData, {
                  onError: (error) => message.error(String(error.message || error))
                })
              }
              block
            >
              Telegram 登录
            </Button>
          ) : (
            <Result
              status={config.data?.telegram_webapp_available ? "info" : "warning"}
              title={config.data?.telegram_webapp_available ? "请从 Telegram 打开" : "Telegram 登录未配置"}
            />
          )}
        </Space>
      </Card>
    </div>
  );
}

export function AdminLogin() {
  const { message } = App.useApp();
  const navigate = useNavigate();
  const me = useMe();
  const createMagicLink = useCreateMagicLink();
  const portals = me.data?.portals ?? [];
  const portalOptions = useMemo(
    () => [
      { label: "管理端", value: "admin" },
      { label: "广告主端", value: "advertiser" },
      { label: "频道主端", value: "publisher" }
    ],
    []
  );

  if (me.isLoading) {
    return <Spin fullscreen />;
  }
  if (portals.length > 0) {
    return <Navigate to={landingForPortals(portals)} replace />;
  }

  return (
    <div className="login-page">
      <Card className="login-card" title="运营授权入口">
        <Form<AdminLoginForm>
          layout="vertical"
          initialValues={{ portals: ["admin"] }}
          onFinish={(values) => {
            createMagicLink.mutate(values, {
              onSuccess: (data) => navigate(data.path, { replace: true }),
              onError: (error) => message.error(String(error.message || error))
            });
          }}
        >
          <Form.Item label="Telegram 用户 ID" name="telegram_user_id" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item label="显示名" name="display_name">
            <Input />
          </Form.Item>
          <Form.Item label="管理 Token" name="admin_token" rules={[{ required: true }]}>
            <Input.Password />
          </Form.Item>
          <Form.Item label="门户" name="portals" rules={[{ required: true }]}>
            <Checkbox.Group options={portalOptions} />
          </Form.Item>
          <Button type="primary" icon={<KeyOutlined />} htmlType="submit" loading={createMagicLink.isPending} block>
            生成一次性登录
          </Button>
        </Form>
        <Typography.Text type="secondary" className="login-note">
          该入口不会出现在普通登录页。
        </Typography.Text>
      </Card>
    </div>
  );
}
