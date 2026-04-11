import { useState, useEffect, useCallback } from "react";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import {
  Folder,
  FolderOpen,
  FileText,
  ChevronRight,
  ArrowUp,
  Loader2,
  Check,
  FolderPlus,
} from "lucide-react";

interface DirectoryEntry {
  name: string;
  path: string;
  type?: 'directory' | 'file';
}

interface DirectoryBrowserProps {
  serverId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  selectedPaths: string[];
  onSelect: (paths: string[]) => void;
}

export default function DirectoryBrowser({
  serverId,
  open,
  onOpenChange,
  selectedPaths,
  onSelect,
}: DirectoryBrowserProps) {
  const [currentPath, setCurrentPath] = useState("/");
  const [directories, setDirectories] = useState<DirectoryEntry[]>([]);
  const [files, setFiles] = useState<DirectoryEntry[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (open) {
      setSelected(new Set(selectedPaths.filter(Boolean)));
      setCurrentPath("/");
    }
  }, [open, selectedPaths]);

  const fetchDirectories = useCallback(async (path: string) => {
    setIsLoading(true);
    try {
      const response = await fetch(
        `/api/servers/${serverId}/browse?path=${encodeURIComponent(path)}`
      );
      if (response.ok) {
        const data = await response.json();
        setDirectories(data.directories || []);
        setFiles(data.files || []);
      } else {
        const data = await response.json();
        toast.error(data.error || "Failed to browse directories");
        setDirectories([]);
        setFiles([]);
      }
    } catch {
      toast.error("Failed to connect to server");
      setDirectories([]);
      setFiles([]);
    } finally {
      setIsLoading(false);
    }
  }, [serverId]);

  useEffect(() => {
    if (open && serverId) {
      fetchDirectories(currentPath);
    }
  }, [open, serverId, currentPath, fetchDirectories]);

  const navigateTo = (path: string) => {
    setCurrentPath(path);
  };

  const goUp = () => {
    if (currentPath === "/") return;
    const parent = currentPath.replace(/\/[^/]+\/?$/, "") || "/";
    navigateTo(parent);
  };

  const toggleSelect = (path: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  };

  const handleConfirm = () => {
    onSelect(Array.from(selected).sort());
    onOpenChange(false);
  };

  const breadcrumbs = currentPath === "/"
    ? ["/"]
    : ["/", ...currentPath.split("/").filter(Boolean)];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg max-h-[80vh] flex flex-col p-0 overflow-hidden">
        <DialogHeader className="px-6 pt-6 pb-3 shrink-0">
          <DialogTitle className="flex items-center gap-2">
            <FolderPlus className="h-5 w-5" />
            Browse Directories
          </DialogTitle>
          <div className="flex items-center gap-1 text-sm text-muted-foreground font-mono mt-2 flex-wrap">
            {breadcrumbs.map((segment, i) => {
              const path = i === 0
                ? "/"
                : "/" + breadcrumbs.slice(1, i + 1).join("/");
              return (
                <span key={i} className="flex items-center gap-1">
                  {i > 0 && <ChevronRight className="h-3 w-3" />}
                  <button
                    type="button"
                    onClick={() => navigateTo(path)}
                    className="hover:text-foreground hover:underline"
                  >
                    {segment === "/" ? "root" : segment}
                  </button>
                </span>
              );
            })}
          </div>
        </DialogHeader>

        <div className="px-6 pb-2 shrink-0">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={goUp}
            disabled={currentPath === "/" || isLoading}
            className="gap-1"
          >
            <ArrowUp className="h-4 w-4" />
            Parent directory
          </Button>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto px-6">
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : directories.length === 0 && files.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground text-sm">
              Empty directory
            </div>
          ) : (
            <div className="space-y-0.5 pb-4">
              {directories.map((dir) => {
                const isSelected = selected.has(dir.path);
                return (
                  <div
                    key={dir.path}
                    className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm ${
                      isSelected
                        ? "border-primary bg-primary/10"
                        : "border-transparent hover:bg-muted/50"
                    }`}
                  >
                    <button
                      type="button"
                      onClick={() => toggleSelect(dir.path)}
                      className={`flex items-center justify-center h-5 w-5 rounded border shrink-0 ${
                        isSelected
                          ? "bg-primary border-primary text-primary-foreground"
                          : "border-muted-foreground/30"
                      }`}
                    >
                      {isSelected && <Check className="h-3 w-3" />}
                    </button>
                    <button
                      type="button"
                      onClick={() => navigateTo(dir.path)}
                      className="flex items-center gap-2 flex-1 min-w-0 text-left hover:text-primary"
                    >
                      {isSelected ? (
                        <FolderOpen className="h-4 w-4 text-primary shrink-0" />
                      ) : (
                        <Folder className="h-4 w-4 text-muted-foreground shrink-0" />
                      )}
                      <span className="truncate font-mono">{dir.name}</span>
                      <ChevronRight className="h-4 w-4 text-muted-foreground ml-auto shrink-0" />
                    </button>
                  </div>
                );
              })}
              {files.map((file) => (
                <div
                  key={file.path}
                  className="flex items-center gap-2 rounded-md px-3 py-2 text-sm opacity-60"
                >
                  <div className="h-5 w-5 shrink-0" />
                  <FileText className="h-4 w-4 text-muted-foreground shrink-0" />
                  <span className="truncate font-mono text-muted-foreground">{file.name}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <DialogFooter className="px-6 py-4 border-t shrink-0">
          <div className="flex items-center justify-between w-full">
            <span className="text-sm text-muted-foreground">
              {selected.size} {selected.size === 1 ? "directory" : "directories"} selected
            </span>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => onOpenChange(false)}
              >
                Cancel
              </Button>
              <Button type="button" onClick={handleConfirm}>
                Add Selected
              </Button>
            </div>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
