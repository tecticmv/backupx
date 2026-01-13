import { useState, useEffect } from "react";
import { useParams, useNavigate, useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Badge } from "@/components/ui/badge";
import { toast } from "sonner";
import type { SnapshotFile, SnapshotFilesResponse } from "@/types/job";
import {
  ArrowLeft,
  Folder,
  File,
  Loader2,
  RefreshCw,
  FolderOpen,
  FileText,
  Download,
  Camera,
} from "lucide-react";

export default function SnapshotBrowser() {
  const { jobId, snapshotId } = useParams<{ jobId: string; snapshotId: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const [files, setFiles] = useState<SnapshotFile[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [downloadingFiles, setDownloadingFiles] = useState<Set<string>>(new Set());

  const currentPath = searchParams.get("path") || "/";

  useEffect(() => {
    fetchFiles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, snapshotId, currentPath]);

  const fetchFiles = async () => {
    setIsLoading(true);
    try {
      const url = `/api/jobs/${jobId}/snapshots/${snapshotId}/files?path=${encodeURIComponent(currentPath)}`;
      const response = await fetch(url);
      if (response.ok) {
        const data: SnapshotFilesResponse = await response.json();
        setFiles(data.files);
      } else {
        const error = await response.json();
        toast.error(error.error || "Failed to load files");
      }
    } catch {
      toast.error("Failed to load files");
    } finally {
      setIsLoading(false);
    }
  };

  const navigateToPath = (path: string) => {
    setSearchParams({ path });
  };

  const handleRowClick = (item: SnapshotFile) => {
    if (item.type === "dir") {
      navigateToPath(item.path);
    }
  };

  const handleDownload = async (item: SnapshotFile, e: React.MouseEvent) => {
    e.stopPropagation();

    setDownloadingFiles((prev) => new Set(prev).add(item.path));

    try {
      const url = `/api/jobs/${jobId}/snapshots/${snapshotId}/download?path=${encodeURIComponent(item.path)}`;
      const response = await fetch(url);

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.error || "Download failed");
      }

      const blob = await response.blob();
      const downloadUrl = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = downloadUrl;
      link.download = item.name;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(downloadUrl);

      toast.success(`Downloaded ${item.name}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Download failed");
    } finally {
      setDownloadingFiles((prev) => {
        const next = new Set(prev);
        next.delete(item.path);
        return next;
      });
    }
  };

  const formatBytes = (bytes: number) => {
    if (bytes === 0) return "-";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + " " + sizes[i];
  };

  const formatTime = (timestamp: string) => {
    if (!timestamp) return "-";
    const date = new Date(timestamp);
    return date.toLocaleString();
  };

  const pathSegments = currentPath === "/" ? [] : currentPath.split("/").filter(Boolean);
  const folderCount = files.filter((f) => f.type === "dir").length;
  const fileCount = files.filter((f) => f.type === "file").length;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => navigate(`/jobs/${jobId}/snapshots`)}
          >
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <h1 className="text-2xl font-bold">Snapshot Browser</h1>
            <p className="text-sm text-muted-foreground flex items-center gap-2">
              <Camera className="h-3 w-3" />
              <Badge variant="outline" className="font-mono text-xs">
                {snapshotId?.slice(0, 8)}
              </Badge>
            </p>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={fetchFiles}>
          <RefreshCw className="h-4 w-4 mr-2" />
          Refresh
        </Button>
      </div>

      {/* Stats Cards */}
      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Folders</CardTitle>
            <Folder className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{folderCount}</div>
            <p className="text-xs text-muted-foreground">In current directory</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Files</CardTitle>
            <FileText className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-primary">{fileCount}</div>
            <p className="text-xs text-muted-foreground">In current directory</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">Total Items</CardTitle>
            <FolderOpen className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{files.length}</div>
            <p className="text-xs text-muted-foreground truncate">
              {currentPath}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Breadcrumb Navigation */}
      <Card>
        <CardHeader className="pb-3">
          <Breadcrumb>
            <BreadcrumbList>
              <BreadcrumbItem>
                {currentPath !== "/" ? (
                  <BreadcrumbLink
                    className="cursor-pointer"
                    onClick={() => navigateToPath("/")}
                  >
                    /
                  </BreadcrumbLink>
                ) : (
                  <BreadcrumbPage>/</BreadcrumbPage>
                )}
              </BreadcrumbItem>
              {pathSegments.map((segment, index) => {
                const segmentPath = "/" + pathSegments.slice(0, index + 1).join("/");
                const isLast = index === pathSegments.length - 1;
                return (
                  <BreadcrumbItem key={segmentPath}>
                    <BreadcrumbSeparator />
                    {isLast ? (
                      <BreadcrumbPage>{segment}</BreadcrumbPage>
                    ) : (
                      <BreadcrumbLink
                        className="cursor-pointer"
                        onClick={() => navigateToPath(segmentPath)}
                      >
                        {segment}
                      </BreadcrumbLink>
                    )}
                  </BreadcrumbItem>
                );
              })}
            </BreadcrumbList>
          </Breadcrumb>
        </CardHeader>
      </Card>

      {/* Contents Table */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Contents</CardTitle>
            <CardDescription>
              Files and folders in snapshot at {currentPath}
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          {files.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <FolderOpen className="h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="font-medium">Empty directory</h3>
              <p className="text-sm text-muted-foreground mt-1">
                This location contains no files or folders
              </p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Size</TableHead>
                  <TableHead>Modified</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {files.map((item) => (
                  <TableRow
                    key={item.path}
                    className={item.type === "dir" ? "cursor-pointer hover:bg-muted/50" : ""}
                    onClick={() => handleRowClick(item)}
                  >
                    <TableCell>
                      <div className="flex items-center gap-2">
                        {item.type === "dir" ? (
                          <Folder className="h-4 w-4 text-blue-500" />
                        ) : (
                          <File className="h-4 w-4 text-muted-foreground" />
                        )}
                        <span className={item.type === "dir" ? "font-medium" : ""}>
                          {item.name}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {item.type === "dir" ? "Folder" : "File"}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {item.type === "dir" ? "-" : formatBytes(item.size)}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatTime(item.mtime)}
                    </TableCell>
                    <TableCell className="text-right">
                      {item.type === "file" && (
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={(e) => handleDownload(item, e)}
                          disabled={downloadingFiles.has(item.path)}
                        >
                          {downloadingFiles.has(item.path) ? (
                            <Loader2 className="h-4 w-4 animate-spin" />
                          ) : (
                            <Download className="h-4 w-4" />
                          )}
                        </Button>
                      )}
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
