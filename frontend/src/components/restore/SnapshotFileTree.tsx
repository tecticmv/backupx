import { useEffect, useState } from "react";
import { ChevronRight, File, Folder, Loader2 } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
} from "@/components/ui/sidebar";
import { Skeleton } from "@/components/ui/skeleton";
import { formatBytes } from "@/lib/format";
import { listSnapshotFiles } from "@/lib/restore";
import type { SnapshotFile } from "@/types/job";

// File tree of one snapshot, composed like the official shadcn `sidebar-11`
// block (Collapsible + SidebarMenu/SidebarMenuSub); children load lazily
// from /api/jobs/:id/snapshots/:sid/files?path= when a folder opens.

interface TreeContext {
  jobId: string;
  snapshotId: string;
  storageId: string | null;
  selected: Set<string>;
  onToggle: (path: string, checked: boolean) => void;
}

function useChildren(ctx: TreeContext, path: string, enabled: boolean) {
  const [files, setFiles] = useState<SnapshotFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || files) return;
    let cancelled = false;
    listSnapshotFiles(ctx.jobId, ctx.snapshotId, path, ctx.storageId)
      .then((data) => {
        if (cancelled) return;
        const sorted = [...data.files].sort((a, b) =>
          a.type === b.type ? a.name.localeCompare(b.name) : a.type === "dir" ? -1 : 1
        );
        setFiles(sorted);
      })
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : "Could not list files"));
    return () => {
      cancelled = true;
    };
  }, [ctx.jobId, ctx.snapshotId, ctx.storageId, path, enabled, files]);

  return { files, error };
}

function ChildList({ ctx, path, open }: { ctx: TreeContext; path: string; open: boolean }) {
  const { files, error } = useChildren(ctx, path, open);
  if (error) return <p className="px-2 py-1 text-xs text-destructive">{error}</p>;
  if (!files) {
    return (
      <div className="flex items-center gap-2 px-2 py-1 text-xs text-muted-foreground">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading...
      </div>
    );
  }
  if (!files.length) return <p className="px-2 py-1 text-xs text-muted-foreground">Empty</p>;
  return (
    <>
      {files.map((f) => (
        <Tree key={f.path} ctx={ctx} item={f} />
      ))}
    </>
  );
}

function Tree({ ctx, item }: { ctx: TreeContext; item: SnapshotFile }) {
  const [open, setOpen] = useState(false);
  const checked = ctx.selected.has(item.path);
  const box = (
    <Checkbox
      checked={checked}
      onCheckedChange={(v) => ctx.onToggle(item.path, v === true)}
      aria-label={`Select ${item.path}`}
      className="ml-1 shrink-0"
    />
  );

  if (item.type !== "dir") {
    return (
      <SidebarMenuItem className="flex items-center gap-1">
        {box}
        <SidebarMenuButton
          isActive={checked}
          className="data-[active=true]:bg-transparent"
          onClick={() => ctx.onToggle(item.path, !checked)}
        >
          <File />
          <span className="truncate">{item.name}</span>
          <span className="ml-auto text-xs text-muted-foreground">{formatBytes(item.size)}</span>
        </SidebarMenuButton>
      </SidebarMenuItem>
    );
  }

  return (
    <SidebarMenuItem>
      <Collapsible
        open={open}
        onOpenChange={setOpen}
        className="group/collapsible [&[data-state=open]>div>button>svg:first-child]:rotate-90"
      >
        <div className="flex items-center gap-1">
          {box}
          <CollapsibleTrigger asChild>
            <SidebarMenuButton>
              <ChevronRight className="transition-transform" />
              <Folder />
              <span className="truncate">{item.name}</span>
            </SidebarMenuButton>
          </CollapsibleTrigger>
        </div>
        <CollapsibleContent>
          <SidebarMenuSub className="mr-0 pr-0">
            {open && <ChildList ctx={ctx} path={item.path} open={open} />}
          </SidebarMenuSub>
        </CollapsibleContent>
      </Collapsible>
    </SidebarMenuItem>
  );
}

interface SnapshotFileTreeProps extends TreeContext {
  className?: string;
}

export default function SnapshotFileTree({ className, ...ctx }: SnapshotFileTreeProps) {
  const { files, error } = useChildren(ctx, "/", true);
  return (
    <ScrollArea className={className ?? "h-80 rounded-md border"}>
      <SidebarMenu className="p-2">
        {error ? (
          <p className="text-sm text-destructive">{error}</p>
        ) : !files ? (
          <div className="space-y-2">
            <Skeleton className="h-7 w-2/3" />
            <Skeleton className="h-7 w-1/2" />
            <Skeleton className="h-7 w-3/5" />
          </div>
        ) : (
          files.map((f) => <Tree key={f.path} ctx={ctx} item={f} />)
        )}
      </SidebarMenu>
    </ScrollArea>
  );
}
