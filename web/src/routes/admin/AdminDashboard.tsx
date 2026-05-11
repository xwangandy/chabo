import { useEffect, useState, type ReactNode } from "react";
import { DownloadOutlined, EyeOutlined, SafetyCertificateOutlined } from "@ant-design/icons";
import { App, Button, Checkbox, Col, DatePicker, Descriptions, Drawer, Form, Input, InputNumber, List, Row, Segmented, Space, Table, Tabs, Tag, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { API_BASE, apiFetch, cents, type Portal } from "../../shared/api/client";
import { MetricCard } from "../../shared/components/MetricCard";
import { useHashTab } from "../../shared/hooks/useHashTab";

interface AdminSummary {
  metrics: Record<string, number>;
}

interface Page<T> {
  items: T[];
  total: number;
  limit?: number;
  offset?: number;
  retention_days?: number;
  chain_head?: string | null;
}

interface AdminOrder {
  id: string;
  status: string;
  budget_cents: number;
  reserved_cents: number;
  spent_cents: number;
  channel_title: string;
  advertiser_telegram_user_id: string;
  created_at: string;
}

interface TopupRequest {
  id: string;
  status: string;
  recipient_telegram_user_id: string;
  amount_cents: number;
  reason: string;
  requester_telegram_user_id: string;
  created_at: string;
}

interface AdminLedgerTransaction {
  id: string;
  type: string;
  currency: string;
  amount_cents: number;
  memo: string | null;
  created_at: string;
  order_id: string | null;
  delivery_id: string | null;
  account_telegram_user_id: string | null;
  account_display_name: string | null;
  related_telegram_user_id: string | null;
}

interface AdminWalletAccount {
  id: string;
  telegram_user_id: string | null;
  display_name: string | null;
  role: string;
  available_balance_cents: number;
  reserved_balance_cents: number;
  spent_balance_cents: number;
  pending_earnings_cents: number;
  confirmed_earnings_cents: number;
  releasable_earnings_cents: number;
  updated_at: string;
}

interface AdminWalletResponse {
  totals: {
    available_cents: number;
    reserved_cents: number;
    spent_cents: number;
    pending_earnings_cents: number;
    confirmed_earnings_cents: number;
    releasable_earnings_cents: number;
  };
  topups_by_status: Array<{ status: string; count: number; amount_cents: number }>;
  recent_ledger: AdminLedgerTransaction[];
  accounts: AdminWalletAccount[];
  limit: number;
}

interface Delivery {
  id: string;
  status: string;
  channel_title: string;
  charge_cents: number;
  refunded_cents: number;
  scheduled_at: string;
  advertiser_telegram_user_id: string;
}

interface Dispute {
  id: string;
  status: string;
  channel_title: string;
  reason: string;
  created_at: string;
}

interface AdminAccount {
  id: string;
  telegram_user_id: string | null;
  role: string;
  display_name: string | null;
  available_balance_cents: number;
  reserved_balance_cents: number;
  pending_earnings_cents: number;
  admin_portal_status: string | null;
  advertiser_portal_status: string | null;
  publisher_portal_status: string | null;
  admin_level: string | null;
}

interface AdminChannel {
  id: string;
  title: string;
  username: string | null;
  ref_token: string;
  owner_telegram_user_id: string;
  daily_ad_limit: number;
  deliveries_count: number;
  pending_earnings_cents: number;
}

interface AuditLog {
  id: string;
  action: string;
  entity_type: string;
  entity_id: string;
  actor_telegram_user_id: string | null;
  actor_account_id: string | null;
  target_telegram_user_id: string | null;
  target_display_name: string | null;
  created_at: string;
  payload_json: string;
  previous_hash?: string | null;
  audit_hash?: string | null;
  hash_version?: string | null;
}

interface AuditDiffRow {
  field: string;
  before: unknown;
  after: unknown;
}

interface AuditIntegrityReport {
  ok: boolean;
  strict_unsigned: boolean;
  checked_rows: number;
  signed_rows: number;
  unsigned_rows: number;
  unsigned_after_chain_started: number;
  invalid_hashes: number;
  broken_links: number;
  first_signed_hash: string | null;
  chain_head: string | null;
  created_from: string | null;
  created_to: string | null;
  issue_count: number;
  truncated_issues: number;
  issues: Array<Record<string, unknown>>;
}

interface AdminSettingsResponse {
  environment: string;
  public_base_url: string;
  web_allowed_origins: string[];
  session_cookie_secure: boolean;
  session_cookie_samesite: string;
  dev_auth_bypass: boolean;
  dev_session_enabled: boolean;
  audit_retention_days: number;
  audit_export_max_rows: number;
  magic_link_ttl_seconds: number;
  impersonation_session_ttl_seconds: number;
  database: {
    path: string;
    backup_dir: string;
    last_backup: { path: string; size_bytes: number } | null;
    migrations: number;
  };
  audit: {
    total: number;
    signed: number;
    unsigned: number;
    first_at: string | null;
    last_at: string | null;
  };
  release_gates: Array<{ key: string; ok: boolean; label: string }>;
}

interface TimelineEvent {
  kind: string;
  at: string;
  title: string;
  entity: string;
  actor: string;
  note: string;
  summary: string;
}

type DetailTarget = { type: "order" | "delivery" | "dispute"; id: string };
type AdminLevel = "viewer" | "operator" | "finance" | "super_admin";
type PortalAccessStatus = "candidate" | "active" | "suspended" | "revoked";
type AuditCategory = "all" | "permission" | "impersonation" | "admin_level";

const adminLevelLabels: Record<AdminLevel, string> = {
  viewer: "只读",
  operator: "运营",
  finance: "财务",
  super_admin: "超级管理员"
};

const userPortalLabels: Record<Exclude<Portal, "admin">, string> = {
  advertiser: "广告主端",
  publisher: "频道主端"
};

const auditCategoryOptions: Array<{ label: string; value: AuditCategory }> = [
  { label: "全部", value: "all" },
  { label: "权限调整", value: "permission" },
  { label: "代看", value: "impersonation" },
  { label: "管理员等级", value: "admin_level" }
];
const adminTabKeys = ["orders", "topups", "wallet", "deliveries", "disputes", "accounts", "channels", "audit", "settings"] as const;
const permissionConfirmPhrase = "确认调整权限";

export function AdminDashboard() {
  const { message, modal } = App.useApp();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [topupForm] = Form.useForm<{ recipient_telegram_user_id: string; amount: number; reason: string; request_note?: string }>();
  const [detailTarget, setDetailTarget] = useState<DetailTarget | null>(null);
  const [permissionTarget, setPermissionTarget] = useState<AdminAccount | null>(null);
  const [impersonationTarget, setImpersonationTarget] = useState<{
    account: AdminAccount;
    portal: Exclude<Portal, "admin">;
  } | null>(null);
  const [auditDetail, setAuditDetail] = useState<AuditLog | null>(null);
  const [adminSearch, setAdminSearch] = useState("");
  const [auditSearch, setAuditSearch] = useState("");
  const [auditActor, setAuditActor] = useState("");
  const [auditTarget, setAuditTarget] = useState("");
  const [auditDateRange, setAuditDateRange] = useState<[string, string] | null>(null);
  const [auditCategory, setAuditCategory] = useState<AuditCategory>("all");
  const [auditPage, setAuditPage] = useState(1);
  const [auditPageSize, setAuditPageSize] = useState(50);
  const [auditIntegrity, setAuditIntegrity] = useState<AuditIntegrityReport | null>(null);
  const [auditVerifyStrict, setAuditVerifyStrict] = useState(false);
  const [activeTab, setActiveTab] = useHashTab("orders", adminTabKeys);
  const buildAuditParams = (limit = String(auditPageSize), offset = String((auditPage - 1) * auditPageSize)) => {
    const params = new URLSearchParams({ limit, offset, q: auditSearch });
    if (auditCategory !== "all") {
      params.set("category", auditCategory);
    }
    if (auditActor.trim()) {
      params.set("actor", auditActor.trim());
    }
    if (auditTarget.trim()) {
      params.set("target", auditTarget.trim());
    }
    if (auditDateRange) {
      params.set("created_from", `${auditDateRange[0]} 00:00:00`);
      params.set("created_to", `${auditDateRange[1]} 23:59:59`);
    }
    return params;
  };
  const buildAuditVerifyParams = () => {
    const params = new URLSearchParams();
    if (auditDateRange) {
      params.set("created_from", `${auditDateRange[0]} 00:00:00`);
      params.set("created_to", `${auditDateRange[1]} 23:59:59`);
    }
    if (auditVerifyStrict) {
      params.set("strict_unsigned", "true");
    }
    return params;
  };
  useEffect(() => {
    setAuditPage(1);
  }, [auditSearch, auditCategory, auditActor, auditTarget, auditDateRange]);
  const { data, isLoading } = useQuery({
    queryKey: ["admin", "summary"],
    queryFn: () => apiFetch<AdminSummary>("/api/admin/summary")
  });
  const orders = useQuery({
    queryKey: ["admin", "orders"],
    queryFn: () =>
      apiFetch<Page<AdminOrder>>("/api/admin/orders?limit=50")
  });
  const topups = useQuery({
    queryKey: ["admin", "topups", "pending"],
    queryFn: () =>
      apiFetch<Page<TopupRequest>>("/api/admin/topups?status=pending&limit=50")
  });
  const adminWallet = useQuery({
    queryKey: ["admin", "wallet"],
    queryFn: () => apiFetch<AdminWalletResponse>("/api/admin/wallet?limit=20")
  });
  const deliveries = useQuery({
    queryKey: ["admin", "deliveries"],
    queryFn: () => apiFetch<Page<Delivery>>("/api/admin/deliveries?limit=50")
  });
  const disputes = useQuery({
    queryKey: ["admin", "disputes", "open"],
    queryFn: () => apiFetch<Page<Dispute>>("/api/admin/disputes?status=open&limit=50")
  });
  const accounts = useQuery({
    queryKey: ["admin", "accounts", adminSearch],
    queryFn: () =>
      apiFetch<Page<AdminAccount>>(`/api/admin/accounts?limit=50&q=${encodeURIComponent(adminSearch)}`)
  });
  const channels = useQuery({
    queryKey: ["admin", "channels", adminSearch],
    queryFn: () =>
      apiFetch<Page<AdminChannel>>(`/api/admin/channels?limit=50&q=${encodeURIComponent(adminSearch)}`)
  });
  const auditLogs = useQuery({
    queryKey: ["admin", "audit-logs", auditSearch, auditCategory, auditActor, auditTarget, auditDateRange, auditPage, auditPageSize],
    queryFn: () => apiFetch<Page<AuditLog>>(`/api/admin/audit-logs?${buildAuditParams().toString()}`)
  });
  const adminSettings = useQuery({
    queryKey: ["admin", "settings"],
    queryFn: () => apiFetch<AdminSettingsResponse>("/api/admin/settings")
  });
  const detail = useQuery({
    queryKey: ["admin", "detail", detailTarget],
    enabled: Boolean(detailTarget),
    queryFn: () => {
      if (!detailTarget) {
        throw new Error("missing detail target");
      }
      const path =
        detailTarget.type === "order"
          ? `/api/admin/orders/${detailTarget.id}`
          : detailTarget.type === "delivery"
            ? `/api/admin/deliveries/${detailTarget.id}`
            : `/api/admin/disputes/${detailTarget.id}`;
      return apiFetch<Record<string, unknown>>(path);
    }
  });
  const refreshAdmin = async () => {
    await queryClient.invalidateQueries({ queryKey: ["admin"] });
  };
  const approveOrder = useMutation({
    mutationFn: (orderId: string) =>
      apiFetch(`/api/admin/orders/${orderId}/approve`, {
        method: "POST",
        body: JSON.stringify({ note: "网页端审核通过" })
      }),
    onSuccess: async () => {
      message.success("订单已通过");
      await refreshAdmin();
    }
  });
  const rejectOrder = useMutation({
    mutationFn: (orderId: string) =>
      apiFetch(`/api/admin/orders/${orderId}/reject`, {
        method: "POST",
        body: JSON.stringify({ reason: "网页端审核拒绝", note: "网页端拒绝订单" })
      }),
    onSuccess: async () => {
      message.success("订单已拒绝");
      await refreshAdmin();
    }
  });
  const approveTopup = useMutation({
    mutationFn: (requestId: string) =>
      apiFetch(`/api/admin/topups/${requestId}/approve`, {
        method: "POST",
        body: JSON.stringify({ note: "网页端复核通过" })
      }),
    onSuccess: async () => {
      message.success("入账已通过");
      await refreshAdmin();
    }
  });
  const createTopup = useMutation({
    mutationFn: (values: { recipient_telegram_user_id: string; amount: number; reason: string; request_note?: string }) =>
      apiFetch("/api/admin/topups", {
        method: "POST",
        body: JSON.stringify({
          recipient_telegram_user_id: values.recipient_telegram_user_id,
          amount_cents: Math.round(Number(values.amount) * 100),
          reason: values.reason,
          request_note: values.request_note
        })
      }),
    onSuccess: async () => {
      message.success("入账申请已创建，等待另一位管理员复核");
      topupForm.resetFields();
      await refreshAdmin();
    }
  });
  const rejectTopup = useMutation({
    mutationFn: (requestId: string) =>
      apiFetch(`/api/admin/topups/${requestId}/reject`, {
        method: "POST",
        body: JSON.stringify({ note: "网页端复核拒绝" })
      }),
    onSuccess: async () => {
      message.success("入账已拒绝");
      await refreshAdmin();
    }
  });
  const refundDelivery = useMutation({
    mutationFn: (deliveryId: string) =>
      apiFetch(`/api/admin/deliveries/${deliveryId}/refund`, {
        method: "POST",
        body: JSON.stringify({ reason: "网页端运营退款", note: "网页端退款" })
      }),
    onSuccess: async () => {
      message.success("投放已退款");
      await refreshAdmin();
    }
  });
  const reportDeletion = useMutation({
    mutationFn: (deliveryId: string) =>
      apiFetch(`/api/admin/deliveries/${deliveryId}/report-deletion`, {
        method: "POST",
        body: JSON.stringify({ note: "网页端举报频道主提前删除广告" })
      }),
    onSuccess: async () => {
      message.success("已按删帖异常处理并退款");
      await refreshAdmin();
    }
  });
  const resolveDispute = useMutation({
    mutationFn: (disputeId: string) =>
      apiFetch(`/api/admin/disputes/${disputeId}/resolve`, {
        method: "POST",
        body: JSON.stringify({ resolution: "网页端运营裁决完成", note: "网页端裁决" })
      }),
    onSuccess: async () => {
      message.success("争议已裁决");
      await refreshAdmin();
    }
  });
  const startImpersonation = useMutation({
    mutationFn: (payload: { target_account_id: string; portal: Exclude<Portal, "admin">; reason: string }) =>
      apiFetch<{ portals: Portal[] }>("/api/admin/impersonations", {
        method: "POST",
        body: JSON.stringify({
          target_account_id: payload.target_account_id,
          portal: payload.portal,
          reason: payload.reason
        })
      }),
    onSuccess: async (_data, variables) => {
      message.success("已进入代看模式");
      setImpersonationTarget(null);
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      navigate(`/${variables.portal}`);
    },
    onError: (error) => message.error(error instanceof Error ? error.message : String(error))
  });
  const updatePortalAccess = useMutation({
    mutationFn: (payload: {
      account_id: string;
      portal: Portal;
      status: PortalAccessStatus;
      reason: string;
      admin_level?: AdminLevel;
      confirm_phrase?: string;
    }) =>
      apiFetch(`/api/admin/accounts/${payload.account_id}/portals/${payload.portal}`, {
        method: "PUT",
        body: JSON.stringify({
          status: payload.status,
          reason: payload.reason,
          admin_level: payload.admin_level,
          confirm_phrase: payload.confirm_phrase
        })
      }),
    onSuccess: async () => {
      message.success("权限已更新");
      setPermissionTarget(null);
      await queryClient.invalidateQueries({ queryKey: ["admin", "accounts"] });
      await queryClient.invalidateQueries({ queryKey: ["admin", "audit-logs"] });
      await queryClient.invalidateQueries({ queryKey: ["me"] });
    },
    onError: (error) => message.error(error instanceof Error ? error.message : String(error))
  });
  const confirmPermissionUpdate = (payload: {
    account_id: string;
    portal: Portal;
    status: PortalAccessStatus;
    reason: string;
    admin_level?: AdminLevel;
    confirm_phrase?: string;
  }, summary: string) => {
    modal.confirm({
      title: "确认权限调整",
      content: (
        <Space direction="vertical" size={6}>
          <Typography.Text>{summary}</Typography.Text>
          <Typography.Text type="secondary">{payload.reason}</Typography.Text>
        </Space>
      ),
      okText: "确认执行",
      cancelText: "取消",
      okButtonProps: { danger: payload.status === "revoked" || payload.portal === "admin" },
      onOk: () => updatePortalAccess.mutate(payload)
    });
  };
  const exportAuditCsv = async () => {
    const response = await fetch(`${API_BASE}/api/admin/audit-logs/export.csv?${buildAuditParams("5000", "0").toString()}`, {
      credentials: "include"
    });
    if (!response.ok) {
      message.error(`导出失败：${response.status}`);
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `chabo-audit-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    const digest = response.headers.get("X-Chabo-Audit-Export-Sha256");
    message.success(digest ? `审计 CSV 已导出，签名 ${digest.slice(0, 12)}...` : "审计 CSV 已导出");
  };
  const verifyAuditChain = useMutation({
    mutationFn: () => {
      const params = buildAuditVerifyParams().toString();
      return apiFetch<AuditIntegrityReport>(`/api/admin/audit-logs/verify${params ? `?${params}` : ""}`);
    },
    onSuccess: (report) => {
      setAuditIntegrity(report);
      if (report.ok) {
        message.success("审计链校验通过");
      } else {
        message.warning("审计链存在异常，请查看报告");
      }
    },
    onError: (error) => message.error(error instanceof Error ? error.message : String(error))
  });
  const metrics = data?.metrics ?? {};
  const urgentOrders = (orders.data?.items ?? []).filter((item) => ["pending_review", "paused"].includes(item.status)).slice(0, 5);
  const actionableDeliveries = (deliveries.data?.items ?? []).filter((item) => ["sent", "disputed"].includes(item.status)).slice(0, 5);
  const openDisputes = (disputes.data?.items ?? []).filter((item) => item.status === "open").slice(0, 5);

  return (
    <section className="page-stack">
      <Typography.Title level={2}>管理端</Typography.Title>
      <Row gutter={[12, 12]}>
        <Col xs={12} lg={6}>
          <MetricCard title="待审核订单" value={metrics.pending_review_orders ?? 0} />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="到期投放" value={metrics.scheduled_due ?? 0} />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="Open 争议" value={metrics.open_disputes ?? 0} />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="待审入账" value={metrics.pending_topups ?? 0} />
        </Col>
      </Row>
      <div className="mobile-ops-list" data-testid="mobile-admin-ops-list">
        <MobileOpsSection title="待审核订单">
          {urgentOrders.map((record) => (
            <div className="mobile-action-card" key={record.id}>
              <Typography.Text strong>{record.channel_title}</Typography.Text>
              <Typography.Text type="secondary">{`${record.advertiser_telegram_user_id}｜${cents(record.budget_cents)}`}</Typography.Text>
              <Space>
                <Button size="small" onClick={() => setDetailTarget({ type: "order", id: record.id })}>
                  详情
                </Button>
                <Button type="primary" size="small" loading={approveOrder.isPending} onClick={() => approveOrder.mutate(record.id)}>
                  通过
                </Button>
                <Button size="small" danger loading={rejectOrder.isPending} onClick={() => rejectOrder.mutate(record.id)}>
                  拒绝
                </Button>
              </Space>
            </div>
          ))}
        </MobileOpsSection>
        <MobileOpsSection title="待审入账">
          {(topups.data?.items ?? []).slice(0, 5).map((record) => (
            <div className="mobile-action-card" key={record.id}>
              <Typography.Text strong>{record.recipient_telegram_user_id}</Typography.Text>
              <Typography.Text type="secondary">{`${cents(record.amount_cents)}｜${record.reason}`}</Typography.Text>
              <Space>
                <Button type="primary" size="small" loading={approveTopup.isPending} onClick={() => approveTopup.mutate(record.id)}>
                  通过
                </Button>
                <Button size="small" danger loading={rejectTopup.isPending} onClick={() => rejectTopup.mutate(record.id)}>
                  拒绝
                </Button>
              </Space>
            </div>
          ))}
        </MobileOpsSection>
        <MobileOpsSection title="投放异常">
          {actionableDeliveries.map((record) => (
            <div className="mobile-action-card" key={record.id}>
              <Typography.Text strong>{record.channel_title}</Typography.Text>
              <Typography.Text type="secondary">{`${record.status}｜已退 ${cents(record.refunded_cents)}`}</Typography.Text>
              <Space>
                <Button size="small" onClick={() => setDetailTarget({ type: "delivery", id: record.id })}>
                  详情
                </Button>
                <Button size="small" danger loading={refundDelivery.isPending} onClick={() => refundDelivery.mutate(record.id)}>
                  退款
                </Button>
                <Button size="small" loading={reportDeletion.isPending} onClick={() => reportDeletion.mutate(record.id)}>
                  删帖
                </Button>
              </Space>
            </div>
          ))}
        </MobileOpsSection>
        <MobileOpsSection title="争议">
          {openDisputes.map((record) => (
            <div className="mobile-action-card" key={record.id}>
              <Typography.Text strong>{record.channel_title}</Typography.Text>
              <Typography.Text type="secondary">{record.reason}</Typography.Text>
              <Space>
                <Button size="small" onClick={() => setDetailTarget({ type: "dispute", id: record.id })}>
                  详情
                </Button>
                <Button size="small" type="primary" loading={resolveDispute.isPending} onClick={() => resolveDispute.mutate(record.id)}>
                  裁决完成
                </Button>
              </Space>
            </div>
          ))}
        </MobileOpsSection>
        <MobileOpsSection title="钱包总览">
          <div className="mobile-action-card">
            <Typography.Text strong>资金池</Typography.Text>
            <Space size={[6, 6]} wrap>
              <Tag>可用 {cents(adminWallet.data?.totals.available_cents ?? 0)}</Tag>
              <Tag>冻结 {cents(adminWallet.data?.totals.reserved_cents ?? 0)}</Tag>
              <Tag>待确认收益 {cents(adminWallet.data?.totals.pending_earnings_cents ?? 0)}</Tag>
            </Space>
          </div>
          {(adminWallet.data?.recent_ledger ?? []).slice(0, 3).map((record) => (
            <div className="mobile-action-card" key={record.id}>
              <Typography.Text strong>{record.account_display_name || record.account_telegram_user_id || record.id}</Typography.Text>
              <Typography.Text type="secondary">{`${record.type}｜${cents(record.amount_cents)}｜${record.memo || record.created_at}`}</Typography.Text>
            </div>
          ))}
        </MobileOpsSection>
        <MobileOpsSection title="上线设置">
          <div className="mobile-action-card">
            <Typography.Text strong>{adminSettings.data?.environment ?? "local"}</Typography.Text>
            <Space size={[6, 6]} wrap>
              {(adminSettings.data?.release_gates ?? []).map((gate) => (
                <Tag key={gate.key} color={gate.ok ? "green" : "red"}>{gate.label}</Tag>
              ))}
            </Space>
          </div>
        </MobileOpsSection>
      </div>
      <Tabs
        className="desktop-admin-tabs"
        activeKey={activeTab}
        onChange={(key) => setActiveTab(key as (typeof adminTabKeys)[number])}
        items={[
          {
            key: "orders",
            label: "订单",
            children: (
              <Table<AdminOrder>
                rowKey="id"
                loading={orders.isLoading || isLoading}
                size="small"
                dataSource={orders.data?.items ?? []}
                pagination={false}
                columns={[
                  { title: "订单", dataIndex: "id", width: 190 },
                  { title: "频道", dataIndex: "channel_title" },
                  { title: "广告主", dataIndex: "advertiser_telegram_user_id", width: 120 },
                  {
                    title: "预算",
                    dataIndex: "budget_cents",
                    width: 120,
                    render: cents
                  },
                  {
                    title: "状态",
                    dataIndex: "status",
                    width: 120,
                    render: (value) => <Tag color="blue">{value}</Tag>
                  },
                  {
                    title: "操作",
                    width: 220,
                    render: (_, record) => (
                      <Space>
                        <Button size="small" onClick={() => setDetailTarget({ type: "order", id: record.id })}>
                          详情
                        </Button>
                        <Button
                          type="primary"
                          size="small"
                          disabled={!["pending_review", "paused"].includes(record.status)}
                          loading={approveOrder.isPending}
                          onClick={() => approveOrder.mutate(record.id)}
                        >
                          通过
                        </Button>
                        <Button
                          size="small"
                          danger
                          disabled={!["pending_review", "approved", "paused"].includes(record.status)}
                          loading={rejectOrder.isPending}
                          onClick={() =>
                            modal.confirm({
                              title: "拒绝订单",
                              content: "确认拒绝这笔待审核订单，并释放冻结预算？",
                              okText: "拒绝",
                              okButtonProps: { danger: true },
                              onOk: () => rejectOrder.mutate(record.id)
                            })
                          }
                        >
                          拒绝
                        </Button>
                      </Space>
                    )
                  }
                ]}
              />
            )
          },
          {
            key: "topups",
            label: "待审入账",
            children: (
              <Space direction="vertical" size={12} className="full-width">
                <Form
                  form={topupForm}
                  layout="inline"
                  onFinish={(values) => createTopup.mutate(values)}
                >
                  <Form.Item name="recipient_telegram_user_id" rules={[{ required: true }]}>
                    <Input placeholder="收款 Telegram ID" style={{ width: 160 }} />
                  </Form.Item>
                  <Form.Item name="amount" rules={[{ required: true }]}>
                    <InputNumber min={0.01} step={1} addonBefore="USD" placeholder="金额" style={{ width: 150 }} />
                  </Form.Item>
                  <Form.Item name="reason" rules={[{ required: true }]}>
                    <Input placeholder="入账原因/凭证摘要" style={{ width: 260 }} />
                  </Form.Item>
                  <Form.Item name="request_note">
                    <Input placeholder="备注" style={{ width: 180 }} />
                  </Form.Item>
                  <Button type="primary" htmlType="submit" loading={createTopup.isPending}>
                    创建申请
                  </Button>
                </Form>
                <Table<TopupRequest>
                  rowKey="id"
                  loading={topups.isLoading || isLoading}
                  size="small"
                  dataSource={topups.data?.items ?? []}
                  pagination={false}
                  columns={[
                    { title: "请求", dataIndex: "id", width: 190 },
                    { title: "收款方", dataIndex: "recipient_telegram_user_id", width: 120 },
                    {
                      title: "金额",
                      dataIndex: "amount_cents",
                      width: 120,
                      render: cents
                    },
                    { title: "原因", dataIndex: "reason" },
                    {
                      title: "操作",
                      width: 170,
                      render: (_, record) => (
                        <Space>
                          <Button
                            type="primary"
                            size="small"
                            loading={approveTopup.isPending}
                            onClick={() => approveTopup.mutate(record.id)}
                          >
                            通过
                          </Button>
                          <Button
                            size="small"
                            danger
                            loading={rejectTopup.isPending}
                            onClick={() => rejectTopup.mutate(record.id)}
                          >
                            拒绝
                          </Button>
                        </Space>
                      )
                    }
                  ]}
                />
              </Space>
            )
          },
          {
            key: "wallet",
            label: "钱包",
            children: <AdminWalletPanel data={adminWallet.data} loading={adminWallet.isLoading || isLoading} />
          },
          {
            key: "deliveries",
            label: "投放记录",
            children: (
              <Table<Delivery>
                rowKey="id"
                loading={deliveries.isLoading || isLoading}
                size="small"
                dataSource={deliveries.data?.items ?? []}
                pagination={false}
                columns={[
                  { title: "投放", dataIndex: "id", width: 190 },
                  { title: "频道", dataIndex: "channel_title" },
                  { title: "广告主", dataIndex: "advertiser_telegram_user_id", width: 120 },
                  {
                    title: "状态",
                    dataIndex: "status",
                    width: 110,
                    render: (value) => <Tag>{value}</Tag>
                  },
                  { title: "扣费", dataIndex: "charge_cents", width: 110, render: cents },
                  { title: "已退", dataIndex: "refunded_cents", width: 110, render: cents },
                  {
                    title: "操作",
                    width: 230,
                    render: (_, record) => (
                      <Space>
                        <Button size="small" onClick={() => setDetailTarget({ type: "delivery", id: record.id })}>
                          详情
                        </Button>
                        <Button
                          size="small"
                          danger
                          disabled={!["sent", "disputed"].includes(record.status)}
                          loading={refundDelivery.isPending}
                          onClick={() =>
                            modal.confirm({
                              title: "投放退款",
                              content: "确认对这条投放执行全额退款？",
                              okText: "退款",
                              okButtonProps: { danger: true },
                              onOk: () => refundDelivery.mutate(record.id)
                            })
                          }
                        >
                          退款
                        </Button>
                        <Button
                          size="small"
                          disabled={!["sent", "disputed"].includes(record.status)}
                          loading={reportDeletion.isPending}
                          onClick={() =>
                            modal.confirm({
                              title: "举报删帖",
                              content: "确认按频道主提前删除广告处理？系统会打开争议并走退款流程。",
                              okText: "确认处理",
                              onOk: () => reportDeletion.mutate(record.id)
                            })
                          }
                        >
                          删帖
                        </Button>
                      </Space>
                    )
                  }
                ]}
              />
            )
          },
          {
            key: "disputes",
            label: "争议",
            children: (
              <Table<Dispute>
                rowKey="id"
                loading={disputes.isLoading || isLoading}
                size="small"
                dataSource={disputes.data?.items ?? []}
                pagination={false}
                columns={[
                  { title: "争议", dataIndex: "id", width: 190 },
                  { title: "频道", dataIndex: "channel_title" },
                  { title: "原因", dataIndex: "reason" },
                  {
                    title: "状态",
                    dataIndex: "status",
                    width: 110,
                    render: (value) => <Tag color="orange">{value}</Tag>
                  },
                  {
                    title: "操作",
                    width: 170,
                    render: (_, record) => (
                      <Space>
                        <Button size="small" onClick={() => setDetailTarget({ type: "dispute", id: record.id })}>
                          详情
                        </Button>
                        <Button
                          size="small"
                          type="primary"
                          disabled={record.status !== "open"}
                          loading={resolveDispute.isPending}
                          onClick={() => resolveDispute.mutate(record.id)}
                        >
                          裁决完成
                        </Button>
                      </Space>
                    )
                  }
                ]}
              />
            )
          },
          {
            key: "accounts",
            label: "账户",
            children: (
              <Space direction="vertical" size={12} className="full-width">
                <Input.Search allowClear placeholder="搜索 Telegram ID / 昵称 / Account ID" onSearch={setAdminSearch} />
                <Table<AdminAccount>
                  rowKey="id"
                  size="small"
                  loading={accounts.isLoading}
                  dataSource={accounts.data?.items ?? []}
                  pagination={false}
                  columns={[
                    { title: "账户", dataIndex: "id", width: 190 },
                    { title: "Telegram", dataIndex: "telegram_user_id", width: 130 },
                    { title: "角色", dataIndex: "role", width: 100, render: (value) => <Tag>{value}</Tag> },
                    { title: "名称", dataIndex: "display_name" },
                    {
                      title: "门户",
                      width: 260,
                      render: (_, record) => (
                        <Space size={[4, 4]} wrap>
                          {record.admin_portal_status ? (
                            <Tag color={record.admin_portal_status === "active" ? "purple" : "default"}>
                              管理 {record.admin_level ? `· ${record.admin_level}` : `· ${record.admin_portal_status}`}
                            </Tag>
                          ) : null}
                          {record.advertiser_portal_status ? (
                            <Tag color={record.advertiser_portal_status === "active" ? "green" : "default"}>
                              广告主 · {record.advertiser_portal_status}
                            </Tag>
                          ) : null}
                          {record.publisher_portal_status ? (
                            <Tag color={record.publisher_portal_status === "active" ? "cyan" : "default"}>
                              频道主 · {record.publisher_portal_status}
                            </Tag>
                          ) : null}
                        </Space>
                      )
                    },
                    { title: "可用", dataIndex: "available_balance_cents", width: 110, render: cents },
                    { title: "冻结", dataIndex: "reserved_balance_cents", width: 110, render: cents },
                    { title: "待确认收益", dataIndex: "pending_earnings_cents", width: 130, render: cents },
                    {
                      title: "代看",
                      width: 240,
                      render: (_, record) => (
                        <Space>
                          <Button size="small" onClick={() => setPermissionTarget(record)}>
                            权限
                          </Button>
                          <Button
                            size="small"
                            disabled={record.advertiser_portal_status !== "active"}
                            loading={startImpersonation.isPending}
                            onClick={() => setImpersonationTarget({ account: record, portal: "advertiser" })}
                          >
                            广告主
                          </Button>
                          <Button
                            size="small"
                            disabled={record.publisher_portal_status !== "active"}
                            loading={startImpersonation.isPending}
                            onClick={() => setImpersonationTarget({ account: record, portal: "publisher" })}
                          >
                            频道主
                          </Button>
                        </Space>
                      )
                    }
                  ]}
                />
              </Space>
            )
          },
          {
            key: "channels",
            label: "频道",
            children: (
              <Space direction="vertical" size={12} className="full-width">
                <Input.Search allowClear placeholder="搜索频道名 / 用户名 / Token / Chat ID" onSearch={setAdminSearch} />
                <Table<AdminChannel>
                  rowKey="id"
                  size="small"
                  loading={channels.isLoading}
                  dataSource={channels.data?.items ?? []}
                  pagination={false}
                  columns={[
                    { title: "频道", dataIndex: "title" },
                    { title: "用户名", dataIndex: "username", width: 150 },
                    { title: "Token", dataIndex: "ref_token", width: 150 },
                    { title: "频道主", dataIndex: "owner_telegram_user_id", width: 130 },
                    { title: "日上限", dataIndex: "daily_ad_limit", width: 90 },
                    { title: "投放数", dataIndex: "deliveries_count", width: 90 },
                    { title: "待确认收益", dataIndex: "pending_earnings_cents", width: 130, render: cents }
                  ]}
                />
              </Space>
            )
          },
          {
            key: "audit",
            label: "审计",
            children: (
              <Space direction="vertical" size={12} className="full-width">
                <Segmented<AuditCategory>
                  data-testid="audit-category-filter"
                  options={auditCategoryOptions}
                  value={auditCategory}
                  onChange={setAuditCategory}
                />
                <Space size={[8, 8]} wrap>
                  <Input.Search
                    allowClear
                    placeholder="搜索动作 / 对象 ID / Payload"
                    onSearch={setAuditSearch}
                    style={{ width: 260 }}
                  />
                  <Input
                    allowClear
                    placeholder="操作者 Telegram / Account"
                    value={auditActor}
                    onChange={(event) => setAuditActor(event.target.value)}
                    style={{ width: 220 }}
                  />
                  <Input
                    allowClear
                    placeholder="目标账号 Telegram / Account"
                    value={auditTarget}
                    onChange={(event) => setAuditTarget(event.target.value)}
                    style={{ width: 220 }}
                  />
                  <DatePicker.RangePicker
                    onChange={(_, dateStrings) =>
                      setAuditDateRange(dateStrings[0] && dateStrings[1] ? [dateStrings[0], dateStrings[1]] : null)
                    }
                  />
                  <Checkbox checked={auditVerifyStrict} onChange={(event) => setAuditVerifyStrict(event.target.checked)}>
                    严格
                  </Checkbox>
                  <Button icon={<DownloadOutlined />} onClick={exportAuditCsv}>
                    导出 CSV
                  </Button>
                  <Button
                    icon={<SafetyCertificateOutlined />}
                    loading={verifyAuditChain.isPending}
                    onClick={() => verifyAuditChain.mutate()}
                  >
                    校验链路
                  </Button>
                </Space>
                <Space size={12} wrap>
                  <Typography.Text type="secondary">
                    共 {auditLogs.data?.total ?? 0} 条
                    {auditLogs.data?.retention_days ? ` · 保留 ${auditLogs.data.retention_days} 天` : ""}
                  </Typography.Text>
                  <Typography.Text type="secondary">
                    链头 {auditLogs.data?.chain_head ? `${auditLogs.data.chain_head.slice(0, 18)}...` : "暂无"}
                  </Typography.Text>
                  <Typography.Text type="secondary">
                    校验范围 {auditDateRange ? `${auditDateRange[0]} 至 ${auditDateRange[1]}` : "全量"}
                    {auditVerifyStrict ? " · 严格" : ""}
                  </Typography.Text>
                </Space>
                <Table<AuditLog>
                  rowKey="id"
                  size="small"
                  loading={auditLogs.isLoading}
                  dataSource={auditLogs.data?.items ?? []}
                  pagination={{
                    current: auditPage,
                    pageSize: auditPageSize,
                    total: auditLogs.data?.total ?? 0,
                    showSizeChanger: true,
                    onChange: (page, pageSize) => {
                      setAuditPage(page);
                      setAuditPageSize(pageSize);
                    }
                  }}
                  columns={[
                    { title: "时间", dataIndex: "created_at", width: 170 },
                    { title: "动作", dataIndex: "action", width: 190 },
                    { title: "对象", width: 230, render: (_, record) => `${record.entity_type}#${record.entity_id}` },
                    { title: "操作人", dataIndex: "actor_telegram_user_id", width: 130 },
                    { title: "目标账号", dataIndex: "target_telegram_user_id", width: 130 },
                    { title: "Hash", width: 118, render: (_, record) => record.audit_hash ? `${record.audit_hash.slice(0, 10)}...` : "-" },
                    { title: "Payload", dataIndex: "payload_json", ellipsis: true },
                    {
                      title: "操作",
                      width: 90,
                      render: (_, record) => (
                        <Button size="small" icon={<EyeOutlined />} onClick={() => setAuditDetail(record)}>
                          详情
                        </Button>
                      )
                    }
                  ]}
                />
              </Space>
            )
          },
          {
            key: "settings",
            label: "设置",
            children: <AdminSettingsPanel data={adminSettings.data} loading={adminSettings.isLoading || isLoading} />
          }
        ]}
      />
      <PermissionDrawer
        target={permissionTarget}
        loading={updatePortalAccess.isPending}
        onClose={() => setPermissionTarget(null)}
        onUpdate={confirmPermissionUpdate}
      />
      <ImpersonationDrawer
        target={impersonationTarget}
        loading={startImpersonation.isPending}
        onClose={() => setImpersonationTarget(null)}
        onSubmit={(payload) => startImpersonation.mutate(payload)}
      />
      <Drawer
        title={auditDetail ? `审计详情 · ${auditDetail.action}` : "审计详情"}
        width={760}
        open={Boolean(auditDetail)}
        onClose={() => setAuditDetail(null)}
      >
        {auditDetail ? <AuditLogDetail record={auditDetail} /> : null}
      </Drawer>
      <Drawer
        title="审计链校验"
        width={760}
        open={Boolean(auditIntegrity)}
        onClose={() => setAuditIntegrity(null)}
      >
        {auditIntegrity ? <AuditIntegrityReportView report={auditIntegrity} /> : null}
      </Drawer>
      <Drawer
        title={detailTarget ? `${detailTarget.type} ${detailTarget.id}` : "详情"}
        width={760}
        open={Boolean(detailTarget)}
        onClose={() => setDetailTarget(null)}
      >
        {detail.isLoading ? <Typography.Text>加载中...</Typography.Text> : <AdminDetailView data={detail.data} />}
      </Drawer>
    </section>
  );
}

function AdminWalletPanel({ data, loading }: { data?: AdminWalletResponse; loading: boolean }) {
  const totals = data?.totals;
  return (
    <Space direction="vertical" size={14} className="full-width" data-testid="admin-wallet-panel">
      <Row gutter={[12, 12]}>
        <Col xs={12} lg={6}>
          <MetricCard title="全站可用余额" value={cents(totals?.available_cents ?? 0)} />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="全站冻结预算" value={cents(totals?.reserved_cents ?? 0)} />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="累计消耗" value={cents(totals?.spent_cents ?? 0)} />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="可结算收益" value={cents(totals?.releasable_earnings_cents ?? 0)} />
        </Col>
      </Row>
      <Row gutter={[12, 12]}>
        {(data?.topups_by_status ?? []).map((item) => (
          <Col xs={24} md={8} key={item.status}>
            <MetricCard title={`入账 ${item.status}`} value={cents(item.amount_cents)} suffix={`${item.count} 笔`} />
          </Col>
        ))}
      </Row>
      <Typography.Title level={4}>最近账本流水</Typography.Title>
      <Table<AdminLedgerTransaction>
        data-testid="admin-wallet-ledger-table"
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={data?.recent_ledger ?? []}
        pagination={false}
        locale={{ emptyText: "暂无账本流水" }}
        columns={[
          { title: "时间", dataIndex: "created_at", width: 170 },
          { title: "账户", width: 180, render: (_, record) => record.account_display_name || record.account_telegram_user_id || "-" },
          { title: "类型", dataIndex: "type", width: 150 },
          { title: "金额", dataIndex: "amount_cents", width: 120, render: cents },
          { title: "关联", width: 180, render: (_, record) => record.order_id || record.delivery_id || record.related_telegram_user_id || "-" },
          { title: "备注", dataIndex: "memo", ellipsis: true }
        ]}
      />
      <Typography.Title level={4}>资金账户榜</Typography.Title>
      <Table<AdminWalletAccount>
        data-testid="admin-wallet-accounts-table"
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={data?.accounts ?? []}
        pagination={false}
        locale={{ emptyText: "暂无资金账户" }}
        columns={[
          { title: "账户", width: 180, render: (_, record) => record.display_name || record.telegram_user_id || record.id },
          { title: "角色", dataIndex: "role", width: 100 },
          { title: "可用", dataIndex: "available_balance_cents", width: 120, render: cents },
          { title: "冻结", dataIndex: "reserved_balance_cents", width: 120, render: cents },
          { title: "已消耗", dataIndex: "spent_balance_cents", width: 120, render: cents },
          { title: "待确认收益", dataIndex: "pending_earnings_cents", width: 130, render: cents },
          { title: "可结算", dataIndex: "releasable_earnings_cents", width: 120, render: cents }
        ]}
      />
    </Space>
  );
}

function AdminSettingsPanel({ data, loading }: { data?: AdminSettingsResponse; loading: boolean }) {
  if (!data && loading) {
    return <Typography.Text>加载中...</Typography.Text>;
  }
  if (!data) {
    return <Typography.Text type="secondary">暂无设置数据</Typography.Text>;
  }
  return (
    <Space direction="vertical" size={14} className="full-width" data-testid="admin-settings-panel">
      <Row gutter={[12, 12]}>
        <Col xs={12} lg={6}>
          <MetricCard title="审计记录" value={data.audit.total ?? 0} />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="已签名" value={data.audit.signed ?? 0} />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="未签名" value={data.audit.unsigned ?? 0} />
        </Col>
        <Col xs={12} lg={6}>
          <MetricCard title="迁移版本" value={data.database.migrations} />
        </Col>
      </Row>
      <Descriptions
        bordered
        size="small"
        column={1}
        items={[
          { key: "env", label: "环境", children: data.environment },
          { key: "base", label: "公开地址", children: data.public_base_url || "-" },
          { key: "origins", label: "允许来源", children: data.web_allowed_origins.join(", ") || "-" },
          { key: "cookie", label: "Cookie", children: `${data.session_cookie_secure ? "secure" : "not-secure"} · ${data.session_cookie_samesite}` },
          { key: "dev", label: "开发开关", children: `bypass=${String(data.dev_auth_bypass)} · dev-session=${String(data.dev_session_enabled)}` },
          { key: "audit", label: "审计策略", children: `${data.audit_retention_days} 天 · 导出上限 ${data.audit_export_max_rows}` },
          { key: "db", label: "数据库", children: data.database.path },
          { key: "backup", label: "最近备份", children: data.database.last_backup ? `${data.database.last_backup.path} · ${formatBytes(data.database.last_backup.size_bytes)}` : "暂无" }
        ]}
      />
      <section className="mobile-section">
        <Typography.Title level={4}>发布准入</Typography.Title>
        <Space size={[6, 6]} wrap>
          {data.release_gates.map((gate) => (
            <Tag key={gate.key} color={gate.ok ? "green" : "red"}>{gate.label}</Tag>
          ))}
        </Space>
      </section>
      <section className="mobile-section">
        <Typography.Title level={4}>审计时间</Typography.Title>
        <Space direction="vertical" size={4}>
          <Typography.Text type="secondary">{`首条：${data.audit.first_at || "-"}`}</Typography.Text>
          <Typography.Text type="secondary">{`最新：${data.audit.last_at || "-"}`}</Typography.Text>
        </Space>
      </section>
    </Space>
  );
}

function formatBytes(value: number) {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function PermissionDrawer({
  target,
  loading,
  onClose,
  onUpdate
}: {
  target: AdminAccount | null;
  loading: boolean;
  onClose: () => void;
  onUpdate: (payload: {
    account_id: string;
    portal: Portal;
    status: PortalAccessStatus;
    reason: string;
    admin_level?: AdminLevel;
    confirm_phrase?: string;
  }, summary: string) => void;
}) {
  const accountLabel = target?.display_name || target?.telegram_user_id || target?.id || "";
  const [reason, setReason] = useState("");
  const [confirmText, setConfirmText] = useState("");
  useEffect(() => {
    setReason("");
    setConfirmText("");
  }, [target?.id]);
  const reasonText = reason.trim();
  const submitUpdate = (
    payload: Omit<Parameters<typeof onUpdate>[0], "reason">,
    summary: string
  ) => {
    if (!reasonText) {
      return;
    }
    const highRisk = payload.portal === "admin" || payload.status === "revoked";
    if (highRisk && confirmText.trim() !== permissionConfirmPhrase) {
      return;
    }
    onUpdate(
      {
        ...payload,
        confirm_phrase: highRisk ? permissionConfirmPhrase : undefined,
        reason: `${summary}｜备注：${reasonText}`
      },
      `${accountLabel}：${summary}`
    );
  };
  const canRun = (portal: Portal, status: PortalAccessStatus) => {
    const highRisk = portal === "admin" || status === "revoked";
    return Boolean(reasonText) && (!highRisk || confirmText.trim() === permissionConfirmPhrase);
  };
  const userPortalRows: Array<{ portal: Exclude<Portal, "admin">; status: string | null }> = target
    ? [
        { portal: "advertiser", status: target.advertiser_portal_status },
        { portal: "publisher", status: target.publisher_portal_status }
      ]
    : [];
  return (
    <Drawer
      title={target ? `权限管理 · ${accountLabel}` : "权限管理"}
      width={520}
      open={Boolean(target)}
      onClose={onClose}
    >
      {target ? (
        <Space direction="vertical" size={18} className="full-width">
          <section>
            <Typography.Title level={5}>权限调整备注</Typography.Title>
            <Input.TextArea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="填写调整原因、审批依据或工单号；备注必填"
              rows={3}
              maxLength={240}
              showCount
            />
            <Typography.Text type="secondary">开通、撤销、设候选和管理员等级调整都会写入审计日志，并需要二次确认。</Typography.Text>
            <Input
              value={confirmText}
              onChange={(event) => setConfirmText(event.target.value)}
              placeholder={`高风险操作需输入：${permissionConfirmPhrase}`}
              style={{ marginTop: 10 }}
            />
          </section>
          <section>
            <Typography.Title level={5}>用户门户</Typography.Title>
            <Space direction="vertical" size={12} className="full-width">
              {userPortalRows.map((item) => (
                <div className="mobile-action-card" key={item.portal}>
                  <Space direction="vertical" size={6}>
                    <Space>
                      <Typography.Text strong>{userPortalLabels[item.portal]}</Typography.Text>
                      <Tag color={item.status === "active" ? "green" : item.status === "revoked" ? "red" : "default"}>
                        {item.status || "未开通"}
                      </Tag>
                    </Space>
                    <Space wrap>
                      <Button
                        type={item.status === "active" ? "primary" : "default"}
                        loading={loading}
                        disabled={item.status === "active" || !canRun(item.portal, "active")}
                        onClick={() =>
                          submitUpdate({
                            account_id: target.id,
                            portal: item.portal,
                            status: "active"
                          }, `开通${userPortalLabels[item.portal]}`)
                        }
                      >
                        开通
                      </Button>
                      <Button
                        loading={loading}
                        disabled={item.status === "candidate" || !canRun(item.portal, "candidate")}
                        onClick={() =>
                          submitUpdate({
                            account_id: target.id,
                            portal: item.portal,
                            status: "candidate"
                          }, `设为候选${userPortalLabels[item.portal]}`)
                        }
                      >
                        设候选
                      </Button>
                      <Button
                        danger
                        loading={loading}
                        disabled={item.status === "revoked" || !canRun(item.portal, "revoked")}
                        onClick={() =>
                          submitUpdate({
                            account_id: target.id,
                            portal: item.portal,
                            status: "revoked"
                          }, `撤销${userPortalLabels[item.portal]}`)
                        }
                      >
                        撤销
                      </Button>
                    </Space>
                  </Space>
                </div>
              ))}
            </Space>
          </section>
          <section>
            <Typography.Title level={5}>管理员等级</Typography.Title>
            <Space direction="vertical" size={10} className="full-width">
              <Space size={[8, 8]} wrap>
                {(Object.keys(adminLevelLabels) as AdminLevel[]).map((level) => (
                  <Button
                    key={level}
                    type={target.admin_level === level ? "primary" : "default"}
                    loading={loading}
                    disabled={!canRun("admin", "active")}
                    onClick={() =>
                      submitUpdate({
                        account_id: target.id,
                        portal: "admin",
                        status: "active",
                        admin_level: level
                      }, `设置管理员等级为 ${level}`)
                    }
                  >
                    {adminLevelLabels[level]}
                  </Button>
                ))}
              </Space>
              <Button
                danger
                loading={loading}
                disabled={target.admin_portal_status !== "active" || !canRun("admin", "revoked")}
                onClick={() =>
                  submitUpdate({
                    account_id: target.id,
                    portal: "admin",
                    status: "revoked"
                  }, "撤销管理员权限")
                }
              >
                撤销管理员
              </Button>
            </Space>
          </section>
        </Space>
      ) : null}
    </Drawer>
  );
}

function ImpersonationDrawer({
  target,
  loading,
  onClose,
  onSubmit
}: {
  target: { account: AdminAccount; portal: Exclude<Portal, "admin"> } | null;
  loading: boolean;
  onClose: () => void;
  onSubmit: (payload: { target_account_id: string; portal: Exclude<Portal, "admin">; reason: string }) => void;
}) {
  const [reason, setReason] = useState("");
  useEffect(() => {
    setReason("");
  }, [target?.account.id, target?.portal]);
  const reasonText = reason.trim();
  const accountLabel = target?.account.display_name || target?.account.telegram_user_id || target?.account.id || "";
  return (
    <Drawer
      title={target ? `代看确认 · ${accountLabel}` : "代看确认"}
      width={460}
      open={Boolean(target)}
      onClose={onClose}
    >
      {target ? (
        <Space direction="vertical" size={14} className="full-width">
          <Typography.Text type="secondary">
            管理员代看会写入审计日志；请填写本次查看原因、工单号或排查目标。
          </Typography.Text>
          <Input.TextArea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="例如：排查广告主计划提交异常，工单 CHABO-123"
            rows={4}
            maxLength={240}
            showCount
          />
          <Button
            type="primary"
            danger
            loading={loading}
            disabled={reasonText.length < 4}
            onClick={() =>
              onSubmit({
                target_account_id: target.account.id,
                portal: target.portal,
                reason: reasonText
              })
            }
          >
            进入代看
          </Button>
        </Space>
      ) : null}
    </Drawer>
  );
}

function AuditLogDetail({ record }: { record: AuditLog }) {
  const parsedPayload = parseAuditPayload(record.payload_json);
  const diffRows = buildAuditDiffRows(parsedPayload);
  const payload = parsedPayload ? JSON.stringify(parsedPayload, null, 2) : record.payload_json;
  return (
    <Space direction="vertical" size={14} className="full-width">
      <Descriptions
        bordered
        size="small"
        column={1}
        items={[
          { key: "id", label: "审计 ID", children: record.id },
          { key: "time", label: "时间", children: record.created_at },
          { key: "action", label: "动作", children: record.action },
          { key: "entity", label: "对象", children: `${record.entity_type}#${record.entity_id}` },
          { key: "target", label: "目标账号", children: record.target_display_name || record.target_telegram_user_id || "-" },
          { key: "actor", label: "操作者", children: record.actor_telegram_user_id || record.actor_account_id || "-" },
          {
            key: "audit_hash",
            label: "链式 Hash",
            children: record.audit_hash ? <Typography.Text copyable code>{record.audit_hash}</Typography.Text> : "-"
          },
          {
            key: "previous_hash",
            label: "上一条 Hash",
            children: record.previous_hash ? <Typography.Text copyable code>{record.previous_hash}</Typography.Text> : "-"
          },
          { key: "hash_version", label: "Hash 版本", children: record.hash_version || "-" }
        ]}
      />
      {diffRows.length ? (
        <section>
          <Typography.Title level={5}>字段变更</Typography.Title>
          <Table<AuditDiffRow>
            rowKey="field"
            size="small"
            pagination={false}
            dataSource={diffRows}
            columns={[
              { title: "字段", dataIndex: "field", width: 150 },
              { title: "变更前", dataIndex: "before", render: (value) => <Typography.Text>{formatAuditValue(value)}</Typography.Text> },
              { title: "变更后", dataIndex: "after", render: (value) => <Typography.Text>{formatAuditValue(value)}</Typography.Text> }
            ]}
          />
        </section>
      ) : null}
      <Typography.Title level={5}>Payload</Typography.Title>
      <pre className="json-pre">{payload}</pre>
    </Space>
  );
}

function parseAuditPayload(payloadJson: string): Record<string, unknown> | null {
  try {
    const payload = JSON.parse(payloadJson);
    return payload && typeof payload === "object" && !Array.isArray(payload) ? payload : null;
  } catch {
    return null;
  }
}

function buildAuditDiffRows(payload: Record<string, unknown> | null): AuditDiffRow[] {
  if (!payload || !payload.changed_fields || typeof payload.changed_fields !== "object" || Array.isArray(payload.changed_fields)) {
    return [];
  }
  return Object.entries(payload.changed_fields as Record<string, unknown>).map(([field, value]) => {
    if (value && typeof value === "object" && !Array.isArray(value) && "before" in value && "after" in value) {
      const diff = value as { before: unknown; after: unknown };
      return { field, before: diff.before, after: diff.after };
    }
    return { field, before: "-", after: value };
  });
}

function formatAuditValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function AuditIntegrityReportView({ report }: { report: AuditIntegrityReport }) {
  return (
    <Space direction="vertical" size={14} className="full-width">
      <Descriptions
        bordered
        size="small"
        column={1}
        items={[
          { key: "ok", label: "校验结果", children: report.ok ? <Tag color="green">通过</Tag> : <Tag color="red">异常</Tag> },
          { key: "checked", label: "检查记录", children: report.checked_rows },
          { key: "signed", label: "已签名记录", children: report.signed_rows },
          { key: "unsigned", label: "未签名记录", children: report.unsigned_rows },
          { key: "gaps", label: "链后未签名", children: report.unsigned_after_chain_started },
          { key: "invalid", label: "Hash 异常", children: report.invalid_hashes },
          { key: "broken", label: "断链记录", children: report.broken_links },
          { key: "strict", label: "严格模式", children: report.strict_unsigned ? "开启" : "关闭" },
          {
            key: "window",
            label: "校验时间窗",
            children: report.created_from || report.created_to ? `${report.created_from || "-"} 至 ${report.created_to || "-"}` : "全量"
          },
          {
            key: "head",
            label: "当前链头",
            children: report.chain_head ? <Typography.Text copyable code>{report.chain_head}</Typography.Text> : "-"
          },
          {
            key: "first",
            label: "首条签名",
            children: report.first_signed_hash ? <Typography.Text copyable code>{report.first_signed_hash}</Typography.Text> : "-"
          }
        ]}
      />
      <section>
        <Typography.Title level={5}>异常记录</Typography.Title>
        <List<Record<string, unknown>>
          size="small"
          dataSource={report.issues}
          locale={{ emptyText: "暂无异常" }}
          renderItem={(item) => (
            <List.Item>
              <Space direction="vertical" size={2}>
                <Typography.Text strong>{formatAuditValue(item.kind)}</Typography.Text>
                <Typography.Text type="secondary">
                  {formatAuditValue(item.created_at)} · {formatAuditValue(item.action)} · {formatAuditValue(item.entity_type)}#
                  {formatAuditValue(item.entity_id)}
                </Typography.Text>
                <Typography.Text code>{formatAuditValue(item.id)}</Typography.Text>
              </Space>
            </List.Item>
          )}
        />
        {report.truncated_issues ? (
          <Typography.Text type="secondary">还有 {report.truncated_issues} 条异常未展示</Typography.Text>
        ) : null}
      </section>
    </Space>
  );
}

function MobileOpsSection({ title, children }: { title: string; children: ReactNode }) {
  const hasChildren = Array.isArray(children) ? children.length > 0 : Boolean(children);
  return (
    <section className="mobile-section" data-testid="mobile-admin-ops-section">
      <Typography.Title level={4}>{title}</Typography.Title>
      {hasChildren ? children : <Typography.Text type="secondary">暂无待处理项</Typography.Text>}
    </section>
  );
}

function AdminDetailView({ data }: { data?: Record<string, unknown> }) {
  if (!data) {
    return null;
  }
  const timeline = (data.timeline as TimelineEvent[] | undefined) ?? [];
  const primary = data.order ?? data.delivery ?? data.dispute;
  const ledger = (data.ledger_transactions as unknown[] | undefined) ?? [];
  return (
    <Space direction="vertical" size={16} className="full-width">
      <section>
        <Typography.Title level={4}>基础信息</Typography.Title>
        <pre className="json-pre">{JSON.stringify(primary, null, 2)}</pre>
      </section>
      <section>
        <Typography.Title level={4}>时间线</Typography.Title>
        <List<TimelineEvent>
          size="small"
          dataSource={timeline}
          locale={{ emptyText: "暂无时间线" }}
          renderItem={(item) => (
            <List.Item>
              <Space direction="vertical" size={2}>
                <Typography.Text strong>{`${item.kind}｜${item.title}`}</Typography.Text>
                <Typography.Text type="secondary">{`${item.at}｜${item.entity}`}</Typography.Text>
                {item.summary ? <Typography.Text>{item.summary}</Typography.Text> : null}
                {item.note ? <Typography.Text type="warning">{item.note}</Typography.Text> : null}
              </Space>
            </List.Item>
          )}
        />
      </section>
      <section>
        <Typography.Title level={4}>账本流水</Typography.Title>
        <pre className="json-pre">{JSON.stringify(ledger, null, 2)}</pre>
      </section>
    </Space>
  );
}
