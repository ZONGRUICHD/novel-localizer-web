// @vitest-environment node
import { createLocalJWKSet, exportJWK, generateKeyPair, SignJWT } from "jose";
import { describe, expect, it } from "vitest";
import {
  authenticateRequest,
  buildBrowserHeaders,
  buildOriginHeaders,
  buildOriginRequestInit,
  EdgeSecurityError,
  enforceMutationGuards,
  validateEdgeEnv,
  verifyAccessJwt,
  type AccessPrincipal,
  type EdgeEnv,
} from "../src/edge/security";

const env: EdgeEnv = {
  ACCESS_TEAM_DOMAIN: "https://shiori.cloudflareaccess.com",
  ACCESS_AUD: "pages-audience",
  OWNER_EMAIL: "zongrui0831@outlook.com",
  ACCESS_ALLOWED_IDP_IDS: "github-idp,google-idp",
  PUBLIC_ORIGIN: "https://translate.zongtech.xyz",
  SHIORI_ORIGIN: "https://translate-origin.zongtech.xyz",
  CF_ACCESS_CLIENT_ID: "service-client-id",
  CF_ACCESS_CLIENT_SECRET: "service-client-secret",
  CSRF_SHARED_SECRET: "this-is-a-test-secret-with-at-least-32-bytes",
  SHIORI_ENVIRONMENT: "production",
};

async function signedAccessToken(email = env.OWNER_EMAIL, audience = env.ACCESS_AUD) {
  const { publicKey, privateKey } = await generateKeyPair("RS256");
  const jwk = await exportJWK(publicKey);
  jwk.kid = "test-key";
  const now = Math.floor(Date.now() / 1000);
  const token = await new SignJWT({ email, type: "app" })
    .setProtectedHeader({ alg: "RS256", kid: "test-key" })
    .setIssuer(env.ACCESS_TEAM_DOMAIN)
    .setAudience(audience)
    .setSubject("owner-subject")
    .setNotBefore(now - 1)
    .setIssuedAt(now)
    .setExpirationTime(now + 600)
    .sign(privateKey);
  return { token, keySet: createLocalJWKSet({ keys: [jwk] }) };
}

function principal(token = "verified-user-token"): AccessPrincipal {
  return { token, subject: "owner-subject", email: env.OWNER_EMAIL, identityProviderId: "github-idp", claims: {} };
}

function base64Url(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64url");
}

async function csrfToken(): Promise<string> {
  const payload = base64Url(new TextEncoder().encode(JSON.stringify({ sub: "owner-subject", email: env.OWNER_EMAIL, exp: Math.floor(Date.now() / 1000) + 600, nonce: "test" })));
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(env.CSRF_SHARED_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const signature = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(payload)));
  return `${payload}.${base64Url(signature)}`;
}

describe("Cloudflare Pages edge security", () => {
  it("verifies RS256 signature, issuer, audience, time claims and exact owner", async () => {
    const { token, keySet } = await signedAccessToken();
    const payload = await verifyAccessJwt(token, env, keySet);
    expect(payload.email).toBe(env.OWNER_EMAIL);

    const wrongOwner = await signedAccessToken("somebody@example.com");
    await expect(verifyAccessJwt(wrongOwner.token, env, wrongOwner.keySet)).rejects.toMatchObject({ status: 403, code: "OWNER_ONLY" });
    const wrongAudience = await signedAccessToken(env.OWNER_EMAIL, "other-app");
    await expect(verifyAccessJwt(wrongAudience.token, env, wrongAudience.keySet)).rejects.toMatchObject({ status: 401, code: "AUTH_INVALID" });
  });

  it("checks the login identity provider through the Access identity endpoint", async () => {
    const { token, keySet } = await signedAccessToken();
    const request = new Request("https://translate.zongtech.xyz/api/session", { headers: { "Cf-Access-Jwt-Assertion": token } });
    const fetchImpl: typeof fetch = async () => Response.json({ idp: { id: "github-idp", type: "github" } });
    const result = await authenticateRequest(request, env, { keySet, fetchImpl });
    expect(result.identityProviderId).toBe("github-idp");

    const badFetch: typeof fetch = async () => Response.json({ idp: { id: "otp-idp" } });
    await expect(authenticateRequest(request, env, { keySet, fetchImpl: badFetch })).rejects.toMatchObject({ status: 403, code: "OWNER_ONLY" });
  });

  it("fails closed for preview deployments and missing secrets", () => {
    expect(() => validateEdgeEnv({ ...env, SHIORI_ENVIRONMENT: "preview" })).toThrowError(EdgeSecurityError);
    expect(() => validateEdgeEnv({ ...env, CF_ACCESS_CLIENT_SECRET: "" })).toThrowError(EdgeSecurityError);
  });

  it("requires exact Origin and a short-lived principal-bound CSRF token on writes", async () => {
    const token = await csrfToken();
    const good = new Request("https://translate.zongtech.xyz/api/projects", { method: "POST", headers: { Origin: env.PUBLIC_ORIGIN, "X-CSRF-Token": token } });
    await expect(enforceMutationGuards(good, env, principal())).resolves.toBeUndefined();
    const wrongOrigin = new Request("https://translate.zongtech.xyz/api/projects", { method: "POST", headers: { Origin: "https://attacker.example", "X-CSRF-Token": token } });
    await expect(enforceMutationGuards(wrongOrigin, env, principal())).rejects.toMatchObject({ code: "ORIGIN_REJECTED" });
  });

  it("strips forged internal headers and sets only verified assertions and service credentials", () => {
    const request = new Request("https://translate.zongtech.xyz/api/uploads", {
      method: "PUT",
      headers: {
        "Cf-Access-Jwt-Assertion": "browser-forgery",
        "CF-Access-Client-Id": "browser-forgery",
        "CF-Access-Client-Secret": "browser-forgery",
        "Shiori-User-Assertion": "browser-forgery",
        "Shiori-Verified-IdP": "browser-forgery",
        "X-CSRF-Token": "csrf",
        "Idempotency-Key": "chunk-1",
      },
    });
    const headers = buildOriginHeaders(request, env, principal("verified-user-token"));
    expect(headers.get("Shiori-User-Assertion")).toBe("verified-user-token");
    expect(headers.get("CF-Access-Client-Id")).toBe(env.CF_ACCESS_CLIENT_ID);
    expect(headers.get("CF-Access-Client-Secret")).toBe(env.CF_ACCESS_CLIENT_SECRET);
    expect(headers.get("Shiori-Verified-IdP")).toBe("github-idp");
    expect(headers.has("Cf-Access-Jwt-Assertion")).toBe(false);
    expect(headers.get("Idempotency-Key")).toBe("chunk-1");
  });

  it("passes request bodies through as streams instead of buffering them at the edge", () => {
    const request = new Request("https://translate.zongtech.xyz/api/uploads/id/chunks/0", { method: "PUT", body: "streamed-chunk" });
    const init = buildOriginRequestInit(request, new Headers());
    expect(init.body).toBe(request.body);
    expect(request.bodyUsed).toBe(false);
    expect(init.redirect).toBe("manual");
  });

  it("preserves streaming bodies and removes CORS or cookie response headers", async () => {
    let pullCount = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pullCount += 1;
        controller.enqueue(new TextEncoder().encode(pullCount === 1 ? "first" : "second"));
        if (pullCount === 2) controller.close();
      },
    });
    const upstream = new Response(stream, { headers: { "Content-Type": "text/event-stream", "Access-Control-Allow-Origin": "*", "Set-Cookie": "secret=value" } });
    expect(upstream.bodyUsed).toBe(false);
    const browserResponse = new Response(upstream.body, { status: upstream.status, headers: buildBrowserHeaders(upstream) });
    expect(browserResponse.headers.get("Content-Type")).toBe("text/event-stream");
    expect(browserResponse.headers.has("Access-Control-Allow-Origin")).toBe(false);
    expect(browserResponse.headers.has("Set-Cookie")).toBe(false);
    expect(await browserResponse.text()).toBe("firstsecond");
    expect(pullCount).toBe(2);
  });
});
