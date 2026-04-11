export type DatabaseType = "mysql" | "postgres";

export interface DatabaseConfig {
  id: string;
  name: string;
  type: DatabaseType;
  host: string;
  port: number;
  username: string;
  password: string;
  databases: string; // comma-separated list or "*" for all
  docker_container?: string; // If set, use `docker exec` instead of host/port
  status: "active" | "inactive";
  created_at?: string;
  updated_at?: string;
}
