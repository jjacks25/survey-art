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

/** Cognito's hosted-UI /logout endpoint predates OIDC RP-initiated logout and only
 * understands its own `client_id`/`logout_uri` query params (see
 * https://docs.aws.amazon.com/cognito/latest/developerguide/logout-endpoint.html) —
 * it doesn't recognize the standard `id_token_hint`/`post_logout_redirect_uri` pair
 * that `react-oidc-context`'s `signoutRedirect()` sends, which lands on Cognito's
 * "Client does not exist" error page instead of signing out. Build the URL by hand
 * instead of going through oidc-client-ts's generic OIDC signout. */
export function cognitoLogoutUrl(cognito: CognitoConfig): string {
  const region = cognito.authority.split(".")[1];
  const postLogoutUri = window.location.origin + "/";
  return (
    `https://${cognito.domain}.auth.${region}.amazoncognito.com/logout` +
    `?client_id=${encodeURIComponent(cognito.clientId)}` +
    `&logout_uri=${encodeURIComponent(postLogoutUri)}`
  );
}
