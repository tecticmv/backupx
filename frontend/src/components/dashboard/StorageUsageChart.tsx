import { useMemo } from "react";
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts";
import { HardDrive } from "lucide-react";
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart";
import { formatBytes } from "@/lib/format";
import type { StorageUsage } from "@/types/health";

const chartConfig = {
  bytes: { label: "Repository size", color: "var(--chart-1)" },
} satisfies ChartConfig;

interface StorageRow {
  storage_id: string;
  name: string;
  bytes: number;
  jobs: { job_id: string; job_name: string; bytes: number | null; snapshots: number | null }[];
}

/** Horizontal bars: total repository bytes per storage, per-job sizes in the tooltip. */
export default function StorageUsageChart({ usage }: { usage: StorageUsage[] }) {
  const rows = useMemo<StorageRow[]>(() => {
    const by = new Map<string, StorageRow>();
    for (const u of usage) {
      const row = by.get(u.storage_id) ?? { storage_id: u.storage_id, name: u.storage_name, bytes: 0, jobs: [] };
      row.bytes += u.bytes ?? 0;
      row.jobs.push({ job_id: u.job_id, job_name: u.job_name, bytes: u.bytes, snapshots: u.snapshots });
      by.set(u.storage_id, row);
    }
    return [...by.values()]
      .filter((r) => r.jobs.some((j) => j.bytes != null))
      .map((r) => ({ ...r, jobs: [...r.jobs].sort((a, b) => (b.bytes ?? 0) - (a.bytes ?? 0)) }))
      .sort((a, b) => b.bytes - a.bytes);
  }, [usage]);

  if (rows.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-10 text-center">
        <HardDrive className="mb-3 h-10 w-10 text-muted-foreground/50" />
        <h3 className="font-medium">No size data yet</h3>
        <p className="mt-1 text-sm text-muted-foreground">Repository sizes appear after the daily size check.</p>
      </div>
    );
  }

  return (
    <ChartContainer
      config={chartConfig}
      className="aspect-auto w-full"
      style={{ height: Math.max(120, rows.length * 44 + 32) }}
    >
      <BarChart accessibilityLayer data={rows} layout="vertical" margin={{ left: 8, right: 16 }}>
        <CartesianGrid horizontal={false} />
        <XAxis
          type="number"
          dataKey="bytes"
          tickLine={false}
          axisLine={false}
          tickFormatter={(v: number) => formatBytes(v)}
          minTickGap={24}
        />
        <YAxis
          type="category"
          dataKey="name"
          tickLine={false}
          axisLine={false}
          width={120}
          tickFormatter={(v: string) => (v.length > 16 ? `${v.slice(0, 15)}…` : v)}
        />
        <ChartTooltip
          cursor={false}
          content={
            <ChartTooltipContent
              className="min-w-[14rem]"
              labelFormatter={(_, payload) => (payload?.[0]?.payload as StorageRow | undefined)?.name ?? ""}
              formatter={(_value, _name, item) => {
                const row = item.payload as StorageRow;
                return (
                  <div className="grid w-full gap-1">
                    <div className="flex justify-between gap-4">
                      <span className="text-muted-foreground">Total</span>
                      <span className="font-mono font-medium tabular-nums">{formatBytes(row.bytes)}</span>
                    </div>
                    {row.jobs.map((j) => (
                      <div key={j.job_id} className="flex justify-between gap-4">
                        <span className="truncate">{j.job_name}</span>
                        <span className="font-mono tabular-nums text-muted-foreground">
                          {formatBytes(j.bytes)}
                          {j.snapshots != null && ` · ${j.snapshots} snap`}
                        </span>
                      </div>
                    ))}
                  </div>
                );
              }}
            />
          }
        />
        <Bar dataKey="bytes" fill="var(--color-bytes)" radius={4} />
      </BarChart>
    </ChartContainer>
  );
}
