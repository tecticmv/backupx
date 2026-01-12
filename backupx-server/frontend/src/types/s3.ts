export interface S3Config {
  id: string;
  name: string;
  endpoint: string;
  bucket: string;
  access_key: string;
  secret_key: string;
  region?: string;
  skip_ssl_verify?: boolean;
  created_at: string;
  updated_at: string;
}

export interface S3ConfigFormData {
  name: string;
  endpoint: string;
  bucket: string;
  access_key: string;
  secret_key: string;
  region?: string;
  skip_ssl_verify?: boolean;
}
