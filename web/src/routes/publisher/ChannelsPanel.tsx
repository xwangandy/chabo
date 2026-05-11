import { useState } from "react";
import { App, Button, Form, InputNumber, Select, Space, Spin, Table, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, cents } from "../../shared/api/client";

interface Page<T> {
  items: T[];
}

interface Channel {
  id: string;
  title: string;
  username: string | null;
  ref_token: string;
  status: string;
}

interface ChannelDetail extends Channel {
  config: {
    daily_ad_limit: number;
  };
  format_policies: Array<{
    format_type: string;
    enabled: number;
    owner_price_band: string;
    platform_promo_enabled: number;
  }>;
  rate_cards: Array<{
    slot_type: string;
    unit_price_cents: number;
    currency: string;
  }>;
  today_ads: number;
  pending_earnings_cents: number;
}

interface PublisherDelivery {
  id: string;
  order_id: string;
  status: string;
  scheduled_at: string;
  sent_at: string | null;
  charge_cents: number;
  publisher_net_cents: number;
  refunded_cents: number;
  creative_text: string;
  advertiser_telegram_user_id: string;
}

export function ChannelsPanel() {
  const [expandedMobileChannel, setExpandedMobileChannel] = useState<string>();
  const channels = useQuery({
    queryKey: ["publisher", "channels"],
    queryFn: () => apiFetch<Page<Channel>>("/api/publisher/channels")
  });

  return (
    <>
      <Table<Channel>
        className="desktop-table"
        rowKey="id"
        size="small"
        loading={channels.isLoading}
        dataSource={channels.data?.items ?? []}
        pagination={false}
        expandable={{
          expandedRowRender: (record) => <ChannelSettings channelId={record.id} />
        }}
        columns={[
          { title: "频道", dataIndex: "title" },
          { title: "用户名", dataIndex: "username", width: 160 },
          { title: "Token", dataIndex: "ref_token", width: 160 },
          {
            title: "状态",
            dataIndex: "status",
            width: 100,
            render: (value) => <Tag>{value}</Tag>
          }
        ]}
      />
      <div className="mobile-channel-list" data-testid="mobile-publisher-channel-list">
        {channels.isLoading ? (
          <Typography.Text type="secondary">加载频道中...</Typography.Text>
        ) : (
          (channels.data?.items ?? []).map((channel) => {
            const expanded = expandedMobileChannel === channel.id;
            return (
              <div className="mobile-channel-card" data-testid="mobile-publisher-channel-card" key={channel.id}>
                <div className="mobile-card-main">
                  <Typography.Text strong>{channel.title}</Typography.Text>
                  <Typography.Text type="secondary">
                    {channel.username ? `@${channel.username}` : channel.ref_token}
                  </Typography.Text>
                </div>
                <Space>
                  <Tag>{channel.status}</Tag>
                  <Button
                    size="small"
                    type={expanded ? "default" : "primary"}
                    onClick={() => setExpandedMobileChannel(expanded ? undefined : channel.id)}
                  >
                    {expanded ? "收起" : "配置"}
                  </Button>
                </Space>
                {expanded ? <ChannelSettings channelId={channel.id} /> : null}
              </div>
            );
          })
        )}
      </div>
    </>
  );
}

function ChannelSettings({ channelId }: { channelId: string }) {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: ["publisher", "channels", channelId],
    queryFn: () => apiFetch<ChannelDetail>(`/api/publisher/channels/${channelId}`)
  });
  const deliveries = useQuery({
    queryKey: ["publisher", "channels", channelId, "deliveries"],
    queryFn: () => apiFetch<Page<PublisherDelivery>>(`/api/publisher/channels/${channelId}/deliveries?limit=20`)
  });
  const dailyLimit = useMutation({
    mutationFn: ({ value }: { value: number }) =>
      apiFetch(`/api/publisher/channels/${channelId}/daily-limit`, {
        method: "PATCH",
        body: JSON.stringify({ daily_ad_limit: value })
      }),
    onSuccess: async () => {
      message.success("每日限制已更新");
      await queryClient.invalidateQueries({ queryKey: ["publisher"] });
    }
  });
  const formatPolicy = useMutation({
    mutationFn: ({
      formatType,
      enabled,
      ownerPriceBand,
      platformPromoEnabled
    }: {
      formatType: string;
      enabled: boolean;
      ownerPriceBand: string;
      platformPromoEnabled: boolean;
    }) =>
      apiFetch(`/api/publisher/channels/${channelId}/format-policy`, {
        method: "PATCH",
        body: JSON.stringify({
          format_type: formatType,
          enabled,
          owner_price_band: ownerPriceBand,
          platform_promo_enabled: platformPromoEnabled
        })
      }),
    onSuccess: async () => {
      message.success("展示形态已更新");
      await queryClient.invalidateQueries({ queryKey: ["publisher"] });
    }
  });
  const rateUpdate = useMutation({
    mutationFn: ({ formatType, price }: { formatType: string; price: number }) =>
      apiFetch(`/api/publisher/channels/${channelId}/rate`, {
        method: "PATCH",
        body: JSON.stringify({
          slot_type: formatType,
          unit_price_cents: Math.round(Number(price) * 100)
        })
      }),
    onSuccess: async () => {
      message.success("刊例价已更新");
      await queryClient.invalidateQueries({ queryKey: ["publisher"] });
    }
  });

  if (detail.isLoading) {
    return <Spin />;
  }
  const channel = detail.data;
  if (!channel) {
    return null;
  }
  const rateMap = new Map(channel.rate_cards.map((item) => [item.slot_type, item]));
  const policyMap = new Map(channel.format_policies.map((item) => [item.format_type, item]));
  const formatRows = [
    ["button_tail", "按钮插播"],
    ["standard_card", "标准插播"],
    ["strong_post", "定制插播"]
  ] as const;

  return (
    <Space direction="vertical" size={12} className="full-width channel-settings">
      <Space wrap>
        <Tag>今日广告 {channel.today_ads}</Tag>
        <Tag>待确认收益 {cents(channel.pending_earnings_cents)}</Tag>
      </Space>
      <Form
        key={channel.config.daily_ad_limit}
        layout="inline"
        initialValues={{ daily_ad_limit: channel.config.daily_ad_limit }}
        onFinish={(values) => dailyLimit.mutate({ value: values.daily_ad_limit })}
      >
        <Form.Item name="daily_ad_limit" label="每日广告上限" rules={[{ required: true }]}>
          <InputNumber min={1} max={24} />
        </Form.Item>
        <Button htmlType="submit" loading={dailyLimit.isPending}>
          保存
        </Button>
      </Form>
      <Space direction="vertical" size={8} className="full-width">
        {formatRows.map(([formatType, label]) => {
          const policy = policyMap.get(formatType);
          const rate = rateMap.get(formatType);
          const enabled = Boolean(policy?.enabled);
          const ownerPriceBand = policy?.owner_price_band ?? "medium";
          const platformPromoEnabled = Boolean(policy?.platform_promo_enabled ?? 1);
          return (
            <div className="channel-policy-row" key={formatType}>
              <Typography.Text strong>{label}</Typography.Text>
              <Form
                key={`${formatType}:${rate?.unit_price_cents ?? 0}`}
                layout="inline"
                initialValues={{ price: (rate?.unit_price_cents ?? 0) / 100 }}
                onFinish={(values) => rateUpdate.mutate({ formatType, price: values.price })}
              >
                <Form.Item name="price" rules={[{ required: true }]}>
                  <InputNumber min={0.01} step={1} addonBefore="USD" style={{ width: 140 }} />
                </Form.Item>
                <Button size="small" htmlType="submit" loading={rateUpdate.isPending}>
                  改价
                </Button>
              </Form>
              <Select
                value={enabled ? "enabled" : "disabled"}
                style={{ width: 120 }}
                options={[
                  { label: "开启", value: "enabled" },
                  { label: "关闭", value: "disabled" }
                ]}
                onChange={(next) =>
                  formatPolicy.mutate({
                    formatType,
                    enabled: next === "enabled",
                    ownerPriceBand,
                    platformPromoEnabled
                  })
                }
              />
              <Select
                value={ownerPriceBand}
                style={{ width: 120 }}
                options={[
                  { label: "低档", value: "low" },
                  { label: "中档", value: "medium" },
                  { label: "高档", value: "high" }
                ]}
                onChange={(next) =>
                  formatPolicy.mutate({
                    formatType,
                    enabled,
                    ownerPriceBand: next,
                    platformPromoEnabled
                  })
                }
              />
            </div>
          );
        })}
      </Space>
      <Table<PublisherDelivery>
        className="desktop-table"
        rowKey="id"
        size="small"
        loading={deliveries.isLoading}
        dataSource={deliveries.data?.items ?? []}
        pagination={false}
        columns={[
          { title: "广告记录", dataIndex: "id", width: 180 },
          {
            title: "状态",
            dataIndex: "status",
            width: 110,
            render: (value) => <Tag>{value}</Tag>
          },
          { title: "广告主", dataIndex: "advertiser_telegram_user_id", width: 120 },
          { title: "文案", dataIndex: "creative_text", ellipsis: true },
          { title: "计划时间", dataIndex: "scheduled_at", width: 170 },
          { title: "收入", dataIndex: "publisher_net_cents", width: 110, render: cents },
          { title: "已退", dataIndex: "refunded_cents", width: 110, render: cents }
        ]}
      />
      <div className="mobile-delivery-list" data-testid="mobile-publisher-delivery-list">
        <Typography.Title level={5}>广告记录</Typography.Title>
        {deliveries.isLoading ? (
          <Typography.Text type="secondary">加载广告记录中...</Typography.Text>
        ) : (deliveries.data?.items ?? []).length ? (
          (deliveries.data?.items ?? []).map((record) => (
            <div className="mobile-action-card" key={record.id}>
              <Typography.Text strong>{record.creative_text}</Typography.Text>
              <Typography.Text type="secondary">{`${record.status}｜${record.advertiser_telegram_user_id}`}</Typography.Text>
              <Space size={4} wrap>
                <Tag>收入 {cents(record.publisher_net_cents)}</Tag>
                <Tag>已退 {cents(record.refunded_cents)}</Tag>
              </Space>
            </div>
          ))
        ) : (
          <Typography.Text type="secondary">暂无广告记录</Typography.Text>
        )}
      </div>
    </Space>
  );
}
