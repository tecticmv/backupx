export type ConnectionType = 'ssh' | 'agent';

export interface Server {
  id: string;
  name: string;
  host: string;
  connection_type: ConnectionType;
  // SSH fields
  ssh_port?: number;
  ssh_user?: string;
  ssh_key?: string;
  // Agent fields
  agent_port?: number;
  agent_api_key?: string;
  created_at: string;
  updated_at: string;
}

export interface ServerFormData {
  name: string;
  host: string;
  connection_type: ConnectionType;
  // SSH fields
  ssh_port: number;
  ssh_user: string;
  ssh_key: string;
  // Agent fields
  agent_port: number;
  agent_api_key: string;
}
