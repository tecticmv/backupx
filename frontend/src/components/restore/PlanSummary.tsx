import { AlertTriangle, HardDrive, Info, Loader2 } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { formatBytes } from "@/lib/format";
import { formatLocal, formatUtc } from "@/lib/restore";
import type { RestorePlan } from "@/types/restore";

interface PlanSummaryProps {
  plan: RestorePlan | null;
  loading?: boolean;
  /** Server says it can't run (e.g. disk too small). */
  error?: string | null;
  title?: string;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-right">{children}</dd>
    </div>
  );
}

/** The server's restore plan: what gets downloaded, how big, where it goes, disk check, warnings. */

function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

/** `files` includes the open-file copy when `partial` is set. */
function binlogCountText(b: { files: number; segments: number; partial: boolean }) {
  const archived = Math.max(0, b.files - (b.partial ? 1 : 0));
  if (b.segments === 0 || archived === 0) {
    return b.partial ? "1 binlog file (open-file copy)" : plural(b.files, "binlog file");
  }
  const main = `${plural(archived, "binlog file")} in ${plural(b.segments, "segment")}`;
  return b.partial ? `${main} + 1 open-file copy` : main;
}

export default function PlanSummary({ plan, loading, error, title = "Restore plan" }: PlanSummaryProps) {
  if (!plan) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            {title}
            {loading && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
          </CardTitle>
          <CardDescription>
            {loading ? "Working out the plan..." : error ? "" : "Pick a point to see the plan."}
          </CardDescription>
        </CardHeader>
        {error && (
          <CardContent>
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>Can't restore this point</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          </CardContent>
        )}
      </Card>
    );
  }

  const disk = plan.disk;
  const diskPct = disk && disk.free_bytes > 0 ? Math.min(100, (disk.needed_bytes / disk.free_bytes) * 100) : 100;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          {title}
          {loading && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
        </CardTitle>
        <CardDescription>{plan.summary}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <dl className="space-y-1.5">
          {plan.target_time && (
            <Row label="Restores to">
              {formatLocal(plan.target_time)} <span className="font-mono text-xs text-muted-foreground">({formatUtc(plan.target_time)})</span>
            </Row>
          )}
          <Row label="Storage">{plan.storage.name}</Row>
          <Row label="Download">{formatBytes(plan.download_bytes)}</Row>
          <Row label="Restored size (est.)">{formatBytes(plan.estimated_restored_bytes)}</Row>
          {plan.dump && (
            <Row label={plan.method === "walg" ? "Base backup" : plan.method === "files" ? "Snapshot" : "Dump"}>
              <span className="font-mono text-xs">{plan.dump.snapshot_id.slice(0, 8)}</span> · {formatUtc(plan.dump.time)}
              {plan.dump.size != null && ` · ${formatBytes(plan.dump.size)}`}
            </Row>
          )}
          {plan.binlogs && (
            <Row label="Binlogs">
              {binlogCountText(plan.binlogs)} · {formatBytes(plan.binlogs.size)}
              <span className="block font-mono text-xs text-muted-foreground">
                {plan.binlogs.first} → {plan.binlogs.last}
                {plan.binlogs.partial ? " (last file is an open-file copy)" : ""}
              </span>
            </Row>
          )}
          {plan.wal && (
            <Row label="WAL-G">
              base {plan.wal.base_backup} · {formatUtc(plan.wal.base_time)}
              {plan.wal.uncompressed_size != null && ` · ${formatBytes(plan.wal.uncompressed_size)}`}
            </Row>
          )}
          {plan.image && (
            <Row label="Image">
              <span className="font-mono text-xs">{plan.image}</span>
            </Row>
          )}
          <Row label="Where">{plan.where}</Row>
        </dl>

        {disk && (
          <div className="space-y-1.5 rounded-md border p-3">
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="flex items-center gap-1.5 font-medium">
                <HardDrive className="h-3.5 w-3.5" />
                Disk on <span className="font-mono">{disk.path}</span>
              </span>
              <span className={disk.ok ? "text-muted-foreground" : "text-destructive"}>
                needs {formatBytes(disk.needed_bytes)} of {formatBytes(disk.free_bytes)} free
              </span>
            </div>
            <Progress value={diskPct} className={disk.ok ? "h-1.5" : "h-1.5 [&>*]:bg-destructive"} />
          </div>
        )}

        {error && (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" />
            <AlertTitle>Can't run this restore</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {plan.steps.length > 0 && (
          <div>
            <h4 className="mb-1.5 font-medium">Steps</h4>
            <ol className="list-decimal space-y-0.5 pl-5 text-muted-foreground">
              {plan.steps.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          </div>
        )}

        {plan.warnings.length > 0 && (
          <Alert>
            <Info className="h-4 w-4" />
            <AlertTitle>Worth knowing</AlertTitle>
            <AlertDescription>
              <ul className="list-disc space-y-1 pl-4">
                {plan.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}
