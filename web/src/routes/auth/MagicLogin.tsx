import { useEffect } from "react";
import { Button, Card, Result, Spin } from "antd";
import { useSearchParams, useNavigate } from "react-router-dom";
import { useConsumeMagicLink } from "../../shared/auth/session";

export function MagicLogin() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get("token");
  const consume = useConsumeMagicLink();

  useEffect(() => {
    if (token && !consume.isPending && !consume.isSuccess && !consume.isError) {
      consume.mutate(token);
    }
  }, [token, consume]);

  if (!token) {
    return (
      <Result
        status="404"
        title="链接无效"
        extra={<Button onClick={() => navigate("/login")}>返回登录</Button>}
      />
    );
  }

  return (
    <div className="login-page">
      <Card className="login-card">
        {consume.isError ? (
          <Result
            status="403"
            title="登录链接已失效"
            extra={<Button onClick={() => navigate("/login")}>返回登录</Button>}
          />
        ) : (
          <Spin tip="正在登录">
            <div className="magic-login-box" />
          </Spin>
        )}
      </Card>
    </div>
  );
}
