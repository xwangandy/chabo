import { useEffect, useMemo, useState } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef, GridReadyEvent } from "ag-grid-community";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Card, Input, Select, Space, Tag, Typography } from "antd";
import { apiFetch, cents } from "../../shared/api/client";

interface ChannelRow {
  id: string;
  title: string;
  username: string | null;
  ref_token: string;
  score: number | null;
  risk_level: string | null;
  standard_price_cents: number | null;
  light_enabled: boolean;
  standard_enabled: boolean;
  strong_enabled: boolean;
}

interface ChannelPage {
  items: ChannelRow[];
  total: number;
  limit: number;
  offset: number;
}

interface Page<T> {
  items: T[];
}

interface Material {
  id: string;
  format_type: string;
  text: string;
}

interface PlanDraft {
  id: string;
  title: string;
  total_budget_cents: number;
  validation_summary_json: string;
  items: Array<{
    id: string;
    status: string;
    channel_title: string;
    estimated_total_cents: number;
    validation_errors_json: string;
  }>;
}

export function ChannelMarketGrid() {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<ChannelRow[]>([]);
  const [creativeId, setCreativeId] = useState<string>();
  const [slotType, setSlotType] = useState("standard_card");
  const [latestPlan, setLatestPlan] = useState<PlanDraft>();
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["advertiser", "channels", q],
    queryFn: () =>
      apiFetch<ChannelPage>(
        `/api/advertiser/channels?limit=100&offset=0&q=${encodeURIComponent(q)}`
      )
  });
  const materials = useQuery({
    queryKey: ["advertiser", "materials"],
    queryFn: () => apiFetch<Page<Material>>("/api/advertiser/materials?limit=100")
  });

  useEffect(() => {
    const firstMaterialId = materials.data?.items[0]?.id;
    if (!creativeId && firstMaterialId) {
      setCreativeId(firstMaterialId);
    }
  }, [creativeId, materials.data]);

  const columns = useMemo<ColDef<ChannelRow>[]>(
    () => [
      {
        headerName: "频道",
        field: "title",
        minWidth: 220,
        pinned: "left"
      },
      { headerName: "用户名", field: "username", width: 160 },
      { headerName: "评分", field: "score", width: 90, sortable: true },
      { headerName: "风险", field: "risk_level", width: 100 },
      {
        headerName: "标准价",
        field: "standard_price_cents",
        width: 120,
        valueFormatter: ({ value }) => cents(value)
      },
      {
        headerName: "可投位置",
        minWidth: 260,
        valueGetter: ({ data }) =>
          [
            data?.light_enabled ? "文字" : null,
            data?.standard_enabled ? "标准" : null,
            data?.strong_enabled ? "定制" : null
          ]
            .filter(Boolean)
            .join(" / "),
        cellRenderer: ({ value }: { value: string }) => (
          <Space size={4} wrap>
            {value
              .split(" / ")
              .filter(Boolean)
              .map((item) => (
                <Tag key={item}>{item}</Tag>
              ))}
          </Space>
        )
      },
      { headerName: "Token", field: "ref_token", minWidth: 160 }
    ],
    []
  );
  const selectedIds = useMemo(() => new Set(selected.map((item) => item.id)), [selected]);
  const toggleChannel = (channel: ChannelRow) => {
    setSelected((current) =>
      current.some((item) => item.id === channel.id)
        ? current.filter((item) => item.id !== channel.id)
        : [...current, channel]
    );
  };
  const formatLabels = (channel: ChannelRow) =>
    [
      channel.light_enabled ? "文字" : null,
      channel.standard_enabled ? "标准" : null,
      channel.strong_enabled ? "定制" : null
    ].filter(Boolean);
  const addToPlan = useMutation<PlanDraft>({
    mutationFn: async () => {
      const plan = await apiFetch<{ id: string }>("/api/advertiser/plans", {
        method: "POST",
        body: JSON.stringify({
          title: `频道批量投放 ${new Date().toLocaleString()}`,
          creative_id: creativeId
        })
      });
      return apiFetch<PlanDraft>(`/api/advertiser/plans/${plan.id}/items`, {
        method: "POST",
        body: JSON.stringify({
          channel_ids: selected.map((item) => item.id),
          slot_type: slotType,
          schedule_mode: "once"
        })
      });
    },
    onSuccess: async (plan) => {
      message.success("已生成投放计划草稿");
      setLatestPlan(plan);
      setSelected([]);
      await queryClient.invalidateQueries({ queryKey: ["advertiser", "plans"] });
    }
  });
  const submitPlan = useMutation({
    mutationFn: (planId: string) =>
      apiFetch(`/api/advertiser/plans/${planId}/submit`, {
        method: "POST",
        body: JSON.stringify({ note: "H5 摘要确认提交" })
      }),
    onSuccess: async () => {
      message.success("投放计划已提交生成订单");
      setLatestPlan(undefined);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["advertiser", "plans"] }),
        queryClient.invalidateQueries({ queryKey: ["advertiser", "orders"] }),
        queryClient.invalidateQueries({ queryKey: ["advertiser", "dashboard"] })
      ]);
    }
  });

  return (
    <Card
      title="频道市场"
      extra={
        <Space>
          <Input.Search
            allowClear
            placeholder="搜索频道"
            onSearch={(value) => setQ(value)}
            style={{ width: 220 }}
          />
          <Button onClick={() => refetch()}>刷新</Button>
        </Space>
      }
    >
      <div className="grid-toolbar">
        <Space wrap>
          <Typography.Text>已选 {selected.length} 个频道</Typography.Text>
          <Select
            allowClear
            placeholder="选择广告素材"
            value={creativeId}
            loading={materials.isLoading}
            onChange={setCreativeId}
            style={{ width: 260 }}
            options={(materials.data?.items ?? []).map((item) => ({
              label: `${item.format_type}｜${item.text}`,
              value: item.id
            }))}
          />
          <Select
            value={slotType}
            onChange={setSlotType}
            style={{ width: 140 }}
            options={[
              { label: "文字插播", value: "light_tail" },
              { label: "标准插播", value: "standard_card" },
              { label: "定制插播", value: "strong_post" }
            ]}
          />
        </Space>
        <Button
          data-testid="planner-generate-plan"
          type="primary"
          disabled={!selected.length || !creativeId}
          loading={addToPlan.isPending}
          onClick={() => addToPlan.mutate()}
        >
          生成计划
        </Button>
      </div>
      {!materials.isLoading && !materials.data?.items.length ? (
        <Alert
          className="section-alert"
          type="warning"
          showIcon
          message="请先在素材库保存至少一条广告素材，再批量生成投放计划。"
        />
      ) : null}
      {latestPlan ? (
        <MobilePlanSummary
          plan={latestPlan}
          submitting={submitPlan.isPending}
          onSubmit={() => submitPlan.mutate(latestPlan.id)}
        />
      ) : null}
      <div className="ag-theme-quartz ag-theme-quartz-dark channel-grid">
        <AgGridReact<ChannelRow>
          theme="legacy"
          rowData={data?.items ?? []}
          columnDefs={columns}
          loading={isLoading}
          rowSelection={{
            mode: "multiRow",
            checkboxes: true,
            headerCheckbox: true,
            enableClickSelection: false
          } as never}
          animateRows={false}
          getRowId={({ data }) => data.id}
          onGridReady={(event: GridReadyEvent<ChannelRow>) => {
            event.api.sizeColumnsToFit();
          }}
          onSelectionChanged={(event) => {
            setSelected(event.api.getSelectedRows());
          }}
        />
      </div>
      <div className="mobile-channel-list" data-testid="mobile-channel-list">
        {isLoading ? (
          <Typography.Text type="secondary">加载频道中...</Typography.Text>
        ) : (
          (data?.items ?? []).map((channel) => {
            const picked = selectedIds.has(channel.id);
            return (
              <div
                className={`mobile-channel-card ${picked ? "selected" : ""}`}
                data-testid="mobile-channel-card"
                key={channel.id}
              >
                <div className="mobile-card-main">
                  <Typography.Text strong>{channel.title}</Typography.Text>
                  <Typography.Text type="secondary">
                    {channel.username ? `@${channel.username}` : channel.ref_token}
                  </Typography.Text>
                </div>
                <Space size={4} wrap>
                  {formatLabels(channel).map((label) => (
                    <Tag key={label}>{label}</Tag>
                  ))}
                  <Tag>{cents(channel.standard_price_cents)}</Tag>
                  {channel.score != null ? <Tag color="blue">评分 {channel.score}</Tag> : null}
                </Space>
                <Button
                  block
                  data-testid={`mobile-channel-toggle-${channel.id}`}
                  type={picked ? "default" : "primary"}
                  onClick={() => toggleChannel(channel)}
                >
                  {picked ? "取消选择" : "选择频道"}
                </Button>
              </div>
            );
          })
        )}
      </div>
    </Card>
  );
}

function MobilePlanSummary({
  plan,
  submitting,
  onSubmit
}: {
  plan: PlanDraft;
  submitting: boolean;
  onSubmit: () => void;
}) {
  const summary = parsePlanSummary(plan.validation_summary_json);
  const invalidItems = plan.items.filter((item) => item.status === "invalid");
  const canSubmit = summary.valid_count > 0 && summary.invalid_count === 0 && plan.total_budget_cents > 0;
  return (
    <div className="mobile-plan-summary" data-testid="mobile-plan-summary">
      <div className="mobile-card-main">
        <Typography.Text strong>计划摘要</Typography.Text>
        <Typography.Text type="secondary">{plan.title}</Typography.Text>
      </div>
      <div className="summary-grid">
        <div>
          <Typography.Text type="secondary">频道数</Typography.Text>
          <Typography.Title level={4}>{summary.items_count}</Typography.Title>
        </div>
        <div>
          <Typography.Text type="secondary">有效</Typography.Text>
          <Typography.Title level={4}>{summary.valid_count}</Typography.Title>
        </div>
        <div>
          <Typography.Text type="secondary">无效</Typography.Text>
          <Typography.Title level={4}>{summary.invalid_count}</Typography.Title>
        </div>
        <div>
          <Typography.Text type="secondary">预算</Typography.Text>
          <Typography.Title level={4}>{cents(plan.total_budget_cents)}</Typography.Title>
        </div>
      </div>
      {invalidItems.length ? (
        <Alert
          type="error"
          showIcon
          message="计划存在无效频道"
          description={invalidItems.map((item) => `${item.channel_title}: ${parseErrors(item.validation_errors_json)}`).join("；")}
        />
      ) : null}
      <Button
        block
        data-testid="mobile-plan-submit"
        type="primary"
        disabled={!canSubmit}
        loading={submitting}
        onClick={onSubmit}
      >
        确认提交并生成订单
      </Button>
    </div>
  );
}

function parsePlanSummary(raw: string) {
  try {
    const parsed = JSON.parse(raw || "{}") as Partial<{
      items_count: number;
      valid_count: number;
      invalid_count: number;
      valid_total_cents: number;
    }>;
    return {
      items_count: parsed.items_count ?? 0,
      valid_count: parsed.valid_count ?? 0,
      invalid_count: parsed.invalid_count ?? 0,
      valid_total_cents: parsed.valid_total_cents ?? 0
    };
  } catch {
    return { items_count: 0, valid_count: 0, invalid_count: 0, valid_total_cents: 0 };
  }
}

function parseErrors(raw: string) {
  try {
    const errors = JSON.parse(raw || "[]") as string[];
    return errors.join("、") || "未知错误";
  } catch {
    return raw || "未知错误";
  }
}
