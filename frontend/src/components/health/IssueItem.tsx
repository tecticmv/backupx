import { useState } from "react";
import { Link } from "react-router-dom";
import {
  BellOff,
  BellRing,
  ChevronDown,
  ClipboardList,
  FileText,
  History,
  ListChecks,
  Loader2,
  MoreHorizontal,
  RefreshCw,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import CopyButton from "@/components/health/CopyButton";
import { formatDateTime, formatRelative, formatValue, humanizeKey } from "@/lib/format";
import { SEVERITY_META, isSnoozed, resourceLabel } from "@/lib/health";
import { cn } from "@/lib/utils";
import { useTasks } from "@/hooks/useTasks";
import { isActiveTask } from "@/types/task";
import type { Issue } from "@/types/health";

const SNOOZE_OPTIONS = [
  { hours: 1, label: "1 hour" },
  { hours: 24, label: "24 hours" },
  { hours: 24 * 7, label: "7 days" },
];

export interface IssueItemActions {
  fixSafe: (issue: Issue) => Promise<unknown>;
  review: (issue: Issue) => void;
  recheckIssue: (issue: Issue) => Promise<unknown>;
  snooze: (issue: Issue, hours: number) => Promise<unknown>;
}

function resourceHref(issue: Issue): string | null {
  const r = issue.resource;
  if (r.job_id) return `/jobs/${r.job_id}`;
  if (r.type === "job") return `/jobs/${r.id}`;
  if (r.type === "server") return "/servers";
  if (r.type === "storage") return "/storage";
  if (r.type === "channel") return "/notifications";
  return null;
}

export default function IssueItem({
  issue,
  actions,
  hideResource = false,
}: {
  issue: Issue;
  actions: IssueItemActions;
  hideResource?: boolean;
}) {
  const { tasks, openTray } = useTasks();
  // Task started from this browser before the next /api/health poll shows it on the issue
  const [localTaskId, setLocalTaskId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [rechecking, setRechecking] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);

  const localTask = localTaskId ? tasks.find((t) => t.id === localTaskId) : undefined;
  const activeTask =
    (issue.active_task && isActiveTask(issue.active_task) ? issue.active_task : null) ??
    (localTask && isActiveTask(localTask) ? localTask : null);
  const busy = starting || !!activeTask;
  const snoozed = isSnoozed(issue);
  const meta = SEVERITY_META[issue.severity];
  const href = resourceHref(issue);
  const evidence = Object.entries(issue.evidence ?? {});
  const tier = issue.fix?.tier ?? (issue.manual ? "manual" : null);

  const onFix = async () => {
    setStarting(true);
    try {
      const task = await actions.fixSafe(issue);
      if (task && typeof task === "object" && "id" in task) setLocalTaskId(String(task.id));
    } finally {
      setStarting(false);
    }
  };

  const onRecheck = async () => {
    setRechecking(true);
    try {
      await actions.recheckIssue(issue);
    } finally {
      setRechecking(false);
    }
  };

  return (
    <div
      className={cn(
        "rounded-lg border border-l-4 bg-card p-4 transition-colors",
        meta.border,
        snoozed && "opacity-70"
      )}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="font-medium leading-tight">{issue.title}</h4>
            {!hideResource &&
              (href ? (
                <Badge variant="outline" asChild>
                  <Link to={href} className="max-w-full truncate">
                    {resourceLabel(issue)}
                  </Link>
                </Badge>
              ) : (
                <Badge variant="outline">{resourceLabel(issue)}</Badge>
              ))}
            {snoozed && (
              <Badge variant="secondary" className="gap-1">
                <BellOff />
                Snoozed until {formatDateTime(issue.snoozed_until)}
              </Badge>
            )}
          </div>
          <p className="text-sm text-muted-foreground">{issue.explanation}</p>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pt-1 text-xs text-muted-foreground">
            <Tooltip>
              <TooltipTrigger asChild>
                <span tabIndex={0}>First seen {formatRelative(issue.first_seen)}</span>
              </TooltipTrigger>
              <TooltipContent>
                First seen {formatDateTime(issue.first_seen)} · last seen {formatDateTime(issue.last_seen)}
              </TooltipContent>
            </Tooltip>
            {evidence.length > 0 && (
              <HoverCard openDelay={150}>
                <HoverCardTrigger asChild>
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 underline decoration-dotted underline-offset-4 hover:text-foreground"
                  >
                    <ClipboardList className="h-3 w-3" />
                    Evidence ({evidence.length})
                  </button>
                </HoverCardTrigger>
                <HoverCardContent align="start" className="w-80">
                  <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-xs">
                    {evidence.map(([key, value]) => (
                      <div key={key} className="contents">
                        <dt className="text-muted-foreground">{humanizeKey(key)}</dt>
                        <dd className="break-all font-mono">{formatValue(value)}</dd>
                      </div>
                    ))}
                  </dl>
                </HoverCardContent>
              </HoverCard>
            )}
            {activeTask && (
              <button
                type="button"
                onClick={() => openTray(activeTask.id)}
                className="inline-flex items-center gap-1 text-primary hover:underline"
              >
                <Loader2 className="h-3 w-3 animate-spin" />
                {activeTask.step || (activeTask.status === "queued" ? "Queued" : "Fixing…")}
              </button>
            )}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {issue.wizard?.kind === "pitr_enable" && issue.wizard.job_id && (
            <Button size="sm" variant={tier === "safe" || tier === "confirm" ? "outline" : "default"} asChild>
              <Link to={`/jobs/${encodeURIComponent(issue.wizard.job_id)}/pitr-setup`}>
                <History />
                Open PITR setup
              </Link>
            </Button>
          )}
          {tier === "safe" && issue.fix && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button size="sm" onClick={onFix} disabled={busy}>
                  {busy ? <Loader2 className="animate-spin" /> : <Wrench />}
                  {issue.fix.label || "Fix"}
                </Button>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">{issue.fix.description}</TooltipContent>
            </Tooltip>
          )}
          {tier === "confirm" && issue.fix && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button size="sm" variant="outline" onClick={() => actions.review(issue)} disabled={busy}>
                  {busy ? <Loader2 className="animate-spin" /> : <ShieldCheck />}
                  Review
                </Button>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">
                {issue.fix.label}: {issue.fix.description}
              </TooltipContent>
            </Tooltip>
          )}
          {tier === "manual" && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => setManualOpen((o) => !o)}
              aria-expanded={manualOpen}
            >
              <ListChecks />
              How to fix
            </Button>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="icon-sm" variant="ghost" aria-label="More actions">
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onClick={onRecheck} disabled={rechecking}>
                <RefreshCw className="mr-2 h-4 w-4" />
                Re-check
              </DropdownMenuItem>
              {activeTask && (
                <DropdownMenuItem onClick={() => openTray(activeTask.id)}>
                  <Loader2 className="mr-2 h-4 w-4" />
                  Show fix progress
                </DropdownMenuItem>
              )}
              <DropdownMenuSeparator />
              {snoozed ? (
                <DropdownMenuItem onClick={() => actions.snooze(issue, 0)}>
                  <BellRing className="mr-2 h-4 w-4" />
                  Unsnooze
                </DropdownMenuItem>
              ) : (
                <>
                  <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">Snooze for</DropdownMenuLabel>
                  {SNOOZE_OPTIONS.map((o) => (
                    <DropdownMenuItem key={o.hours} onClick={() => actions.snooze(issue, o.hours)}>
                      <BellOff className="mr-2 h-4 w-4" />
                      {o.label}
                    </DropdownMenuItem>
                  ))}
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      {issue.evidence_text && (
        <Collapsible className="mt-3">
          <CollapsibleTrigger asChild>
            <Button variant="ghost" size="sm" className="group h-7 px-2 text-xs text-muted-foreground">
              <FileText />
              Log excerpt
              <ChevronDown className="transition-transform group-data-[state=open]:rotate-180" />
            </Button>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md border bg-muted/40 p-3 font-mono text-xs">
              {issue.evidence_text}
            </pre>
          </CollapsibleContent>
        </Collapsible>
      )}

      {tier === "manual" && (
        <Collapsible open={manualOpen} onOpenChange={setManualOpen}>
          <CollapsibleContent>
            <div className="mt-3 space-y-3 rounded-md border bg-muted/30 p-3 text-sm">
              {!!issue.manual?.steps?.length && (
                <ol className="list-decimal space-y-1 pl-5">
                  {issue.manual.steps.map((step, i) => (
                    <li key={i}>{step}</li>
                  ))}
                </ol>
              )}
              {!!issue.manual?.commands?.length && (
                <div className="space-y-2">
                  {issue.manual.commands.map((cmd, i) => (
                    <div key={i} className="flex items-start gap-1 rounded-md border bg-background">
                      <pre className="min-w-0 flex-1 overflow-x-auto whitespace-pre p-2 font-mono text-xs">{cmd}</pre>
                      <CopyButton text={cmd} label="Copy command" />
                    </div>
                  ))}
                </div>
              )}
              {!issue.manual?.steps?.length && !issue.manual?.commands?.length && (
                <p className="text-muted-foreground">{issue.fix?.description || "Fix this outside backupx."}</p>
              )}
              <div className="flex items-center gap-2">
                <Button size="sm" variant="secondary" onClick={onRecheck} disabled={rechecking}>
                  {rechecking ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                  Re-check
                </Button>
                <span className="text-xs text-muted-foreground">after you've made the change</span>
              </div>
            </div>
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  );
}
