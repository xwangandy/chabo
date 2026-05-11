import { Table, Tag } from "antd";
import { useQuery } from "@tanstack/react-query";
import { apiFetch, cents } from "../../shared/api/client";

interface Page<T> {
  items: T[];
}

interface Order {
  id: string;
  status: string;
  channel_title: string;
  budget_cents: number;
  spent_cents: number;
  created_at: string;
}

export function OrdersPanel() {
  const orders = useQuery({
    queryKey: ["advertiser", "orders"],
    queryFn: () => apiFetch<Page<Order>>("/api/advertiser/orders?limit=50")
  });
  return (
    <Table<Order>
      rowKey="id"
      size="small"
      loading={orders.isLoading}
      dataSource={orders.data?.items ?? []}
      pagination={false}
      columns={[
        { title: "订单", dataIndex: "id", width: 190 },
        { title: "频道", dataIndex: "channel_title" },
        {
          title: "状态",
          dataIndex: "status",
          width: 120,
          render: (value) => <Tag>{value}</Tag>
        },
        { title: "预算", dataIndex: "budget_cents", width: 120, render: cents },
        { title: "已花费", dataIndex: "spent_cents", width: 120, render: cents },
        { title: "创建时间", dataIndex: "created_at", width: 180 }
      ]}
    />
  );
}
