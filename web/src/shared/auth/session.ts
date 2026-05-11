import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { apiFetch, type AuthConfig, type MeResponse, type Portal } from "../api/client";

interface AuthSessionResponse {
  portals: Portal[];
}

export function landingForPortals(portals: Portal[]) {
  if (portals.includes("admin")) return "/admin";
  if (portals.includes("advertiser")) return "/advertiser";
  if (portals.includes("publisher")) return "/publisher";
  return "/login";
}

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: () => apiFetch<MeResponse>("/api/auth/me"),
    retry: false
  });
}

export function useAuthConfig() {
  return useQuery({
    queryKey: ["auth-config"],
    queryFn: () => apiFetch<AuthConfig>("/api/auth/config"),
    staleTime: 60_000
  });
}

export function useDevSession() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: (payload: {
      telegram_user_id: string;
      display_name?: string;
      portals: Portal[];
      admin_token: string;
    }) =>
      apiFetch<AuthSessionResponse>("/api/auth/dev-session", {
        method: "POST",
        headers: {
          "X-Chabo-Admin-Token": payload.admin_token
        },
        body: JSON.stringify({
          telegram_user_id: payload.telegram_user_id,
          display_name: payload.display_name,
          portals: payload.portals
        })
      }),
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      navigate(landingForPortals(data.portals));
    }
  });
}

export function useTelegramWebAppLogin() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: (initData: string) =>
      apiFetch<AuthSessionResponse>("/api/auth/telegram-webapp", {
        method: "POST",
        body: JSON.stringify({ init_data: initData })
      }),
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      navigate(landingForPortals(data.portals), { replace: true });
    }
  });
}

export function useCreateMagicLink() {
  return useMutation({
    mutationFn: (payload: {
      telegram_user_id: string;
      display_name?: string;
      portals: Portal[];
      admin_token: string;
    }) =>
      apiFetch<{ token: string; expires_at: string; path: string; url: string }>("/api/auth/magic-link", {
        method: "POST",
        headers: {
          "X-Chabo-Admin-Token": payload.admin_token
        },
        body: JSON.stringify({
          telegram_user_id: payload.telegram_user_id,
          display_name: payload.display_name,
          portals: payload.portals
        })
      })
  });
}

export function useStopImpersonation() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: () =>
      apiFetch<AuthSessionResponse>("/api/auth/impersonation/stop", {
        method: "POST"
      }),
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      await queryClient.invalidateQueries({ queryKey: ["admin"] });
      navigate(landingForPortals(data.portals), { replace: true });
    }
  });
}

export function useConsumeMagicLink() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: (token: string) =>
      apiFetch<AuthSessionResponse>("/api/auth/magic/consume", {
        method: "POST",
        body: JSON.stringify({ token })
      }),
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      navigate(landingForPortals(data.portals));
    }
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return useMutation({
    mutationFn: () => apiFetch("/api/auth/logout", { method: "POST" }),
    onSettled: async () => {
      await queryClient.clear();
      navigate("/login");
    }
  });
}
