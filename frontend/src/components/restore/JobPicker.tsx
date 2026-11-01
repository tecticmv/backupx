import { useEffect, useMemo, useState } from "react";
import { Check, Database, FolderSync } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Skeleton } from "@/components/ui/skeleton";
import PitrBadge from "@/components/PitrBadge";
import { apiJson } from "@/lib/api";
import type { DatabaseConfig } from "@/types/database";
import type { Jobs } from "@/types/job";

interface JobPickerProps {
  value: string | null;
  onSelect: (jobId: string) => void;
}

/** Step 1: searchable list of backup jobs. */
export default function JobPicker({ value, onSelect }: JobPickerProps) {
  const [jobs, setJobs] = useState<Jobs | null>(null);
  const [engines, setEngines] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiJson<Jobs>("/api/jobs")
      .then((data) => !cancelled && setJobs(data))
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : "Could not load jobs"));
    // Engine (MySQL/PostgreSQL) comes from the job's database config; only the type is read
    apiJson<DatabaseConfig[] | { databases?: DatabaseConfig[] }>("/api/databases")
      .then((data) => {
        if (cancelled) return;
        const list = Array.isArray(data) ? data : (data.databases ?? []);
        setEngines(Object.fromEntries(list.map((d) => [d.id, d.type])));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const entries = useMemo(
    () => Object.entries(jobs ?? {}).sort(([, a], [, b]) => a.name.localeCompare(b.name)),
    [jobs]
  );

  if (error) return <p className="text-sm text-destructive">{error}</p>;
  if (!jobs) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-9 w-full" />
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    );
  }

  return (
    <Command className="rounded-lg border">
      <CommandInput placeholder="Search backup jobs..." />
      <CommandList className="max-h-[420px]">
        <CommandEmpty>No job matches.</CommandEmpty>
        <CommandGroup heading="Backup jobs">
          {entries.map(([id, job]) => {
            const engine = job.backup_type === "database" ? engines[job.database_config_id ?? ""] : undefined;
            return (
              <CommandItem
                key={id}
                value={`${job.name} ${id}`}
                onSelect={() => onSelect(id)}
                className="gap-3 py-2"
              >
                {job.backup_type === "database" ? (
                  <Database className="text-muted-foreground" />
                ) : (
                  <FolderSync className="text-muted-foreground" />
                )}
                <div className="min-w-0 flex-1">
                  <p className="truncate font-medium">{job.name}</p>
                  <p className="truncate text-xs text-muted-foreground">
                    {job.backup_type === "database" ? "Database" : "Files"}
                    {engine ? ` · ${engine === "postgres" ? "PostgreSQL" : "MySQL / MariaDB"}` : ""}
                    {job.last_success ? ` · last backup ${new Date(job.last_success).toLocaleString()}` : ""}
                  </p>
                </div>
                <PitrBadge pitr={job.pitr} />
                {job.backup_type === "filesystem" && <Badge variant="secondary">Files</Badge>}
                {value === id && <Check className="text-primary" />}
              </CommandItem>
            );
          })}
        </CommandGroup>
      </CommandList>
    </Command>
  );
}
