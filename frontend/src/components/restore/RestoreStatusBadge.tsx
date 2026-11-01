import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { isRestoreActive, RESTORE_STATUS_META } from "@/lib/restore";
import type { RestoreStatus } from "@/types/restore";

export default function RestoreStatusBadge({ status }: { status: RestoreStatus }) {
  const meta = RESTORE_STATUS_META[status] ?? { label: status, variant: "outline" as const };
  return (
    <Badge variant={meta.variant} className={meta.className}>
      {isRestoreActive(status) && <Loader2 className="animate-spin" />}
      {meta.label}
    </Badge>
  );
}
