import { Link } from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import RestoreStatusBadge from "@/components/restore/RestoreStatusBadge";
import { usePolledJson } from "@/hooks/useHealth";
import { formatRelative } from "@/lib/format";
import { describePoint } from "@/lib/restore";
import type { Restore } from "@/types/restore";

/** This job's restores, newest first, each linking into the wizard. */
export default function JobRestoresList({ jobId }: { jobId: string }) {
  const { data, error, loading } = usePolledJson<{ restores: Restore[] }>(
    `/api/restores?job_id=${encodeURIComponent(jobId)}&limit=20`
  );

  if (loading && !data) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    );
  }
  if (error && !data) return <p className="text-sm text-destructive">{error}</p>;
  const restores = data?.restores ?? [];
  if (!restores.length) return <p className="text-sm text-muted-foreground">No restores of this job yet.</p>;

  return (
    <ul className="divide-y rounded-md border">
      {restores.map((r) => (
        <li key={r.id}>
          <Link
            to={`/restore?restore=${encodeURIComponent(r.id)}`}
            className="flex items-center gap-3 px-3 py-2.5 text-sm hover:bg-muted/50"
          >
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium">
                {describePoint(r)} <span className="font-normal text-muted-foreground">→ {r.target_type}</span>
              </p>
              <p className="truncate text-xs text-muted-foreground">
                {formatRelative(r.created_at)}
                {r.created_by ? ` by ${r.created_by}` : ""}
                {r.parent_id ? " · undo" : ""}
              </p>
            </div>
            <RestoreStatusBadge status={r.status} />
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          </Link>
        </li>
      ))}
    </ul>
  );
}
