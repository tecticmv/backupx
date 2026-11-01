import { Info, Server } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { PitrSetupTarget } from "@/types/pitrSetup";

const INSTALL_LABEL: Record<PitrSetupTarget["install"], string> = {
  host: "Installed on the host",
  docker: "Docker container",
  managed: "Managed database",
  remote: "Remote server (no SSH to the database host)",
};

const CONFIG_LABEL: Record<PitrSetupTarget["config_method"], string> = {
  dropin: "Drop-in config file",
  bind_mount: "Config file bind-mounted into the container",
  alter_system: "ALTER SYSTEM (postgresql.auto.conf)",
  conf_d: "conf.d include file",
  manual: "Manual: backupx can't change the config",
};

const FLAVOR_LABEL: Record<PitrSetupTarget["flavor"], string> = {
  mariadb: "MariaDB",
  mysql: "MySQL",
  postgres: "PostgreSQL",
};

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="contents">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-all">{children}</dd>
    </div>
  );
}

/** Check step: what backupx found on the database host. */
export default function TargetCard({ target }: { target: PitrSetupTarget }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Server className="h-4 w-4" />
          {target.database}
        </CardTitle>
        <CardDescription>
          {FLAVOR_LABEL[target.flavor] ?? target.flavor} on {target.server}
        </CardDescription>
        <CardAction>
          <Badge variant="outline">{INSTALL_LABEL[target.install] ?? target.install}</Badge>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
          {target.version && (
            <Row label="Version">
              <span className="font-mono text-xs">{target.version}</span>
            </Row>
          )}
          {target.service && (
            <Row label="Service">
              <span className="font-mono text-xs">{target.service}</span>
            </Row>
          )}
          {target.container && (
            <Row label="Container">
              <span className="font-mono text-xs">{target.container}</span>
            </Row>
          )}
          <Row label="Config changes">{CONFIG_LABEL[target.config_method] ?? target.config_method}</Row>
          {target.config_path && (
            <Row label="Config file">
              <span className="font-mono text-xs">{target.config_path}</span>
            </Row>
          )}
          <Row label="Restart">
            {target.restart_supported ? "backupx can restart it" : "backupx can't restart it"}
          </Row>
        </dl>
        {(target.managed_provider || target.restart_unsupported_reason) && (
          <Alert>
            <Info className="h-4 w-4" />
            <AlertTitle>
              {target.managed_provider ? `Managed by ${target.managed_provider}` : "Restart not supported"}
            </AlertTitle>
            <AlertDescription>
              {target.restart_unsupported_reason ??
                "The provider manages the server; settings that need a restart have to be changed in its control panel."}
            </AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}
