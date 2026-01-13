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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { S3Config } from "@/types/s3";
import {
  FolderSearch,
  Loader2,
  HardDrive,
  CloudCog,
} from "lucide-react";

export default function S3ExplorerList() {
  const [configs, setConfigs] = useState<S3Config[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    fetchConfigs();
  }, []);

  const fetchConfigs = async () => {
    setIsLoading(true);
    try {
      const response = await fetch("/api/s3-configs");
      if (response.ok) {
        setConfigs(await response.json());
      }
    } catch (err) {
      console.error("Failed to fetch S3 configs:", err);
    } finally {
      setIsLoading(false);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Stats Cards */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">
              Available Buckets
            </CardTitle>
            <HardDrive className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{configs.length}</div>
            <p className="text-xs text-muted-foreground">
              S3 buckets to explore
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium">
              Unique Endpoints
            </CardTitle>
            <CloudCog className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {new Set(configs.map((c) => c.endpoint)).size}
            </div>
            <p className="text-xs text-muted-foreground">Different providers</p>
          </CardContent>
        </Card>
      </div>

      {/* S3 Configs Table */}
      <Card>
        <CardHeader>
          <CardTitle>S3 Storage Explorer</CardTitle>
          <CardDescription>
            Browse files and folders in your S3 buckets
          </CardDescription>
        </CardHeader>
        <CardContent>
          {configs.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <FolderSearch className="h-12 w-12 text-muted-foreground/50 mb-4" />
              <h3 className="font-medium">No S3 configurations</h3>
              <p className="text-sm text-muted-foreground mt-1 mb-4">
                Add an S3 configuration first to browse bucket contents
              </p>
              <Link to="/storage">
                <Button>Go to S3 Storage</Button>
              </Link>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Endpoint</TableHead>
                  <TableHead>Bucket</TableHead>
                  <TableHead>Region</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {configs.map((config) => (
                  <TableRow key={config.id}>
                    <TableCell className="font-medium">{config.name}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {config.endpoint}
                    </TableCell>
                    <TableCell>{config.bucket}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {config.region || "-"}
                    </TableCell>
                    <TableCell className="text-right">
                      <Link to={`/storage/${config.id}/browse`}>
                        <Button variant="outline" size="sm">
                          <FolderSearch className="h-4 w-4 mr-2" />
                          Browse
                        </Button>
                      </Link>
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
