// Runtime configuration loaded from /config.json so the same build works in any
// environment. Local dev ships public/config.json (authDisabled). In AWS, the
// `make deploy-web` step writes config.json into the S3 bucket from stack outputs.

export interface CognitoConfig {
  authority: string; // https://cognito-idp.<region>.amazonaws.com/<userPoolId>
  clientId: string;
  domain: string; // hosted UI domain prefix
  scope: string;
}

export interface AppConfig {
  apiBase: string; // "" = same origin (/api via CloudFront)
  authDisabled: boolean;
  cognito: CognitoConfig;
}

let cached: AppConfig | null = null;

export async function loadConfig(): Promise<AppConfig> {
  if (cached) return cached;
  const resp = await fetch("/config.json", { cache: "no-store" });
  cached = (await resp.json()) as AppConfig;
  return cached;
}
