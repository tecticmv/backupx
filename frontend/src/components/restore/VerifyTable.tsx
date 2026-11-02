import { Badge } from "@/components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import type { Verify } from "@/types/restore";

const num = (n: number | null) => (n == null ? "—" : n.toLocaleString());

function MatchBadge({ match }: { match: boolean | null }) {
  if (match === null) return <Badge variant="outline" className="text-muted-foreground">n/a</Badge>;
  return match ? (
    <Badge variant="outline" className="border-emerald-500/50 text-emerald-600 dark:text-emerald-400">
      match
    </Badge>
  ) : (
    <Badge variant="destructive">mismatch</Badge>
  );
}

/**
 * Row counts per table, production vs restored; key tables first, mismatches highlighted.
 * copyOnly: production wasn't queried, so only the restored copy's counts are shown.
 */
export default function VerifyTable({ tables, copyOnly = false }: { tables: Verify["tables"]; copyOnly?: boolean }) {
  const rows = [...tables].sort((a, b) => {
    if (a.key !== b.key) return a.key ? -1 : 1;
    if ((a.match === false) !== (b.match === false)) return a.match === false ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  if (!rows.length) return <p className="text-sm text-muted-foreground">No tables found in the restored copy.</p>;

  return (
    <div className="max-h-[480px] overflow-auto rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Table</TableHead>
            {!copyOnly && <TableHead className="text-right">Prod (est.)</TableHead>}
            <TableHead className="text-right">Restored (est.)</TableHead>
            {!copyOnly && <TableHead className="text-right">Prod exact</TableHead>}
            <TableHead className="text-right">Restored exact</TableHead>
            {!copyOnly && <TableHead>Match</TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((t) => (
            <TableRow key={t.name} className={cn(t.match === false && "bg-destructive/10 hover:bg-destructive/15")}>
              <TableCell className="font-mono text-xs">
                {t.name}
                {t.key && (
                  <Badge variant="secondary" className="ml-2">
                    key
                  </Badge>
                )}
              </TableCell>
              {!copyOnly && <TableCell className="text-right tabular-nums">{num(t.prod_estimate)}</TableCell>}
              <TableCell className="text-right tabular-nums">{num(t.restored_estimate)}</TableCell>
              {!copyOnly && <TableCell className="text-right tabular-nums">{num(t.prod_exact)}</TableCell>}
              <TableCell className="text-right tabular-nums">{num(t.restored_exact)}</TableCell>
              {!copyOnly && (
                <TableCell>
                  <MatchBadge match={t.match} />
                </TableCell>
              )}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
