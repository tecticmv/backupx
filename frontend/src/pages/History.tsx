import { useState, useEffect } from "react";
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
import { toast } from "sonner";
import type { HistoryEntry } from "@/types/job";
import {
  CheckCircle2,
  XCircle,
  Clock,
  AlertCircle,
  Loader2,
  RefreshCw,
  History as HistoryIcon,
  TrendingUp,
  TrendingDown,
  Timer,
} from "lucide-react";

export default function History() {
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    fetchHistory();
  }, []);

  const fetchHistory = async () => {
    try {
      const response = await fetch("/api/history");
      if (response.ok) {
        const data = await response.json();
        setHistory([...data].reverse());
      }
    } catch {
      toast.error("Failed to fetch history");
    } finally {
      setIsLoading(false);
    }
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case "success":
        return <CheckCircle2 className="h-4 w-4 text-emerald-500" />;
      case "failed":
        return <XCircle className="h-4 w-4 text-destructive" />;
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
      timeout: { variant: "destructive", label: "Timeout" },
    };
    const { variant, label, className } = config[status] || { variant: "outline", label: status };
    return (
      <Badge variant={variant} className={className}>
        {label}
      </Badge>
    );
  };

  const formatDuration = (seconds: number) => {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const mins = Math.floor(seconds / 60);
    const secs = Math.round(seconds % 60);
    if (mins < 60) return `${mins}m ${secs}s`;
    const hours = Math.floor(mins / 60);
    const remainingMins = mins % 60;
    return `${hours}h ${remainingMins}m`;
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

  const successCount = history.filter((e) => e.status === "success").length;
  const failedCount = history.filter((e) => e.status === "failed").length;
  const avgDuration =
    history.length > 0
      ? history.reduce((sum, e) => sum + e.duration, 0) / history.length
      : 0;

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
            <CardTitle className="text-sm font-medium">Total Runs</CardTitle>
            <HistoryIcon className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{history.length}</div>
            <p className="text-xs text-muted-foreground">
              Backup operations logged
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Successful</CardTitle>
            <TrendingUp className="h-4 w-4 text-emerald-500" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-emerald-500">{successCount}</div>
            <p className="text-xs text-muted-foreground">
              {history.length > 0
                ? `${Math.round((successCount / history.length) * 100)}% success rate`
                : "No data"}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Failed</CardTitle>
            <TrendingDown className="h-4 w-4 text-destructive" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-destructive">{failedCount}</div>
            <p className="text-xs text-muted-foreground">
              Require investigation
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Avg Duration</CardTitle>
            <Timer className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-primary">
              {formatDuration(avgDuration)}
            </div>
            <p className="text-xs text-muted-foreground">
              Average backup time
            </p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Backup History</CardTitle>
            <CardDescription>
              Last {history.length} backup operations
            </CardDescription>
          </div>
          <Button variant="outline" size="sm" onClick={fetchHistory}>
            <RefreshCw className="h-4 w-4 mr-2" />
            Refresh
          </Button>
        </CardHeader>
        <CardContent>
          {history.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <HistoryIcon className="h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="font-medium">No backup history</h3>
              <p className="text-sm text-muted-foreground mt-1">
                Run a backup to see activity here
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-12">Status</TableHead>
                  <TableHead>Job</TableHead>
                  <TableHead>Message</TableHead>
                  <TableHead>Duration</TableHead>
                  <TableHead>Time</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {history.map((entry, idx) => (
                  <TableRow key={idx}>
                    <TableCell>
                      {getStatusIcon(entry.status)}
                    </TableCell>
                    <TableCell>
                      <div>
                        <p className="font-medium">{entry.job_name}</p>
                        {getStatusBadge(entry.status)}
                      </div>
                    </TableCell>
                    <TableCell className="max-w-md">
                      <p className="truncate text-muted-foreground text-sm">
                        {entry.message}
                      </p>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className="font-mono">
                        <Timer className="h-3 w-3 mr-1" />
                        {formatDuration(entry.duration)}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground text-sm">
                      {formatTime(entry.timestamp)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
