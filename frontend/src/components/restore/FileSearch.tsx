import { useEffect, useState } from "react";
import { File, Folder, Loader2 } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { formatBytes } from "@/lib/format";
import { findInSnapshot } from "@/lib/restore";
import type { FindMatch } from "@/types/restore";

interface FileSearchProps {
  jobId: string;
  snapshotId: string;
  storageId: string | null;
  selected: Set<string>;
  onToggle: (path: string, checked: boolean) => void;
}

const MIN_QUERY = 2;
const DEBOUNCE_MS = 350;

/** Search a snapshot with restic find (server-side, debounced); pick results with checkboxes. */
export default function FileSearch({ jobId, snapshotId, storageId, selected, onToggle }: FileSearchProps) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<{ q: string; matches: FindMatch[]; truncated: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const q = query.trim();
  const active = q.length >= MIN_QUERY;
  const loading = active && result?.q !== q && !error;

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      findInSnapshot(jobId, snapshotId, q, storageId, controller.signal)
        .then((data) => {
          setError(null);
          setResult({ q, ...data });
        })
        .catch((err) => {
          if (controller.signal.aborted) return;
          setError(err instanceof Error ? err.message : "Search failed");
        });
    }, DEBOUNCE_MS);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [jobId, snapshotId, storageId, q, active]);

  return (
    <Command shouldFilter={false} className="rounded-md border">
      <CommandInput
        placeholder="Search this backup (at least 2 characters)..."
        value={query}
        onValueChange={(v) => {
          setQuery(v);
          setError(null);
        }}
      />
      {active && (
        <CommandList className="max-h-64">
          {loading ? (
            <div className="flex items-center gap-2 p-3 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Searching...
            </div>
          ) : error ? (
            <p className="p-3 text-sm text-destructive">{error}</p>
          ) : (
            <>
              <CommandEmpty>Nothing matches "{q}".</CommandEmpty>
              {result && result.matches.length > 0 && (
                <CommandGroup heading={result.truncated ? "First 500 matches (refine the search)" : `${result.matches.length} matches`}>
                  {result.matches.map((m) => {
                    const checked = selected.has(m.path);
                    return (
                      <CommandItem key={m.path} value={m.path} onSelect={() => onToggle(m.path, !checked)}>
                        <Checkbox checked={checked} tabIndex={-1} aria-hidden className="pointer-events-none" />
                        {m.type === "dir" ? <Folder /> : <File />}
                        <span className="min-w-0 flex-1 truncate font-mono text-xs">{m.path}</span>
                        {m.type === "file" && (
                          <span className="text-xs text-muted-foreground">{formatBytes(m.size)}</span>
                        )}
                      </CommandItem>
                    );
                  })}
                </CommandGroup>
              )}
            </>
          )}
        </CommandList>
      )}
    </Command>
  );
}
