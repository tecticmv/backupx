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
  status: "active" | "inactive";
  created_at?: string;
  updated_at?: string;
}
