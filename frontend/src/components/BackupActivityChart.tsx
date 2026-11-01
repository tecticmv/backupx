import { useMemo } from "react";
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";

export interface ActivityDay {
  date: string; // YYYY-MM-DD
  success: number;
  failed: number;
  total: number;
}

const chartConfig = {
  success: { label: "Successful", color: "var(--chart-2)" },
  failed: { label: "Failed", color: "var(--destructive)" },
} satisfies ChartConfig;

const WEEKS = 26;

/** Monday (UTC) of the week a YYYY-MM-DD date falls in, as YYYY-MM-DD. */
function weekStart(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  const offset = (d.getUTCDay() + 6) % 7;
  d.setUTCDate(d.getUTCDate() - offset);
  return d.toISOString().slice(0, 10);
}

function formatWeek(value: string) {
  return new Date(`${value}T00:00:00Z`).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

/** Backup runs per week (last 26 weeks), successful vs failed, stacked. */
export function BackupActivityChart({ data }: { data: ActivityDay[] }) {
  const weeks = useMemo(() => {
    const byWeek = new Map<string, { week: string; success: number; failed: number }>();
    for (const day of data) {
      const key = weekStart(day.date);
      const entry = byWeek.get(key) ?? { week: key, success: 0, failed: 0 };
      entry.success += day.success;
      entry.failed += day.failed;
      byWeek.set(key, entry);
    }
    return [...byWeek.values()].sort((a, b) => a.week.localeCompare(b.week)).slice(-WEEKS);
  }, [data]);

  if (weeks.length === 0) {
    return <p className="text-sm text-muted-foreground">No backup data available</p>;
  }

  return (
    <ChartContainer config={chartConfig} className="aspect-auto h-[220px] w-full">
      <BarChart accessibilityLayer data={weeks} margin={{ left: -16, right: 8 }}>
        <CartesianGrid vertical={false} />
        <XAxis
          dataKey="week"
          tickLine={false}
          axisLine={false}
          tickMargin={8}
          minTickGap={24}
          tickFormatter={formatWeek}
        />
        <YAxis tickLine={false} axisLine={false} allowDecimals={false} width={40} />
        <ChartTooltip
          cursor={false}
          content={
            <ChartTooltipContent labelFormatter={(value) => `Week of ${formatWeek(String(value))}`} />
          }
        />
        <ChartLegend content={<ChartLegendContent />} />
        <Bar dataKey="success" stackId="runs" fill="var(--color-success)" radius={[0, 0, 4, 4]} />
        <Bar dataKey="failed" stackId="runs" fill="var(--color-failed)" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ChartContainer>
  );
}
