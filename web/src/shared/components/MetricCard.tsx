import { Card, Statistic } from "antd";
import type { ReactNode } from "react";

export function MetricCard({
  title,
  value,
  suffix
}: {
  title: string;
  value: ReactNode;
  suffix?: string;
}) {
  return (
    <Card className="metric-card">
      <Statistic title={title} value={String(value)} suffix={suffix} />
    </Card>
  );
}
