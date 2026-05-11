import { App, Button, Space, Table, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, cents } from "../../shared/api/client";

interface Page<T> {
  items: T[];
}

interface Plan {
  id: string;
  title: string;
  status: string;
  total_budget_cents: number;
  validation_summary_json: string;
  updated_at: string;
}

interface PlanItem {
  id: string;
  channel_title: string;
  slot_type: string;
  schedule_mode: string;
  frequency_per_day: number;
  unit_price_cents: number;
  estimated_total_cents: number;
  status: string;
  validation_errors_json: string;
}

interface PlanDetail extends Plan {
  creative_text: string | null;
  creative_format: string | null;
  items: PlanItem[];
}

export function PlansPanel() {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const plans = useQuery({
    queryKey: ["advertiser", "plans"],
    queryFn: () => apiFetch<Page<Plan>>("/api/advertiser/plans?limit=50")
  });
  const submitPlan = useMutation({
    mutationFn: (planId: string) =>
      apiFetch(`/api/advertiser/plans/${planId}/submit`, {
        method: "POST",
        body: JSON.stringify({ note: "网页端提交投放计划" })
      }),
    onSuccess: async () => {
      message.success("投放计划已生成订单");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["advertiser", "plans"] }),
        queryClient.invalidateQueries({ queryKey: ["advertiser", "orders"] }),
        queryClient.invalidateQueries({ queryKey: ["advertiser", "dashboard"] })
      ]);
    }
  });
  return (
    <Table<Plan>
      rowKey="id"
      size="small"
      loading={plans.isLoading}
      dataSource={plans.data?.items ?? []}
      pagination={false}
      expandable={{
        expandedRowRender: (record) => <PlanItems planId={record.id} />
      }}
      columns={[
        { title: "计划", dataIndex: "title" },
        { title: "ID", dataIndex: "id", width: 180 },
        {
          title: "状态",
          dataIndex: "status",
          width: 110,
          render: (value) => <Tag color="blue">{value}</Tag>
        },
        {
          title: "预算",
          dataIndex: "total_budget_cents",
          width: 130,
          render: cents
        },
        { title: "更新时间", dataIndex: "updated_at", width: 180 },
        {
          title: "操作",
          width: 150,
          render: (_, record) => (
            <Space>
              <Button
                size="small"
                type="primary"
                disabled={record.status === "submitted" || record.total_budget_cents <= 0}
                loading={submitPlan.isPending}
                onClick={() => submitPlan.mutate(record.id)}
              >
                提交生成订单
              </Button>
            </Space>
          )
        }
      ]}
    />
  );
}

function PlanItems({ planId }: { planId: string }) {
  const detail = useQuery({
    queryKey: ["advertiser", "plans", planId],
    queryFn: () => apiFetch<PlanDetail>(`/api/advertiser/plans/${planId}`)
  });
  return (
    <Space direction="vertical" size={8} className="full-width">
      <Typography.Text type="secondary">
        {detail.data?.creative_text ? `素材：${detail.data.creative_text}` : "尚未绑定素材"}
      </Typography.Text>
      <Table<PlanItem>
        rowKey="id"
        size="small"
        loading={detail.isLoading}
        dataSource={detail.data?.items ?? []}
        pagination={false}
        columns={[
          { title: "频道", dataIndex: "channel_title" },
          { title: "位置", dataIndex: "slot_type", width: 120, render: (value) => <Tag>{value}</Tag> },
          { title: "发布", dataIndex: "schedule_mode", width: 100 },
          { title: "频率/日", dataIndex: "frequency_per_day", width: 90 },
          { title: "单价", dataIndex: "unit_price_cents", width: 110, render: cents },
          { title: "预计", dataIndex: "estimated_total_cents", width: 110, render: cents },
          {
            title: "状态",
            dataIndex: "status",
            width: 100,
            render: (value) => <Tag color={value === "valid" ? "green" : value === "invalid" ? "red" : "blue"}>{value}</Tag>
          },
          {
            title: "校验",
            dataIndex: "validation_errors_json",
            ellipsis: true,
            render: (value) => {
              try {
                const errors = JSON.parse(value || "[]") as string[];
                return errors.join("；") || "-";
              } catch {
                return value || "-";
              }
            }
          }
        ]}
      />
    </Space>
  );
}
