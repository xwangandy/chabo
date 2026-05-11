import { Col, Row, Space, Tabs, Table, Tag, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";
import { apiFetch, cents } from "../../shared/api/client";
import { MetricCard } from "../../shared/components/MetricCard";
import { ChannelMarketGrid } from "../../features/planner/ChannelMarketGrid";
import { MaterialsPanel } from "./MaterialsPanel";
import { OrdersPanel } from "./OrdersPanel";
import { PlansPanel } from "./PlansPanel";
import { useMe } from "../../shared/auth/session";
import { useHashTab } from "../../shared/hooks/useHashTab";

interface AdvertiserDashboardResponse {
  balance: {
    available: string;
    reserved: string;
    spent: string;
  };
  orders: {
    total_orders: number;
    running_orders: number;
    pending_orders: number;
  };
}

interface WalletTransaction {
  id: string;
  type: string;
  amount_cents: number;
  currency: string;
  memo: string | null;
  created_at: string;
  order_id: string | null;
  delivery_id: string | null;
}

interface ReservedOrder {
  order_id: string;
  status: string;
  reserved_cents: number;
  budget_cents: number;
  spent_cents: number;
  currency: string;
  channel_title: string | null;
}

interface AdvertiserWalletResponse {
  transactions: WalletTransaction[];
  reserved_orders: ReservedOrder[];
}

const advertiserTabKeys = ["market", "materials", "plans", "orders", "wallet", "settings"] as const;
const walletTypeLabels: Record<string, string> = {
  manual_topup: "人工入账",
  reserve_order_budget: "冻结预算",
  release_order_reserve: "释放冻结",
  charge_delivery: "投放扣费",
  refund_delivery: "投放退款",
  platform_fee: "平台服务费"
};

export function AdvertiserDashboard() {
  const { data: me } = useMe();
  const [activeTab, setActiveTab] = useHashTab("market", advertiserTabKeys);
  const { data } = useQuery({
    queryKey: ["advertiser", "dashboard"],
    queryFn: () =>
      apiFetch<AdvertiserDashboardResponse>("/api/advertiser/dashboard")
  });
  return (
    <section className="page-stack">
      <Typography.Title level={2}>广告主端</Typography.Title>
      <Row gutter={[12, 12]}>
        <Col xs={12} lg={6}>
          <MetricCard title="可用余额" value={data?.balance.available ?? "0.00"} suffix="USD" />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="冻结预算" value={data?.balance.reserved ?? "0.00"} suffix="USD" />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="进行中订单" value={data?.orders.running_orders ?? 0} />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="待审核订单" value={data?.orders.pending_orders ?? 0} />
        </Col>
      </Row>
      <Tabs
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key as (typeof advertiserTabKeys)[number])}
        items={[
          { key: "market", label: "频道市场", children: <ChannelMarketGrid /> },
          { key: "materials", label: "素材库", children: <MaterialsPanel /> },
          { key: "plans", label: "投放计划", children: <PlansPanel /> },
          { key: "orders", label: "订单", children: <OrdersPanel /> },
          { key: "wallet", label: "钱包", children: <AdvertiserWalletPanel data={data} /> },
          { key: "settings", label: "设置", children: <AdvertiserSettingsPanel me={me} /> }
        ]}
      />
    </section>
  );
}

function AdvertiserWalletPanel({ data }: { data?: AdvertiserDashboardResponse }) {
  const wallet = useQuery({
    queryKey: ["advertiser", "wallet"],
    queryFn: () => apiFetch<AdvertiserWalletResponse>("/api/advertiser/wallet?limit=20")
  });
  return (
    <Space direction="vertical" size={14} className="full-width">
      <Row gutter={[12, 12]}>
        <Col xs={24} md={8}>
          <MetricCard title="可用余额" value={data?.balance.available ?? "0.00"} suffix="USD" />
        </Col>
        <Col xs={24} md={8}>
          <MetricCard title="冻结预算" value={data?.balance.reserved ?? "0.00"} suffix="USD" />
        </Col>
        <Col xs={24} md={8}>
          <MetricCard title="累计消耗" value={data?.balance.spent ?? "0.00"} suffix="USD" />
        </Col>
      </Row>
      <Table<ReservedOrder>
        className="desktop-table"
        rowKey="order_id"
        size="small"
        loading={wallet.isLoading}
        dataSource={wallet.data?.reserved_orders ?? []}
        pagination={false}
        locale={{ emptyText: "暂无冻结订单" }}
        columns={[
          { title: "订单", dataIndex: "order_id", ellipsis: true },
          { title: "频道", dataIndex: "channel_title", ellipsis: true },
          { title: "状态", dataIndex: "status", width: 110 },
          { title: "冻结", dataIndex: "reserved_cents", width: 120, render: cents },
          { title: "预算", dataIndex: "budget_cents", width: 120, render: cents }
        ]}
      />
      <div className="mobile-ops-list">
        {(wallet.data?.reserved_orders ?? []).map((order) => (
          <section className="mobile-action-card" key={order.order_id}>
            <Typography.Text strong>{order.channel_title || order.order_id}</Typography.Text>
            <Space size={[6, 6]} wrap>
              <Tag>{order.status}</Tag>
              <Tag>冻结 {cents(order.reserved_cents)}</Tag>
              <Tag>预算 {cents(order.budget_cents)}</Tag>
            </Space>
          </section>
        ))}
        {wallet.data?.reserved_orders?.length === 0 ? <Typography.Text type="secondary">暂无冻结订单</Typography.Text> : null}
      </div>
      <Table<WalletTransaction>
        className="desktop-table"
        rowKey="id"
        size="small"
        loading={wallet.isLoading}
        dataSource={wallet.data?.transactions ?? []}
        pagination={false}
        locale={{ emptyText: "暂无流水" }}
        columns={[
          { title: "时间", dataIndex: "created_at", width: 170 },
          { title: "类型", dataIndex: "type", width: 160, render: (value: string) => walletTypeLabels[value] || value },
          { title: "金额", dataIndex: "amount_cents", width: 120, render: cents },
          { title: "备注", dataIndex: "memo", ellipsis: true }
        ]}
      />
      <div className="mobile-ops-list">
        {(wallet.data?.transactions ?? []).map((tx) => (
          <section className="mobile-action-card" key={tx.id}>
            <Space direction="vertical" size={4}>
              <Typography.Text strong>{walletTypeLabels[tx.type] || tx.type}</Typography.Text>
              <Typography.Text>{cents(tx.amount_cents)}</Typography.Text>
              <Typography.Text type="secondary">{tx.memo || tx.created_at}</Typography.Text>
            </Space>
          </section>
        ))}
        {wallet.data?.transactions?.length === 0 ? <Typography.Text type="secondary">暂无流水</Typography.Text> : null}
      </div>
    </Space>
  );
}

function AdvertiserSettingsPanel({ me }: { me: ReturnType<typeof useMe>["data"] }) {
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
