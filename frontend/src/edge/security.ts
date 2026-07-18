import { createRemoteJWKSet, jwtVerify, type JWTVerifyGetKey, type JWTPayload } from "jose";

export interface EdgeEnv {
  ACCESS_TEAM_DOMAIN: string;
  ACCESS_AUD: string;
  OWNER_EMAIL: string;
  ACCESS_ALLOWED_IDP_IDS: string;
  PUBLIC_ORIGIN: string;
  SHIORI_ORIGIN: string;
  CF_ACCESS_CLIENT_ID: string;
  CF_ACCESS_CLIENT_SECRET: string;
  CSRF_SHARED_SECRET: string;
  SHIORI_ENVIRONMENT: string;
}

export interface AccessPrincipal {
  token: string;
  subject: string;
  email: string;
  identityProviderId: string;
  claims: JWTPayload;
}

export class EdgeSecurityError extends Error {
  constructor(public readonly status: number, public readonly code: string, message: string) {
    super(message);
    this.name = "EdgeSecurityError";
  }
}

export function normalizedHttpsOrigin(value: string, field: string): string {
  let parsed: URL;
  try { parsed = new URL(value); } catch { throw new EdgeSecurityError(503, "EDGE_MISCONFIGURED", `${field} is invalid`); }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.search || parsed.hash || parsed.pathname !== "/") {
    throw new EdgeSecurityError(503, "EDGE_MISCONFIGURED", `${field} must be an HTTPS origin`);
  }
  return parsed.origin;
}

export function validateEdgeEnv(env: Partial<EdgeEnv>): EdgeEnv {
  const required: Array<keyof EdgeEnv> = [
    "ACCESS_TEAM_DOMAIN", "ACCESS_AUD", "OWNER_EMAIL", "ACCESS_ALLOWED_IDP_IDS", "PUBLIC_ORIGIN",
    "SHIORI_ORIGIN", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET", "CSRF_SHARED_SECRET", "SHIORI_ENVIRONMENT",
  ];
  for (const key of required) if (!env[key]?.trim()) throw new EdgeSecurityError(503, "EDGE_MISCONFIGURED", `Missing ${key}`);
  if (env.SHIORI_ENVIRONMENT !== "production") {
    throw new EdgeSecurityError(503, "PREVIEW_ORIGIN_DISABLED", "The private origin is disabled outside production");
  }
  normalizedHttpsOrigin(env.ACCESS_TEAM_DOMAIN!, "ACCESS_TEAM_DOMAIN");
  normalizedHttpsOrigin(env.PUBLIC_ORIGIN!, "PUBLIC_ORIGIN");
  normalizedHttpsOrigin(env.SHIORI_ORIGIN!, "SHIORI_ORIGIN");
  if (!env.OWNER_EMAIL!.includes("@")) throw new EdgeSecurityError(503, "EDGE_MISCONFIGURED", "OWNER_EMAIL is invalid");
  if (new TextEncoder().encode(env.CSRF_SHARED_SECRET!).byteLength < 32) throw new EdgeSecurityError(503, "EDGE_MISCONFIGURED", "CSRF_SHARED_SECRET is too short");
  return env as EdgeEnv;
}

export async function verifyAccessJwt(
  token: string,
  env: Pick<EdgeEnv, "ACCESS_TEAM_DOMAIN" | "ACCESS_AUD" | "OWNER_EMAIL">,
  keySet?: JWTVerifyGetKey,
): Promise<JWTPayload> {
  if (!token) throw new EdgeSecurityError(401, "AUTH_REQUIRED", "Cloudflare Access assertion is required");
  const issuer = normalizedHttpsOrigin(env.ACCESS_TEAM_DOMAIN, "ACCESS_TEAM_DOMAIN");
  const jwks = keySet ?? createRemoteJWKSet(new URL(`${issuer}/cdn-cgi/access/certs`));
  let payload: JWTPayload;
  try {
    ({ payload } = await jwtVerify(token, jwks, {
      issuer,
      audience: env.ACCESS_AUD,
      algorithms: ["RS256"],
    }));
  } catch {
    throw new EdgeSecurityError(401, "AUTH_INVALID", "Cloudflare Access assertion is invalid");
  }
  if (typeof payload.exp !== "number" || typeof payload.nbf !== "number" || typeof payload.sub !== "string") {
    throw new EdgeSecurityError(401, "AUTH_INVALID", "Required Access claims are missing");
  }
  if (payload.type !== "app") throw new EdgeSecurityError(403, "OWNER_ONLY", "A user identity token is required");
  const email = typeof payload.email === "string" ? payload.email.trim().toLowerCase() : "";
  if (!email || email !== env.OWNER_EMAIL.trim().toLowerCase()) {
    throw new EdgeSecurityError(403, "OWNER_ONLY", "This account is not allowed");
  }
  return payload;
}

interface IdentityResponse {
  idp?: { id?: string; type?: string; name?: string };
  identity_provider?: { id?: string };
}

export async function resolveIdentityProvider(
  token: string,
  teamDomain: string,
  fetchImpl: typeof fetch = fetch,
): Promise<string> {
  const issuer = normalizedHttpsOrigin(teamDomain, "ACCESS_TEAM_DOMAIN");
  let response: Response;
  try {
    response = await fetchImpl(`${issuer}/cdn-cgi/access/get-identity`, {
      method: "GET",
      headers: { Cookie: `CF_Authorization=${token}`, Accept: "application/json" },
      redirect: "error",
    });
  } catch {
    throw new EdgeSecurityError(403, "IDENTITY_UNVERIFIED", "The login method could not be verified");
  }
  if (!response.ok) throw new EdgeSecurityError(403, "IDENTITY_UNVERIFIED", "The login method could not be verified");
  const identity = (await response.json()) as IdentityResponse;
  const id = identity.idp?.id ?? identity.identity_provider?.id;
  if (!id) throw new EdgeSecurityError(403, "IDENTITY_UNVERIFIED", "The identity provider ID is missing");
  return id;
}

export async function authenticateRequest(
  request: Request,
  env: EdgeEnv,
  options: { keySet?: JWTVerifyGetKey; fetchImpl?: typeof fetch } = {},
): Promise<AccessPrincipal> {
  const token = request.headers.get("Cf-Access-Jwt-Assertion") ?? "";
  const claims = await verifyAccessJwt(token, env, options.keySet);
  const identityProviderId = await resolveIdentityProvider(token, env.ACCESS_TEAM_DOMAIN, options.fetchImpl);
  const allowed = env.ACCESS_ALLOWED_IDP_IDS.split(",").map((value) => value.trim()).filter(Boolean);
  if (!allowed.includes(identityProviderId)) throw new EdgeSecurityError(403, "OWNER_ONLY", "This login method is not allowed");
  return {
    token,
    subject: claims.sub!,
    email: String(claims.email).trim().toLowerCase(),
    identityProviderId,
    claims,
  };
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

function decodeBase64Url(value: string): Uint8Array {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4);
  const decoded = atob(padded);
  return Uint8Array.from(decoded, (char) => char.charCodeAt(0));
}

function encodeUtf8(value: string): Uint8Array { return new TextEncoder().encode(value); }

export async function enforceMutationGuards(request: Request, env: EdgeEnv, principal: AccessPrincipal): Promise<void> {
  if (SAFE_METHODS.has(request.method.toUpperCase())) return;
  const origin = request.headers.get("Origin") ?? "";
  if (origin !== normalizedHttpsOrigin(env.PUBLIC_ORIGIN, "PUBLIC_ORIGIN")) {
    throw new EdgeSecurityError(403, "ORIGIN_REJECTED", "The request origin is not allowed");
  }
  const token = request.headers.get("X-CSRF-Token") ?? "";
  const [encoded, signature, extra] = token.split(".");
  if (!encoded || !signature || extra) throw new EdgeSecurityError(403, "CSRF_INVALID", "The CSRF token is invalid or expired");
  try {
    const key = await crypto.subtle.importKey("raw", encodeUtf8(env.CSRF_SHARED_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
    const valid = await crypto.subtle.verify("HMAC", key, decodeBase64Url(signature), encodeUtf8(encoded));
    if (!valid) throw new Error("signature mismatch");
    const payload = JSON.parse(new TextDecoder().decode(decodeBase64Url(encoded))) as { sub?: unknown; email?: unknown; exp?: unknown };
    const now = Math.floor(Date.now() / 1000);
    if (payload.sub !== principal.subject || payload.email !== principal.email || typeof payload.exp !== "number" || payload.exp < now || payload.exp > now + 3600) {
      throw new Error("token claims mismatch");
    }
  } catch {
    throw new EdgeSecurityError(403, "CSRF_INVALID", "The CSRF token is invalid or expired");
  }
}

const FORWARDED_REQUEST_HEADERS = new Set([
  "accept", "accept-language", "cache-control", "content-range", "content-type", "if-match", "if-none-match",
  "idempotency-key", "last-event-id", "range", "x-chunk-sha256", "x-csrf-token",
]);

export function buildOriginHeaders(request: Request, env: EdgeEnv, principal: AccessPrincipal): Headers {
  const headers = new Headers();
  request.headers.forEach((value, name) => {
    if (FORWARDED_REQUEST_HEADERS.has(name.toLowerCase())) headers.set(name, value);
  });
  headers.set("Origin", env.PUBLIC_ORIGIN);
  headers.set("CF-Access-Client-Id", env.CF_ACCESS_CLIENT_ID);
  headers.set("CF-Access-Client-Secret", env.CF_ACCESS_CLIENT_SECRET);
  headers.set("Shiori-User-Assertion", principal.token);
  headers.set("Shiori-Verified-IdP", principal.identityProviderId);
  return headers;
}

export function buildOriginRequestInit(request: Request, headers: Headers): RequestInit {
  const method = request.method.toUpperCase();
  return {
    method,
    headers,
    body: method === "GET" || method === "HEAD" ? null : request.body,
    redirect: "manual",
    signal: request.signal,
  };
}

const STRIPPED_RESPONSE_HEADERS = new Set([
  "access-control-allow-credentials", "access-control-allow-headers", "access-control-allow-methods", "access-control-allow-origin",
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "server", "set-cookie", "te", "trailer", "transfer-encoding", "upgrade",
]);

export function buildBrowserHeaders(upstream: Response): Headers {
  const headers = new Headers();
  upstream.headers.forEach((value, name) => {
    if (!STRIPPED_RESPONSE_HEADERS.has(name.toLowerCase()) && !name.toLowerCase().startsWith("cf-access-")) headers.set(name, value);
  });
  headers.set("Cache-Control", "no-store");
  headers.set("Pragma", "no-cache");
  headers.set("X-Content-Type-Options", "nosniff");
  return headers;
}

export function problem(error: unknown): Response {
  const known = error instanceof EdgeSecurityError ? error : new EdgeSecurityError(502, "ORIGIN_UNAVAILABLE", "The private service is unavailable");
  return Response.json({ code: known.code, detail: known.message }, {
    status: known.status,
    headers: { "Cache-Control": "no-store", "Content-Type": "application/problem+json", "X-Content-Type-Options": "nosniff" },
  });
}
