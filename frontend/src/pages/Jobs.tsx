import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
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
import { toast } from "sonner";
import type { Jobs as JobsType } from "@/types/job";
import {
  Plus,
  Play,
  Camera,
  Pencil,
  Trash2,
  CheckCircle2,
  XCircle,
  Clock,
  AlertCircle,
  Loader2,
  FolderSync,
  CalendarClock,
  TrendingUp,
  TrendingDown,
} from "lucide-react";

export default function Jobs() {
  const [jobs, setJobs] = useState<JobsType>({});
  const [isLoading, setIsLoading] = useState(true);
  const [deleteJobId, setDeleteJobId] = useState<string | null>(null);
  const [runningJobs, setRunningJobs] = useState<Set<string>>(new Set());

  useEffect(() => {
    fetchJobs();
  }, []);

  const fetchJobs = async () => {
    try {
      const response = await fetch("/api/jobs");
      if (response.ok) {
        setJobs(await response.json());
      }
    } catch (error) {
      toast.error("Failed to fetch jobs");
    } finally {
      setIsLoading(false);
    }
  };

  const runBackup = async (jobId: string) => {
    setRunningJobs((prev) => new Set(prev).add(jobId));
    try {
      const response = await fetch(`/api/jobs/${jobId}/run`, {
        method: "POST",
      });
      const data = await response.json();
      if (data.success) {
        toast.success("Backup started");
        fetchJobs();
      } else {
        toast.error(data.error || "Failed to start backup");
      }
    } catch {
      toast.error("Failed to start backup");
    } finally {
      setRunningJobs((prev) => {
        const next = new Set(prev);
        next.delete(jobId);
        return next;
      });
    }
  };

  const deleteJob = async () => {
    if (!deleteJobId) return;

    try {
      const response = await fetch(`/api/jobs/${deleteJobId}`, {
        method: "DELETE",
      });
      if (response.ok) {
        toast.success("Job deleted");
        fetchJobs();
      } else {
        toast.error("Failed to delete job");
      }
    } catch {
      toast.error("Failed to delete job");
    } finally {
      setDeleteJobId(null);
    }
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case "success":
        return <CheckCircle2 className="h-4 w-4 text-emerald-500" />;
      case "failed":
        return <XCircle className="h-4 w-4 text-destructive" />;
      case "running":
        return <Loader2 className="h-4 w-4 text-primary animate-spin" />;
      case "timeout":
        return <Clock className="h-4 w-4 text-amber-500" />;
      default:
        return <AlertCircle className="h-4 w-4 text-muted-foreground" />;
    }
  };

  const getStatusBadge = (status: string) => {
    const config: Record<
      string,
      { variant: "default" | "secondary" | "destructive" | "outline"; label: string; className?: string }
    > = {
      success: { variant: "default", label: "Success", className: "bg-emerald-600 hover:bg-emerald-600" },
      failed: { variant: "destructive", label: "Failed" },
      running: { variant: "secondary", label: "Running" },
      timeout: { variant: "destructive", label: "Timeout" },
      pending: { variant: "outline", label: "Pending" },
    };
    const { variant, label, className } = config[status] || { variant: "outline", label: status };
    return (
      <Badge variant={variant} className={className}>
        {label}
      </Badge>
    );
  };

  const formatTime = (timestamp?: string) => {
    if (!timestamp) return "-";
    return new Date(timestamp).toLocaleString();
  };

  const jobsList = Object.entries(jobs);
  const totalJobs = jobsList.length;
  const successJobs = jobsList.filter(([, j]) => j.status === "success").length;
  const failedJobs = jobsList.filter(([, j]) => j.status === "failed").length;
  const scheduledJobs = jobsList.filter(([, j]) => j.schedule_enabled).length;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Total Jobs</CardTitle>
            <FolderSync className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{totalJobs}</div>
            <p className="text-xs text-muted-foreground">
              Backup jobs configured
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Successful</CardTitle>
            <TrendingUp className="h-4 w-4 text-emerald-500" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-emerald-500">{successJobs}</div>
            <p className="text-xs text-muted-foreground">
              Last run succeeded
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Failed</CardTitle>
            <TrendingDown className="h-4 w-4 text-destructive" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-destructive">{failedJobs}</div>
            <p className="text-xs text-muted-foreground">
              Require attention
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Scheduled</CardTitle>
            <CalendarClock className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-primary">{scheduledJobs}</div>
            <p className="text-xs text-muted-foreground">
              Automated backups
            </p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>All Backup Jobs</CardTitle>
            <CardDescription>
              {totalJobs} backup job{totalJobs !== 1 && "s"} configured
            </CardDescription>
          </div>
          <Link to="/jobs/new">
            <Button>
              <Plus className="h-4 w-4 mr-2" />
              New Job
            </Button>
          </Link>
        </CardHeader>
        <CardContent>
          {jobsList.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <FolderSync className="h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="font-medium">No backup jobs yet</h3>
              <p className="text-sm text-muted-foreground mt-1 mb-4">
                Create your first backup job to get started
              </p>
              <Link to="/jobs/new">
                <Button>
                  <Plus className="h-4 w-4 mr-2" />
                  Create Job
                </Button>
              </Link>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[50px]">Status</TableHead>
                  <TableHead>Name</TableHead>
                  <TableHead>Remote Host</TableHead>
                  <TableHead>Schedule</TableHead>
                  <TableHead>Last Run</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobsList.map(([jobId, job]) => (
                  <TableRow key={jobId}>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        {getStatusIcon(job.status)}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div>
                        <p className="font-medium">{job.name}</p>
                        <p className="text-xs text-muted-foreground">{jobId}</p>
                      </div>
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {job.remote_host}
                    </TableCell>
                    <TableCell>
                      {job.schedule_enabled ? (
                        <Badge variant="outline" className="font-mono text-xs">
                          <CalendarClock className="h-3 w-3 mr-1" />
                          {job.schedule_cron}
                        </Badge>
                      ) : (
                        <span className="text-muted-foreground text-sm">Manual</span>
                      )}
                    </TableCell>
                    <TableCell>
                      <div>
                        {getStatusBadge(job.status)}
                        <p className="text-xs text-muted-foreground mt-1">
                          {formatTime(job.last_run)}
                        </p>
                      </div>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button
                          size="icon"
                          variant="ghost"
                          onClick={() => runBackup(jobId)}
                          disabled={runningJobs.has(jobId) || job.status === "running"}
                          title="Run backup"
                        >
                          {runningJobs.has(jobId) || job.status === "running" ? (
                            <Loader2 className="h-4 w-4 animate-spin" />
                          ) : (
                            <Play className="h-4 w-4" />
                          )}
                        </Button>
                        <Link to={`/jobs/${jobId}/snapshots`}>
                          <Button size="icon" variant="ghost" title="View snapshots">
                            <Camera className="h-4 w-4" />
                          </Button>
                        </Link>
                        <Link to={`/jobs/${jobId}/edit`}>
                          <Button size="icon" variant="ghost" title="Edit job">
                            <Pencil className="h-4 w-4" />
                          </Button>
                        </Link>
                        <Button
                          size="icon"
                          variant="ghost"
                          onClick={() => setDeleteJobId(jobId)}
                          title="Delete job"
                          className="text-destructive hover:text-destructive"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Dialog open={!!deleteJobId} onOpenChange={() => setDeleteJobId(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Backup Job</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this backup job? This action cannot
              be undone. All job configuration will be lost.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteJobId(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={deleteJob}>
              <Trash2 className="h-4 w-4 mr-2" />
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
