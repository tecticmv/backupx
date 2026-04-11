import { useState, useEffect } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { toast } from "sonner";
import type { Job, Snapshot, RepoStats } from "@/types/job";
import {
  ArrowLeft,
  Play,
  Pencil,
  HardDrive,
  Camera,
  Loader2,
  Info,
  RefreshCw,
  FolderOpen,
  MoreHorizontal,
  RotateCcw,
  Archive,
  Database,
  AlertTriangle,
} from "lucide-react";

export default function Snapshots() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();

  const [job, setJob] = useState<Job | null>(null);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [stats, setStats] = useState<RepoStats | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isRunning, setIsRunning] = useState(false);

  // Restore dialog state
  const [restoreDialogOpen, setRestoreDialogOpen] = useState(false);
  const [restoreSnapshot, setRestoreSnapshot] = useState<Snapshot | null>(null);
  const [targetPath, setTargetPath] = useState("/");
  const [isRestoring, setIsRestoring] = useState(false);

  // Database restore dialog state
  const [dbRestoreDialogOpen, setDbRestoreDialogOpen] = useState(false);
  const [dbRestoreSnapshot, setDbRestoreSnapshot] = useState<Snapshot | null>(null);
  const [targetDatabase, setTargetDatabase] = useState("");
  const [confirmText, setConfirmText] = useState("");
  const [isDbRestoring, setIsDbRestoring] = useState(false);

  // Download ZIP state
  const [downloadingZip, setDownloadingZip] = useState<string | null>(null);

  useEffect(() => {
    fetchData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  const fetchData = async () => {
    try {
      const [jobsRes, snapshotsRes] = await Promise.all([
        fetch("/api/jobs"),
        fetch(`/api/jobs/${jobId}/snapshots`),
      ]);

      if (jobsRes.ok) {
        const jobs = await jobsRes.json();
        if (jobs[jobId!]) {
          setJob(jobs[jobId!]);
        }
      }

      if (snapshotsRes.ok) {
        const data = await snapshotsRes.json();
        setSnapshots(data.snapshots || []);
      }

      // Fetch stats in the background (slower)
      fetch(`/api/jobs/${jobId}/snapshots/stats`)
        .then((res) => res.ok ? res.json() : null)
        .then((data) => data && setStats(data.stats))
        .catch(() => {});
    } catch {
      toast.error("Failed to fetch data");
    } finally {
      setIsLoading(false);
    }
  };

  const runBackup = async () => {
    setIsRunning(true);
    try {
      const response = await fetch(`/api/jobs/${jobId}/run`, {
        method: "POST",
      });
      const data = await response.json();
      if (data.success) {
        toast.success("Backup started");
        fetchData();
      } else {
        toast.error(data.error || "Failed to start backup");
      }
    } catch {
      toast.error("Failed to start backup");
    } finally {
      setIsRunning(false);
    }
  };

  const openRestoreDialog = (snapshot: Snapshot) => {
    setRestoreSnapshot(snapshot);
    setTargetPath("/");
    setRestoreDialogOpen(true);
  };

  const openDbRestoreDialog = (snapshot: Snapshot) => {
    setDbRestoreSnapshot(snapshot);
    setTargetDatabase("");
    setConfirmText("");
    setDbRestoreDialogOpen(true);
  };

  const handleDbRestore = async () => {
    if (!dbRestoreSnapshot) return;

    setIsDbRestoring(true);
    try {
      const response = await fetch(
        `/api/jobs/${jobId}/snapshots/${dbRestoreSnapshot.id}/restore-db`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            target_database: targetDatabase || undefined,
          }),
        }
      );

      const data = await response.json();

      if (response.ok) {
        toast.success(data.message || "Database restored successfully");
        setDbRestoreDialogOpen(false);
      } else {
        toast.error(data.error || "Database restore failed");
      }
    } catch {
      toast.error("Database restore failed");
    } finally {
      setIsDbRestoring(false);
    }
  };

  const handleRestore = async () => {
    if (!restoreSnapshot || !targetPath) return;

    setIsRestoring(true);
    try {
      const response = await fetch(`/api/jobs/${jobId}/snapshots/${restoreSnapshot.id}/restore`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_path: "/",
          target_path: targetPath,
        }),
      });

      const data = await response.json();

      if (response.ok) {
        toast.success(data.message || "Restore completed successfully");
        setRestoreDialogOpen(false);
      } else {
        toast.error(data.error || "Restore failed");
      }
    } catch {
      toast.error("Restore failed");
    } finally {
      setIsRestoring(false);
    }
  };

  const handleDownloadZip = async (snapshot: Snapshot) => {
    setDownloadingZip(snapshot.id);

    try {
      // Download the root path of the snapshot as ZIP
      const url = `/api/jobs/${jobId}/snapshots/${snapshot.id}/download-zip?path=/`;
      const response = await fetch(url);

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || "Download failed");
      }

      const blob = await response.blob();
      const downloadUrl = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = downloadUrl;
      link.download = `snapshot-${snapshot.id.slice(0, 8)}.zip`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(downloadUrl);

      toast.success("Snapshot downloaded as ZIP");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Download failed");
    } finally {
      setDownloadingZip(null);
    }
  };

  const formatBytes = (bytes: number) => {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
  };

  const formatTime = (timestamp: string) => {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 1) return "Just now";
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;
    return date.toLocaleDateString();
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  if (!job) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <Camera className="h-12 w-12 text-muted-foreground/50 mb-4" />
        <h3 className="font-medium">Job not found</h3>
        <p className="text-sm text-muted-foreground mt-1 mb-4">
          The requested backup job does not exist
        </p>
        <Button onClick={() => navigate("/jobs")}>Go to Jobs</Button>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button variant="ghost" size="icon" onClick={() => navigate("/jobs")}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <h1 className="text-2xl font-bold">{job.name}</h1>
            <p className="text-sm text-muted-foreground">
              Snapshots and repository statistics
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={fetchData}>
            <RefreshCw className="h-4 w-4 mr-2" />
            Refresh
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={runBackup}
            disabled={isRunning || job.status === "running"}
          >
            {isRunning || job.status === "running" ? (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            ) : (
              <Play className="h-4 w-4 mr-2" />
            )}
            Run Backup
          </Button>
          <Link to={`/jobs/${jobId}/edit`}>
            <Button variant="outline" size="sm">
              <Pencil className="h-4 w-4 mr-2" />
              Edit
            </Button>
          </Link>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Total Size</CardTitle>
            <HardDrive className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {stats?.total_size != null ? formatBytes(stats.total_size) : "-"}
            </div>
            <p className="text-xs text-muted-foreground">
              Repository storage used
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Snapshots</CardTitle>
            <Camera className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-primary">{snapshots.length}</div>
            <p className="text-xs text-muted-foreground">
              Available restore points
            </p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Snapshots</CardTitle>
            <CardDescription>All backup snapshots in the repository</CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          {snapshots.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <Camera className="h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="font-medium">No snapshots yet</h3>
              <p className="text-sm text-muted-foreground mt-1 mb-4">
                Run a backup to create your first snapshot
              </p>
              <Button onClick={runBackup} disabled={isRunning}>
                {isRunning ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <Play className="h-4 w-4 mr-2" />
                )}
                Run First Backup
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>Time</TableHead>
                  <TableHead>Hostname</TableHead>
                  <TableHead>Paths</TableHead>
                  <TableHead>Tags</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {snapshots.map((snapshot) => (
                  <TableRow key={snapshot.id}>
                    <TableCell>
                      <Badge variant="outline" className="font-mono text-xs">
                        {snapshot.id.slice(0, 8)}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <div>
                        <p className="text-sm">{formatTime(snapshot.time)}</p>
                        <p className="text-xs text-muted-foreground">
                          {new Date(snapshot.time).toLocaleString()}
                        </p>
                      </div>
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {snapshot.hostname}
                    </TableCell>
                    <TableCell className="max-w-xs">
                      <div className="truncate text-sm text-muted-foreground">
                        {snapshot.paths.join(", ")}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div className="flex gap-1 flex-wrap">
                        {snapshot.tags?.map((tag) => (
                          <Badge key={tag} variant="secondary" className="text-xs">
                            {tag}
                          </Badge>
                        ))}
                        {(!snapshot.tags || snapshot.tags.length === 0) && (
                          <span className="text-muted-foreground text-sm">-</span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => navigate(`/jobs/${jobId}/snapshots/${snapshot.id}/files`)}
                          title="Browse files"
                        >
                          <FolderOpen className="h-4 w-4" />
                        </Button>
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="icon">
                              <MoreHorizontal className="h-4 w-4" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            {job?.backup_type === "database" ? (
                              <DropdownMenuItem onClick={() => openDbRestoreDialog(snapshot)}>
                                <Database className="h-4 w-4 mr-2" />
                                Restore into database
                              </DropdownMenuItem>
                            ) : (
                              <DropdownMenuItem onClick={() => openRestoreDialog(snapshot)}>
                                <RotateCcw className="h-4 w-4 mr-2" />
                                Restore to server
                              </DropdownMenuItem>
                            )}
                            <DropdownMenuItem
                              onClick={() => handleDownloadZip(snapshot)}
                              disabled={downloadingZip === snapshot.id}
                            >
                              {downloadingZip === snapshot.id ? (
                                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                              ) : (
                                <Archive className="h-4 w-4 mr-2" />
                              )}
                              Download as ZIP
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Alert>
        <Info className="h-4 w-4" />
        <AlertTitle>Restoring Files</AlertTitle>
        <AlertDescription className="mt-2">
          Click the <RotateCcw className="inline h-3 w-3 mx-1" /> Restore button on any snapshot to restore it back to the remote server via SSH.
        </AlertDescription>
      </Alert>

      {/* Restore Dialog */}
      <Dialog open={restoreDialogOpen} onOpenChange={setRestoreDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Restore Snapshot to Server</DialogTitle>
            <DialogDescription>
              Restore the entire snapshot "{restoreSnapshot?.id.slice(0, 8)}" to the original server.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label>Snapshot</Label>
              <Input value={restoreSnapshot?.id || ""} disabled />
            </div>
            <div className="space-y-2">
              <Label>Paths in Snapshot</Label>
              <Input value={restoreSnapshot?.paths.join(", ") || ""} disabled />
            </div>
            <div className="space-y-2">
              <Label htmlFor="target-path">Target Path</Label>
              <Input
                id="target-path"
                value={targetPath}
                onChange={(e) => setTargetPath(e.target.value)}
                placeholder="/path/to/restore"
              />
              <p className="text-xs text-muted-foreground">
                The base path on the server where files will be restored. The original directory structure will be preserved.
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRestoreDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleRestore} disabled={isRestoring || !targetPath}>
              {isRestoring ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Restoring...
                </>
              ) : (
                <>
                  <RotateCcw className="h-4 w-4 mr-2" />
                  Restore
                </>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Database Restore Dialog */}
      <Dialog open={dbRestoreDialogOpen} onOpenChange={setDbRestoreDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Database className="h-5 w-5" />
              Restore Database Snapshot
            </DialogTitle>
            <DialogDescription>
              Import the database dump from snapshot "{dbRestoreSnapshot?.id.slice(0, 8)}" back into the configured database.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertTitle>Destructive operation</AlertTitle>
              <AlertDescription className="text-xs">
                This will overwrite data in the target database. Tables with the same name in the dump will be replaced. Make sure you have a current backup before proceeding.
              </AlertDescription>
            </Alert>
            <div className="space-y-2">
              <Label>Snapshot</Label>
              <Input value={dbRestoreSnapshot?.id || ""} disabled />
            </div>
            <div className="space-y-2">
              <Label htmlFor="target-database">Target Database Name <span className="text-muted-foreground text-xs">(optional)</span></Label>
              <Input
                id="target-database"
                value={targetDatabase}
                onChange={(e) => setTargetDatabase(e.target.value)}
                placeholder="Leave blank to use the database(s) in the dump"
              />
              <p className="text-xs text-muted-foreground">
                If set, the dump will be imported into this specific database. Leave blank for dumps that contain their own CREATE DATABASE statements or for pg_dumpall / --all-databases dumps.
              </p>
            </div>
            <div className="space-y-2">
              <Label htmlFor="confirm-text">Type <code className="text-xs">RESTORE</code> to confirm</Label>
              <Input
                id="confirm-text"
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                placeholder="RESTORE"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDbRestoreDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleDbRestore}
              disabled={isDbRestoring || confirmText !== "RESTORE"}
            >
              {isDbRestoring ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Restoring database...
                </>
              ) : (
                <>
                  <Database className="h-4 w-4 mr-2" />
                  Restore Database
                </>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
