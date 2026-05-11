import { Spin, Result, Button } from "antd";
import type { PropsWithChildren } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { type Portal } from "../api/client";
import { useMe } from "./session";

export function RequirePortal({
  portal,
  children
}: PropsWithChildren<{ portal: Portal }>) {
  const navigate = useNavigate();
  const { data, isLoading, error } = useMe();
  if (isLoading) return <Spin fullscreen />;
  if (error) return <Navigate to="/login" replace />;
  const portals = data?.portals ?? [];
  const portalStatus = data?.portal_statuses?.find((item) => item.portal === portal);
  if (!portals.includes("admin") && !portals.includes(portal)) {
    return (
      <Result
        status="403"
        title={portalStatus?.status === "candidate" ? "门户待开通" : "无权限"}
        subTitle={
          portalStatus?.status === "candidate"
            ? portal === "advertiser"
              ? "首次投放审核通过后会自动开通广告主端。"
              : "频道产生真实投放后会自动开通频道主端。"
            : undefined
        }
        extra={<Button onClick={() => navigate("/login")}>返回登录</Button>}
      />
    );
  }
  return children;
}
