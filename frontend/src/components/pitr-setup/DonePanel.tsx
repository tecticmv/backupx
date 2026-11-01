import { Link } from "react-router-dom";
import { ArrowLeft, CheckCircle2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import PitrBadge from "@/components/PitrBadge";
import { formatWindow, PITR_STATE_HELP } from "@/lib/pitr";
import type { PitrEnableResult } from "@/types/pitrSetup";
import type { PitrDetails } from "@/types/job";

interface DonePanelProps {
  jobId: string;
  jobName: string | null;
  pitr: PitrDetails | null;
  result: PitrEnableResult | null;
}

/** Done step: PITR state, restorable window and the way back to the job. */
export default function DonePanel({ jobId, jobName, pitr, result }: DonePanelProps) {
  const windowStart = pitr?.window_start ?? result?.window_start ?? null;
  const windowEnd = pitr?.window_end ?? result?.window_end ?? null;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <CheckCircle2 className="h-5 w-5 text-emerald-500" />
          Point-in-time recovery is on
          {pitr && <PitrBadge pitr={pitr} />}
        </CardTitle>
        <CardDescription>{result?.message || (jobName ? `${jobName} can now be restored to any second in its window.` : null)}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div className="rounded-md border bg-muted/40 p-3">
          <p className="font-medium">Restorable window</p>
          <p className="text-muted-foreground">{formatWindow(windowStart, windowEnd)}</p>
          {pitr && <p className="mt-1 text-xs text-muted-foreground">{PITR_STATE_HELP[pitr.state]}</p>}
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="outline" asChild>
            <Link to={`/jobs/${encodeURIComponent(jobId)}?tab=recovery`}>
              <ArrowLeft />
              Back to {jobName ?? "the job"}
            </Link>
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
