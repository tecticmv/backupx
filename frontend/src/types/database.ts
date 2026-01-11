export interface DatabaseConfig {
  id: string;
  name: string;
  type: "mysql";
  host: string;
  port: number;
  username: string;
  password: string;
  databases: string; // comma-separated list or "*" for all
  created_at?: string;
  updated_at?: string;
}
