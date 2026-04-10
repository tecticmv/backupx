export type SshAuthType = 'key_path' | 'key_content' | 'password';

export interface Server {
  id: string;
  name: string;
  host: string;
  ssh_port?: number;
  ssh_user?: string;
  ssh_key?: string;
  ssh_auth_type?: SshAuthType;
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
  ssh_auth_type: SshAuthType;
  ssh_password: string;
  ssh_key_content: string;
  status: 'active' | 'inactive';
}
