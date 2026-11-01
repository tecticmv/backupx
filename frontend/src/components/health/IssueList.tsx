import { useMemo, useState } from "react";
import { CheckCircle2 } from "lucide-react";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import IssueItem, { type IssueItemActions } from "@/components/health/IssueItem";
import FixReviewDialog from "@/components/health/FixReviewDialog";
import { SEVERITY_META, SEVERITY_ORDER } from "@/lib/health";
import { cn } from "@/lib/utils";
import { useHealthActions } from "@/hooks/useHealth";
import type { Issue, Severity } from "@/types/health";

interface IssueListProps {
  issues: Issue[];
  /** Re-fetch health (and anything derived) after a change or a finished task. */
  onChanged: () => void;
  loading?: boolean;
  /** Hide the resource badge (e.g. on a job's own page). */
  hideResource?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
}

/** Issues grouped by severity (Critical / Warning / Info) with their fix actions. */
export default function IssueList({
  issues,
  onChanged,
  loading = false,
  hideResource = false,
  emptyTitle = "No issues",
  emptyDescription = "Every check passed.",
}: IssueListProps) {
  const { fixSafe, recheckIssue, snooze } = useHealthActions(onChanged);
  const [reviewIssue, setReviewIssue] = useState<Issue | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);

  const groups = useMemo(() => {
    const by: Record<Severity, Issue[]> = { critical: [], warning: [], info: [] };
    for (const issue of issues) (by[issue.severity] ?? by.info).push(issue);
    return SEVERITY_ORDER.filter((s) => by[s].length > 0).map((s) => ({ severity: s, issues: by[s] }));
  }, [issues]);

  // Critical and warnings start open; info collapsed. Groups that appear later open too.
  const [closed, setClosed] = useState<Set<Severity>>(new Set(["info"]));
  const openValues = groups.map((g) => g.severity).filter((s) => !closed.has(s));

  const actions: IssueItemActions = {
    fixSafe,
    recheckIssue,
    snooze,
    review: (issue) => {
      setReviewIssue(issue);
      setReviewOpen(true);
    },
  };

  if (loading && issues.length === 0) {
    return (
      <div className="space-y-3" aria-busy="true" aria-label="Loading issues">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  if (issues.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-10 text-center">
        <CheckCircle2 className="mb-3 h-10 w-10 text-emerald-500" />
        <h3 className="font-medium">{emptyTitle}</h3>
        <p className="mt-1 text-sm text-muted-foreground">{emptyDescription}</p>
      </div>
    );
  }

  return (
    <>
      <Accordion
        type="multiple"
        value={openValues}
        onValueChange={(values) =>
          setClosed(new Set(SEVERITY_ORDER.filter((s) => !values.includes(s))))
        }
        className="space-y-2"
      >
        {groups.map(({ severity, issues: list }) => {
          const meta = SEVERITY_META[severity];
          const Icon = meta.icon;
          return (
            <AccordionItem key={severity} value={severity} className="border-b-0">
              <AccordionTrigger className="py-2 hover:no-underline">
                <span className="flex items-center gap-2">
                  <Icon className={cn("h-4 w-4", meta.text)} />
                  <span className="font-semibold">{meta.plural}</span>
                  <Badge variant="outline" className={meta.badge}>
                    {list.length}
                  </Badge>
                </span>
              </AccordionTrigger>
              <AccordionContent className="space-y-2 pb-2">
                {list.map((issue) => (
                  <IssueItem key={issue.id} issue={issue} actions={actions} hideResource={hideResource} />
                ))}
              </AccordionContent>
            </AccordionItem>
          );
        })}
      </Accordion>
      <FixReviewDialog issue={reviewIssue} open={reviewOpen} onOpenChange={setReviewOpen} onChanged={onChanged} />
    </>
  );
}
