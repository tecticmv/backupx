import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { PitrCheckResult } from "@/types/job";
import { CheckCircle2, AlertTriangle, XCircle, RotateCw } from "lucide-react";

const ICON = {
  ok: <CheckCircle2 className="h-4 w-4 text-emerald-500" />,
  warn: <AlertTriangle className="h-4 w-4 text-amber-500" />,
  fail: <XCircle className="h-4 w-4 text-destructive" />,
};

export default function PitrPrereqTable({ result }: { result: PitrCheckResult }) {
  return (
    <div className="space-y-2">
      <div className="min-w-0 max-w-full rounded-lg border">
        <Table className="min-w-[40rem] table-fixed">
          <TableHeader>
            <TableRow>
              <TableHead className="w-8" />
              <TableHead className="w-[22%] whitespace-normal">Check</TableHead>
              <TableHead className="w-[26%] whitespace-normal">Current</TableHead>
              <TableHead className="w-[22%] whitespace-normal">Required</TableHead>
              <TableHead className="whitespace-normal">Fix</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {result.checks.map((c) => (
              <TableRow key={c.check}>
                <TableCell className="align-top">{ICON[c.status]}</TableCell>
                <TableCell className="align-top whitespace-normal break-words text-xs font-medium">{c.check}</TableCell>
                <TableCell className="align-top whitespace-normal break-all font-mono text-xs">{c.current}</TableCell>
                <TableCell className="align-top whitespace-normal break-words text-xs text-muted-foreground">{c.required}</TableCell>
                <TableCell className="align-top whitespace-normal break-words text-xs">
                  {c.fix}
                  {c.needs_restart && c.status !== "ok" && (
                    <Badge variant="outline" className="ml-1 gap-1 border-amber-500/50 text-amber-500">
                      <RotateCw className="h-3 w-3" />
                      needs restart
                    </Badge>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {result.postgresql_conf && (
        <div className="rounded-lg border bg-muted/50 p-3">
          <p className="text-xs mb-2">
            postgresql.conf changes to apply in a restart window (backupx never edits the file or restarts
            PostgreSQL):
          </p>
          <pre className="text-xs font-mono whitespace-pre-wrap break-all">{result.postgresql_conf.join("\n")}</pre>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        {result.ok
          ? "All required checks pass."
          : "Fix the failed checks; the streamer can't run until they pass."}
        {result.needs_restart && " Some fixes need a database restart."}
      </p>
    </div>
  );
}
