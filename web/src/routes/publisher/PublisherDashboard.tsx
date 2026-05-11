import { Col, Row, Space, Tabs, Table, Tag, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";
import { apiFetch, cents } from "../../shared/api/client";
import { MetricCard } from "../../shared/components/MetricCard";
import { ChannelsPanel } from "./ChannelsPanel";
import { useMe } from "../../shared/auth/session";
import { useHashTab } from "../../shared/hooks/useHashTab";

interface PublisherDashboardResponse {
  earnings: {
    pending: string;
    confirmed: string;
    releasable: string;
  };
  channels: number;
  deliveries: number;
}

interface ChannelEarning {
  channel_id: string;
  title: string;
  ref_token: string;
  pending_cents: number;
  confirmed_cents: number;
  platform_fee_cents: number;
  delivery_count: number;
}

interface PublisherEarningsResponse {
  channels: ChannelEarning[];
}

const publisherTabKeys = ["channels", "earnings", "settings"] as const;

export function PublisherDashboard() {
  const { data: me } = useMe();
  const [activeTab, setActiveTab] = useHashTab("channels", publisherTabKeys);
  const { data } = useQuery({
    queryKey: ["publisher", "dashboard"],
    queryFn: () =>
      apiFetch<PublisherDashboardResponse>("/api/publisher/dashboard")
  });
  return (
    <section className="page-stack">
      <Typography.Title level={2}>频道主端</Typography.Title>
      <Row gutter={[12, 12]}>
        <Col xs={12} lg={6}>
          <MetricCard title="频道数量" value={data?.channels ?? 0} />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="待确认收益" value={data?.earnings.pending ?? "0.00"} suffix="USD" />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="可结算收益" value={data?.earnings.releasable ?? "0.00"} suffix="USD" />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="广告记录" value={data?.deliveries ?? 0} />
        </Col>
      </Row>
      <Tabs
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key as (typeof publisherTabKeys)[number])}
        items={[
          { key: "channels", label: "频道管理", children: <ChannelsPanel /> },
          { key: "earnings", label: "收益", children: <PublisherEarningsPanel data={data} /> },
          { key: "settings", label: "设置", children: <PublisherSettingsPanel me={me} /> }
        ]}
      />
    </section>
  );
}

function PublisherEarningsPanel({ data }: { data?: PublisherDashboardResponse }) {
  const earnings = useQuery({
    queryKey: ["publisher", "earnings"],
    queryFn: () => apiFetch<PublisherEarningsResponse>("/api/publisher/earnings")
  });
  return (
    <Space direction="vertical" size={14} className="full-width">
      <Row gutter={[12, 12]}>
        <Col xs={24} md={8}>
          <MetricCard title="待确认收益" value={data?.earnings.pending ?? "0.00"} suffix="USD" />
        </Col>
        <Col xs={24} md={8}>
          <MetricCard title="已确认收益" value={data?.earnings.confirmed ?? "0.00"} suffix="USD" />
        </Col>
        <Col xs={24} md={8}>
          <MetricCard title="可结算收益" value={data?.earnings.releasable ?? "0.00"} suffix="USD" />
        </Col>
      </Row>
      <Table<ChannelEarning>
        className="desktop-table"
        rowKey="channel_id"
        size="small"
        loading={earnings.isLoading}
        dataSource={earnings.data?.channels ?? []}
        pagination={false}
        locale={{ emptyText: "暂无频道收益" }}
        columns={[
          { title: "频道", dataIndex: "title", ellipsis: true },
          { title: "Token", dataIndex: "ref_token", width: 160, ellipsis: true },
          { title: "待确认", dataIndex: "pending_cents", width: 120, render: cents },
          { title: "已确认", dataIndex: "confirmed_cents", width: 120, render: cents },
          { title: "平台费", dataIndex: "platform_fee_cents", width: 120, render: cents },
          { title: "记录", dataIndex: "delivery_count", width: 90 }
        ]}
      />
      <div className="mobile-ops-list">
        {(earnings.data?.channels ?? []).map((channel) => (
          <section className="mobile-action-card" key={channel.channel_id}>
            <Typography.Text strong>{channel.title}</Typography.Text>
            <Space size={[6, 6]} wrap>
              <Tag>待确认 {cents(channel.pending_cents)}</Tag>
              <Tag>已确认 {cents(channel.confirmed_cents)}</Tag>
              <Tag>平台费 {cents(channel.platform_fee_cents)}</Tag>
              <Tag>记录 {channel.delivery_count}</Tag>
            </Space>
            <Typography.Text type="secondary">{channel.ref_token}</Typography.Text>
          </section>
        ))}
        {earnings.data?.channels?.length === 0 ? <Typography.Text type="secondary">暂无频道收益</Typography.Text> : null}
      </div>
    </Space>
  );
}

function PublisherSettingsPanel({ me }: { me: ReturnType<typeof useMe>["data"] }) {
  return (
    <section className="mobile-section">
      <Space direction="vertical" size={10}>
        <Typography.Title level={4}>账户设置</Typography.Title>
        <Typography.Text>{me?.account.display_name || me?.account.telegram_user_id || "未登录账户"}</Typography.Text>
        <Space size={[6, 6]} wrap>
          {(me?.portal_statuses ?? []).map((item) => (
            <Tag key={item.portal}>{`${item.portal} · ${item.status}`}</Tag>
          ))}
        </Space>
      </Space>
    </section>
  );
}
