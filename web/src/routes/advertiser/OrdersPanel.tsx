import { App, Button, Space, Table, Tag } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, cents } from "../../shared/api/client";

interface Page<T> {
  items: T[];
}

interface Order {
  id: string;
  status: string;
  channel_title: string;
  budget_cents: number;
  reserved_cents?: number;
  spent_cents: number;
  created_at: string;
}

const statusMeta: Record<string, { label: string; color: string }> = {
  pending_review: { label: "1/4 审核中", color: "gold" },
  approved: { label: "2/4 已排期", color: "cyan" },
  running: { label: "3/4 投放采集中", color: "blue" },
  budget_exhausted: { label: "4/4 已结算完结", color: "green" },
  done: { label: "4/4 已完结", color: "green" },
  paused: { label: "已停止并退回余额", color: "default" },
  rejected: { label: "审核未通过（已退回）", color: "red" }
};

export function OrdersPanel() {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const orders = useQuery({
    queryKey: ["advertiser", "orders"],
    queryFn: () => apiFetch<Page<Order>>("/api/advertiser/orders?limit=50")
  });
  const rebook = useMutation({
    mutationFn: (orderId: string) =>
      apiFetch("/api/advertiser/rebook", {
        method: "POST",
        body: JSON.stringify({ source_order_id: orderId })
      }),
    onSuccess: async () => {
      message.success("已成功一键复投该订单");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["advertiser", "orders"] }),
        queryClient.invalidateQueries({ queryKey: ["advertiser", "dashboard"] })
      ]);
    }
  });
  const pauseOrder = useMutation({
    mutationFn: (orderId: string) =>
      apiFetch(`/api/advertiser/orders/${orderId}/pause`, {
        method: "POST"
      }),
    onSuccess: async () => {
      message.success("已停止订单，未消耗的冻结预算已退回可用余额");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["advertiser", "orders"] }),
        queryClient.invalidateQueries({ queryKey: ["advertiser", "dashboard"] }),
        queryClient.invalidateQueries({ queryKey: ["advertiser", "wallet"] })
      ]);
    }
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
          title: "履约进度",
          dataIndex: "status",
          width: 150,
          render: (value: string) => {
            const meta = statusMeta[value] || { label: value, color: "default" };
            return <Tag color={meta.color}>{meta.label}</Tag>;
          }
        },
        { title: "预算", dataIndex: "budget_cents", width: 110, render: cents },
        { title: "已花费", dataIndex: "spent_cents", width: 110, render: cents },
        { title: "创建时间", dataIndex: "created_at", width: 170 },
        {
          title: "操作",
          key: "actions",
          width: 220,
          render: (_, row) => (
            <Space size={6}>
              <Button
                size="small"
                loading={rebook.isPending}
                onClick={() => rebook.mutate(row.id)}
              >
                一键复投
              </Button>
              {["pending_review", "approved", "running"].includes(row.status) ? (
                <Button
                  size="small"
                  danger
                  loading={pauseOrder.isPending}
                  onClick={() => pauseOrder.mutate(row.id)}
                >
                  停止并退回余额
                </Button>
              ) : null}
            </Space>
          )
        }
      ]}
    />
  );
}
