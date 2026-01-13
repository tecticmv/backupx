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
import { toast } from "sonner";
import type { S3Object, S3BrowseResponse } from "@/types/s3";
import {
  ArrowLeft,
  Folder,
  File,
  Loader2,
  RefreshCw,
  HardDrive,
  FolderOpen,
  FileText,
} from "lucide-react";

export default function S3Explorer() {
  const { configId } = useParams<{ configId: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const [objects, setObjects] = useState<S3Object[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [bucket, setBucket] = useState("");
  const [configName, setConfigName] = useState("");

  const currentPath = searchParams.get("path") || "";

  useEffect(() => {
    fetchObjects();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configId, currentPath]);

  const fetchObjects = async () => {
    setIsLoading(true);
    try {
      const url = currentPath
        ? `/api/s3-configs/${configId}/browse?path=${encodeURIComponent(currentPath)}`
        : `/api/s3-configs/${configId}/browse`;

      const response = await fetch(url);
      if (response.ok) {
        const data: S3BrowseResponse = await response.json();
        setObjects(data.objects);
        setBucket(data.bucket);
        setConfigName(data.config_name);
      } else {
        const error = await response.json();
        toast.error(error.error || "Failed to load bucket contents");
      }
    } catch {
      toast.error("Failed to load bucket contents");
    } finally {
      setIsLoading(false);
    }
  };

  const navigateToPath = (path: string) => {
    if (path) {
      setSearchParams({ path });
    } else {
      setSearchParams({});
    }
  };

  const handleRowClick = (item: S3Object) => {
    if (item.is_dir) {
      navigateToPath(item.path);
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

  const pathSegments = currentPath ? currentPath.split("/").filter(Boolean) : [];

  const folderCount = objects.filter((o) => o.is_dir).length;
  const fileCount = objects.filter((o) => !o.is_dir).length;

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
            onClick={() => navigate("/storage")}
          >
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <h1 className="text-2xl font-bold">{configName}</h1>
            <p className="text-sm text-muted-foreground">
              Browsing bucket: {bucket}
            </p>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={fetchObjects}>
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
            <HardDrive className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{objects.length}</div>
            <p className="text-xs text-muted-foreground">
              {currentPath || "Root directory"}
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
                {currentPath ? (
                  <BreadcrumbLink
                    className="cursor-pointer"
                    onClick={() => navigateToPath("")}
                  >
                    {bucket}
                  </BreadcrumbLink>
                ) : (
                  <BreadcrumbPage>{bucket}</BreadcrumbPage>
                )}
              </BreadcrumbItem>
              {pathSegments.map((segment, index) => {
                const segmentPath = pathSegments.slice(0, index + 1).join("/");
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
              {currentPath
                ? `Files and folders in /${currentPath}`
                : "Files and folders in bucket root"}
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          {objects.length === 0 ? (
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
                  <TableHead>Last Modified</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {objects.map((item) => (
                  <TableRow
                    key={item.path}
                    className={item.is_dir ? "cursor-pointer hover:bg-muted/50" : ""}
                    onClick={() => handleRowClick(item)}
                  >
                    <TableCell>
                      <div className="flex items-center gap-2">
                        {item.is_dir ? (
                          <Folder className="h-4 w-4 text-blue-500" />
                        ) : (
                          <File className="h-4 w-4 text-muted-foreground" />
                        )}
                        <span className={item.is_dir ? "font-medium" : ""}>
                          {item.name}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {item.is_dir ? "Folder" : "File"}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {item.is_dir ? "-" : formatBytes(item.size)}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatTime(item.mod_time)}
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
