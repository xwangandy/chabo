import { App as AntApp, ConfigProvider, theme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: 1
    }
  }
});

export function AppProviders({ children }: PropsWithChildren) {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        algorithm: theme.darkAlgorithm,
        token: {
          colorPrimary: "#3b82f6",
          colorBgBase: "#080b12",
          colorBgContainer: "#111827",
          colorBgElevated: "#172033",
          colorBorder: "#263244",
          colorText: "#e5e7eb",
          colorTextSecondary: "#9ca3af",
          borderRadius: 6,
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
        },
        components: {
          Layout: {
            bodyBg: "#080b12",
            headerBg: "#0b1020",
            siderBg: "#050816",
            triggerBg: "#111827"
          },
          Card: {
            borderRadiusLG: 8,
            colorBgContainer: "#111827"
          },
          Table: {
            headerBg: "#172033",
            rowHoverBg: "#1f2937"
          },
          Tabs: {
            itemSelectedColor: "#60a5fa"
          }
        }
      }}
    >
      <AntApp>
        <QueryClientProvider client={queryClient}>
          {children}
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  );
}
