import { AlertTriangle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import PitrTimePicker from "@/components/restore/PitrTimePicker";
import { formatBytes, formatRelative } from "@/lib/format";
import { formatLocal, formatUtc } from "@/lib/restore";
import type { PointType, RestorePoints } from "@/types/restore";

const INLINE_STORAGE = "__inline";

interface PointPickerProps {
  points: RestorePoints | null;
  loading: boolean;
  error: string | null;
  storageId: string | null;
  onStorage: (id: string | null) => void;
  pointType: PointType;
  onPointType: (t: PointType) => void;
  snapshotId: string | null;
  onSnapshot: (id: string) => void;
  timeMs: number | null;
  onTime: (ms: number) => void;
}

/** Step 2: which storage and which point in time to restore. */
export default function PointPicker(props: PointPickerProps) {
  const { points, loading, error, pointType } = props;

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertTriangle className="h-4 w-4" />
        <AlertTitle>Could not load restore points</AlertTitle>
        <AlertDescription className="break-all">{error}</AlertDescription>
      </Alert>
    );
  }
  if (!points || loading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-9 w-64" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  const pitrAvailable = points.pitr.enabled && !!points.pitr.min && !!points.pitr.max && points.backup_type === "database";
  const selectedStorage = props.storageId ?? points.storage_id;

  return (
    <div className="space-y-5">
      {points.storages.length > 1 && (
        <div className="grid gap-2">
          <Label>Storage</Label>
          <Select
            value={selectedStorage ?? INLINE_STORAGE}
            onValueChange={(v) => props.onStorage(v === INLINE_STORAGE ? null : v)}
          >
            <SelectTrigger className="w-72">
              <SelectValue placeholder="Storage" />
            </SelectTrigger>
            <SelectContent>
              {points.storages.map((s) => (
                <SelectItem key={s.id ?? INLINE_STORAGE} value={s.id ?? INLINE_STORAGE}>
                  {s.name}
                  {s.primary ? " (primary)" : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}

      <RadioGroup value={pointType} onValueChange={(v) => props.onPointType(v as PointType)} className="gap-3">
        <Label
          htmlFor="point-latest"
          className="flex cursor-pointer items-start gap-3 rounded-md border p-3 font-normal has-[[data-state=checked]]:border-primary"
        >
          <RadioGroupItem value="latest" id="point-latest" className="mt-0.5" disabled={!points.latest} />
          <div className="grid gap-1">
            <span className="font-medium">Latest restorable</span>
            <span className="text-sm text-muted-foreground">
              {points.latest ? points.latest.label : "Nothing restorable on this storage yet"}
            </span>
          </div>
        </Label>
        <Label
          htmlFor="point-snapshot"
          className="flex cursor-pointer items-start gap-3 rounded-md border p-3 font-normal has-[[data-state=checked]]:border-primary"
        >
          <RadioGroupItem value="snapshot" id="point-snapshot" className="mt-0.5" disabled={!points.snapshots.length} />
          <div className="grid gap-1">
            <span className="font-medium">A specific backup</span>
            <span className="text-sm text-muted-foreground">
              {points.snapshots.length} snapshot{points.snapshots.length === 1 ? "" : "s"} on this storage
            </span>
          </div>
        </Label>
        {pitrAvailable && (
          <Label
            htmlFor="point-time"
            className="flex cursor-pointer items-start gap-3 rounded-md border p-3 font-normal has-[[data-state=checked]]:border-primary"
          >
            <RadioGroupItem value="time" id="point-time" className="mt-0.5" />
            <div className="grid gap-1">
              <span className="font-medium">Exact time (point-in-time recovery)</span>
              <span className="text-sm text-muted-foreground">
                Any second between {formatUtc(points.pitr.min)} and {formatUtc(points.pitr.max)}
              </span>
            </div>
          </Label>
        )}
      </RadioGroup>

      {points.snapshots_error && (
        <Alert variant="destructive">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>Could not read snapshots from this storage</AlertTitle>
          <AlertDescription className="break-all">{points.snapshots_error}</AlertDescription>
        </Alert>
      )}

      {pointType === "snapshot" && points.snapshots.length > 0 && (
        <ScrollArea className="h-72 rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-10" />
                <TableHead>Time</TableHead>
                <TableHead>ID</TableHead>
                <TableHead className="text-right">Size</TableHead>
                <TableHead>Tags</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {points.snapshots.map((s) => {
                const selected = props.snapshotId === s.id;
                return (
                  <TableRow
                    key={s.id}
                    data-state={selected ? "selected" : undefined}
                    className="cursor-pointer"
                    onClick={() => props.onSnapshot(s.id)}
                  >
                    <TableCell>
                      <RadioGroup value={selected ? s.id : ""} onValueChange={() => props.onSnapshot(s.id)}>
                        <RadioGroupItem value={s.id} aria-label={`Snapshot ${s.short_id ?? s.id.slice(0, 8)}`} />
                      </RadioGroup>
                    </TableCell>
                    <TableCell>
                      <p className="text-sm">{formatLocal(s.time)}</p>
                      <p className="font-mono text-xs text-muted-foreground">
                        {formatUtc(s.time)} · {formatRelative(s.time)}
                      </p>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className="font-mono text-xs">
                        {s.short_id ?? s.id.slice(0, 8)}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatBytes(s.summary?.total_bytes_processed ?? null)}
                    </TableCell>
                    <TableCell>
                      <div className="flex max-w-xs flex-wrap gap-1">
                        {(s.tags ?? []).map((t) => (
                          <Badge key={t} variant="secondary" className="text-xs">
                            {t}
                          </Badge>
                        ))}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </ScrollArea>
      )}

      {pointType === "time" && pitrAvailable && (
        <PitrTimePicker pitr={points.pitr} value={props.timeMs} onChange={props.onTime} />
      )}
    </div>
  );
}
