export interface Server {
  id: string;
  name: string;
  host: string;
  ssh_port?: number;
  ssh_user?: string;
  ssh_key?: string;
  status: 'active' | 'inactive';
  created_at: string;
  updated_at: string;
}

export interface ServerFormData {
  name: string;
  host: string;
  ssh_port: number;
  ssh_user: string;
  ssh_key: string;
  status: 'active' | 'inactive';
}
