export type Portal = "admin" | "advertiser" | "publisher";

export interface PortalStatus {
  portal: Portal;
  status: "candidate" | "active" | "suspended" | "revoked";
  grant_reason: string | null;
  granted_at: string | null;
  revoked_at: string | null;
  updated_at: string | null;
}

export interface MeResponse {
  account: {
    id: string;
    telegram_user_id: string | null;
    role: string;
    display_name: string | null;
  };
  portals: Portal[];
  portal_statuses: PortalStatus[];
  admin_level?: "viewer" | "operator" | "finance" | "super_admin" | null;
  activation: {
    advertiser_successful_orders?: number;
    advertiser_total_orders?: number;
    publisher_owned_channels?: number;
    publisher_produced_deliveries?: number;
    promoted?: Portal[];
  };
  impersonator_account_id: string | null;
  session_expires_at?: string | null;
}

export interface AuthConfig {
  environment: string;
  telegram_webapp_available: boolean;
  dev_auth_bypass: boolean;
  dev_session_enabled: boolean;
}

export const API_BASE = import.meta.env.VITE_CHABO_API_BASE_URL || "";

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `API error ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

export async function apiFetch<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init.headers || {})
    }
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new ApiError(response.status, data?.detail ?? data);
  }
  return data as T;
}

export function cents(amount: number | null | undefined) {
  if (amount == null) return "-";
  return `USD ${(amount / 100).toFixed(2)}`;
}
