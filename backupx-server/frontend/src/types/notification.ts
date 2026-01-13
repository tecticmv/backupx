export interface EmailConfig {
  smtp_host: string;
  smtp_port: number;
  smtp_user: string;
  smtp_password: string;
  smtp_tls: boolean;
  from_address: string;
  to_addresses: string;
}

export interface SlackConfig {
  webhook_url: string;
}

export interface DiscordConfig {
  webhook_url: string;
}

export interface TelegramConfig {
  bot_token: string;
  chat_id: string;
}

export interface WebhookConfig {
  url: string;
  method: 'GET' | 'POST' | 'PUT';
  headers: Record<string, string>;
}

export type NotificationChannelType = 'email' | 'slack' | 'discord' | 'telegram' | 'webhook';

export type NotificationConfig = EmailConfig | SlackConfig | DiscordConfig | TelegramConfig | WebhookConfig;

export interface NotificationChannel {
  id: string;
  name: string;
  type: NotificationChannelType;
  enabled: boolean;
  config: NotificationConfig;
  notify_on_success: boolean;
  notify_on_failure: boolean;
  created_at: string;
  updated_at?: string;
}
