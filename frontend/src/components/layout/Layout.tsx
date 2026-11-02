import { Link, useLocation } from "react-router-dom";
import { SidebarProvider, SidebarInset, SidebarTrigger } from "@/components/ui/sidebar";
import { Separator } from "@/components/ui/separator";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { AppSidebar } from "./AppSidebar";
import { TaskTray } from "@/components/TaskTray";

interface LayoutProps {
  children: React.ReactNode;
}

// Same labels as the sidebar
const routeNames: Record<string, string> = {
  "/": "Dashboard",
  "/jobs": "Backup Jobs",
  "/restore": "Restore",
  "/history": "Run History",
  "/servers": "Servers",
  "/databases": "Databases",
  "/storage": "Storage",
  "/notifications": "Notifications",
  "/audit": "Audit Log",
  "/settings": "Settings",
};

interface Crumb {
  path: string;
  label: string;
  isCurrentPage?: boolean;
}

/** Breadcrumbs for a path; every route in App.tsx gets a real title (never a generic "Page"). */
function breadcrumbsFor(pathname: string): Crumb[] {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  const jobs = { path: "/jobs", label: routeNames["/jobs"] };
  const job = path.match(/^\/jobs\/([^/]+)/);
  if (job) {
    const jobPath = `/jobs/${job[1]}`;
    const rest = path.slice(jobPath.length);
    if (!rest) return [jobs, { path, label: "Job", isCurrentPage: true }];
    const jobCrumb = { path: jobPath, label: "Job" };
    if (rest === "/snapshots") return [jobs, jobCrumb, { path, label: "Snapshots", isCurrentPage: true }];
    if (/^\/snapshots\/[^/]+\/files$/.test(rest)) {
      return [jobs, jobCrumb, { path: `${jobPath}/snapshots`, label: "Snapshots" }, { path, label: "Files", isCurrentPage: true }];
    }
    if (rest === "/pitr-setup") return [jobs, jobCrumb, { path, label: "PITR setup", isCurrentPage: true }];
  }
  if (/^\/storage\/[^/]+\/browse$/.test(path)) {
    return [{ path: "/storage", label: routeNames["/storage"] }, { path, label: "Browse", isCurrentPage: true }];
  }
  if (routeNames[path]) return [{ path, label: routeNames[path], isCurrentPage: true }];
  // Unknown route: its top-level section, else the last path segment
  const top = `/${path.split("/")[1] ?? ""}`;
  const label = routeNames[top] ?? (path.split("/").filter(Boolean).pop() ?? "Dashboard");
  return [{ path, label, isCurrentPage: true }];
}

export default function Layout({ children }: LayoutProps) {
  const location = useLocation();
  const breadcrumbs = breadcrumbsFor(location.pathname);

  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset className="min-w-0">
        <header className="flex h-16 shrink-0 items-center gap-2 border-b px-4">
          <SidebarTrigger className="-ml-1" />
          <Separator orientation="vertical" className="mr-2 h-4" />
          <Breadcrumb>
            <BreadcrumbList>
              {breadcrumbs.map((crumb, index) => (
                <BreadcrumbItem key={crumb.path}>
                  {index > 0 && <BreadcrumbSeparator />}
                  {crumb.isCurrentPage ? (
                    <BreadcrumbPage>{crumb.label}</BreadcrumbPage>
                  ) : (
                    <BreadcrumbLink asChild>
                      <Link to={crumb.path}>{crumb.label}</Link>
                    </BreadcrumbLink>
                  )}
                </BreadcrumbItem>
              ))}
            </BreadcrumbList>
          </Breadcrumb>
          <div className="ml-auto">
            <TaskTray />
          </div>
        </header>
        <div className="min-w-0 flex-1 p-6">
          {children}
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
