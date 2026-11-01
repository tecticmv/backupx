import { Loader2, Timer } from "lucide-react";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface PlanStepsSummaryProps {
  steps: string[];
  downtime: string;
  /** A re-plan for a changed selection is in flight (the shown plan is the previous one). */
  loading?: boolean;
  error?: string | null;
}

/** "What will happen" + expected downtime of the current selection. */
export default function PlanStepsSummary({ steps, downtime, loading = false, error = null }: PlanStepsSummaryProps) {
  return (
    <Card className={loading ? "opacity-70 transition-opacity" : "transition-opacity"} aria-busy={loading}>
      <CardHeader>
        <CardTitle className="text-base">What will happen</CardTitle>
        <CardDescription>In this order; a failed step rolls back what it changed.</CardDescription>
        {loading && (
          <CardAction>
            <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Updating
            </span>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {steps.length ? (
          <ol className="list-decimal space-y-1 pl-5 text-muted-foreground">
            {steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        ) : (
          <p className="text-muted-foreground">Nothing selected.</p>
        )}
        <div className="flex items-start gap-2 rounded-md border bg-muted/40 p-3">
          <Timer className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div>
            <span className="font-medium">Expected downtime: </span>
            <span className="text-muted-foreground">{downtime}</span>
          </div>
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
      </CardContent>
    </Card>
  );
}
