import { useState } from "react";
import { Alert, App, Button, Card, Col, Row, Space, Table, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API_BASE, apiFetch, cents } from "../../shared/api/client";
import { MetricCard } from "../../shared/components/MetricCard";

interface ChannelBreakdownRow {
  channel_id: string;
  title: string;
  orders_count: number;
  spent_cents: number;
  charged_cents: number;
  impressions_count: number;
  clicks_count: number;
  cpm_cents: number | null;
  cpc_cents: number | null;
}

interface CheckpointPoint {
  checkpoint_label: string;
  views_count: number;
  forwards_count: number;
}

interface AlternativeChannel {
  channel_id: string;
  title: string;
  ref_token: string;
  score: number;
}

interface RecommendationItem {
  channel_id: string;
  channel_title: string;
  action_type: "scale_up" | "replace_creative" | "reduce_frequency" | "pause_channel";
  action_label: string;
  reason: string;
  spent_cents: number;
  impressions_count: number;
  clicks_count: number;
  cpc_cents: number | null;
  source_order_id: string | null;
  alternative_channels?: AlternativeChannel[];
}

interface AdvertiserReportResponse {
  orders_count: number;
  total_budget_cents: number;
  reserved_cents: number;
  spent_cents: number;
  deliveries_count: number;
  sent_count: number;
  charged_cents: number;
  impressions_count: number;
  clicks_count: number;
  cpm_cents: number | null;
  cpc_cents: number | null;
  bot_starts: number;
  checkpoint_curve?: CheckpointPoint[];
  by_channel: ChannelBreakdownRow[];
  recommendations: RecommendationItem[];
}

interface ShareTokenResponse {
  share_token: string;
  expires_at: string;
  share_path: string;
}

const actionTagColor: Record<string, string> = {
  scale_up: "green",
  replace_creative: "gold",
  reduce_frequency: "blue",
  pause_channel: "red"
};

export function ReportPanel() {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [shareUrl, setShareUrl] = useState<string>();

  const report = useQuery({
    queryKey: ["advertiser", "report"],
    queryFn: () => apiFetch<AdvertiserReportResponse>("/api/advertiser/report")
  });

  const createShareLink = useMutation({
    mutationFn: () =>
      apiFetch<ShareTokenResponse>("/api/advertiser/reports/share-token", {
        method: "POST"
      }),
    onSuccess: (res) => {
      const fullUrl = `${API_BASE || window.location.origin}${res.share_path}`;
      setShareUrl(fullUrl);
      message.success("已生成 7 天有效期的只读战报分享链接");
    }
  });

  const rebook = useMutation({
    mutationFn: (sourceOrderId: string) =>
      apiFetch("/api/advertiser/rebook", {
        method: "POST",
        body: JSON.stringify({ source_order_id: sourceOrderId })
      }),
    onSuccess: async () => {
      message.success("已成功一键复投并创建新订单");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["advertiser", "orders"] }),
        queryClient.invalidateQueries({ queryKey: ["advertiser", "report"] }),
        queryClient.invalidateQueries({ queryKey: ["advertiser", "dashboard"] })
      ]);
    }
  });

  const data = report.data;

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Card
        title="投放效果归因总览（双轨防刷去重）"
        extra={
          <Button
            type="primary"
            loading={createShareLink.isPending}
            onClick={() => createShareLink.mutate()}
          >
            生成只读分享战报链接
          </Button>
        }
      >
        <Space direction="vertical" size={12} className="full-width">
          {shareUrl ? (
            <Alert
              type="success"
              showIcon
              message="外部只读分享链接（已隐去账户余额与隐私字段）"
              description={<Typography.Text copyable>{shareUrl}</Typography.Text>}
            />
          ) : null}
          <Row gutter={[12, 12]}>
            <Col xs={12} md={4}>
              <MetricCard title="累计花费" value={cents(data?.spent_cents ?? 0)} />
            </Col>
            <Col xs={12} md={4}>
              <MetricCard title="已发送场次" value={data?.sent_count ?? 0} />
            </Col>
            <Col xs={12} md={4}>
              <MetricCard title="真实采样浏览" value={data?.impressions_count ?? 0} />
            </Col>
            <Col xs={12} md={4}>
              <MetricCard title="去重有效点击" value={data?.clicks_count ?? 0} />
            </Col>
            <Col xs={12} md={4}>
              <MetricCard
                title="千次曝光成本 (CPM)"
                value={data?.cpm_cents != null ? cents(data.cpm_cents) : "待采样"}
              />
            </Col>
            <Col xs={12} md={4}>
              <MetricCard
                title="单次点击成本 (CPC)"
                value={data?.cpc_cents != null ? cents(data.cpc_cents) : "待点击"}
              />
            </Col>
          </Row>
          {data?.checkpoint_curve?.length ? (
            <Space size={[8, 8]} wrap>
              <Typography.Text type="secondary">多时间点曝光衰减采样曲线：</Typography.Text>
              {data.checkpoint_curve.map((pt) => (
                <Tag key={pt.checkpoint_label} color="processing">
                  {`${pt.checkpoint_label}：${pt.views_count} 浏览 / ${pt.forwards_count} 转发`}
                </Tag>
              ))}
            </Space>
          ) : null}
        </Space>
      </Card>

      <Card title="AI 投后四象限诊断与一键复投建议">
        <Table<RecommendationItem>
          rowKey="channel_id"
          size="small"
          loading={report.isLoading}
          dataSource={data?.recommendations ?? []}
          pagination={false}
          locale={{ emptyText: "完成首单投放后自动生成四象限优化建议" }}
          columns={[
            { title: "频道", dataIndex: "channel_title", width: 180 },
            {
              title: "诊断建议",
              dataIndex: "action_label",
              width: 120,
              render: (val: string, row) => (
                <Tag color={actionTagColor[row.action_type] || "default"}>{val}</Tag>
              )
            },
            {
              title: "归因依据与替补推荐",
              dataIndex: "reason",
              render: (val: string, row) => (
                <Space direction="vertical" size={2}>
                  <Typography.Text>{val}</Typography.Text>
                  {row.alternative_channels?.length ? (
                    <Space size={4} wrap>
                      <Typography.Text type="secondary">同赛道高分替补：</Typography.Text>
                      {row.alternative_channels.map((alt) => (
                        <Tag key={alt.channel_id} color="cyan">{`${alt.title} (${alt.score}分)`}</Tag>
                      ))}
                    </Space>
                  ) : null}
                </Space>
              )
            },
            {
              title: "有效点击",
              dataIndex: "clicks_count",
              width: 100
            },
            {
              title: "CPC",
              dataIndex: "cpc_cents",
              width: 110,
              render: (val: number | null) => (val != null ? cents(val) : "—")
            },
            {
              title: "操作",
              key: "action",
              width: 130,
              render: (_, row) =>
                row.source_order_id ? (
                  <Button
                    size="small"
                    type={row.action_type === "scale_up" ? "primary" : "default"}
                    loading={rebook.isPending}
                    onClick={() => rebook.mutate(row.source_order_id!)}
                  >
                    一键复投
                  </Button>
                ) : null
            }
          ]}
        />
      </Card>

      <Card title="分频道效果明细（全量免费开放）">
        <Table<ChannelBreakdownRow>
          rowKey="channel_id"
          size="small"
          loading={report.isLoading}
          dataSource={data?.by_channel ?? []}
          pagination={false}
          locale={{ emptyText: "暂无分频道投放记录" }}
          columns={[
            { title: "频道", dataIndex: "title" },
            { title: "订单数", dataIndex: "orders_count", width: 90 },
            { title: "实际花费", dataIndex: "spent_cents", width: 120, render: cents },
            { title: "采样浏览量", dataIndex: "impressions_count", width: 120 },
            { title: "有效点击", dataIndex: "clicks_count", width: 110 },
            {
              title: "CPM",
              dataIndex: "cpm_cents",
              width: 110,
              render: (v: number | null) => (v != null ? cents(v) : "—")
            },
            {
              title: "CPC",
              dataIndex: "cpc_cents",
              width: 110,
              render: (v: number | null) => (v != null ? cents(v) : "—")
            }
          ]}
        />
      </Card>
    </Space>
  );
}
