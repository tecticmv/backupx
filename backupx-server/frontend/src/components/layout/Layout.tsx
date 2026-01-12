import { useLocation } from "react-router-dom";
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

interface LayoutProps {
  children: React.ReactNode;
}

const routeNames: Record<string, string> = {
  "/": "Dashboard",
  "/jobs": "Backup Jobs",
  "/history": "History",
  "/servers": "Servers",
  "/databases": "Databases",
  "/storage": "S3 Storage",
};

export default function Layout({ children }: LayoutProps) {
  const location = useLocation();

  const getBreadcrumbs = () => {
    const path = location.pathname;

    // Handle job snapshots routes
    if (path.match(/\/jobs\/[^/]+\/snapshots/)) {
      return [
        { path: "/jobs", label: "Backup Jobs" },
        { path: path, label: "Snapshots", isCurrentPage: true },
      ];
    }

    const label = routeNames[path] || "Page";
    return [{ path, label, isCurrentPage: true }];
  };

  const breadcrumbs = getBreadcrumbs();

  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset>
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
                    <BreadcrumbLink href={crumb.path}>
                      {crumb.label}
                    </BreadcrumbLink>
                  )}
                </BreadcrumbItem>
              ))}
            </BreadcrumbList>
          </Breadcrumb>
        </header>
        <main className="flex-1 p-6">
          {children}
        </main>
      </SidebarInset>
    </SidebarProvider>
  );
}
