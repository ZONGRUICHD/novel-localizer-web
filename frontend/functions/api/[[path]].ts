import {
  authenticateRequest,
  buildBrowserHeaders,
  buildOriginHeaders,
  buildOriginRequestInit,
  EdgeSecurityError,
  enforceMutationGuards,
  normalizedHttpsOrigin,
  problem,
  validateEdgeEnv,
  type EdgeEnv,
} from "../../src/edge/security";

export const onRequest: PagesFunction<EdgeEnv> = async (context) => {
  try {
    const env = validateEdgeEnv(context.env);
    const principal = await authenticateRequest(context.request, env);
    await enforceMutationGuards(context.request, env, principal);

    const incomingUrl = new URL(context.request.url);
    const origin = normalizedHttpsOrigin(env.SHIORI_ORIGIN, "SHIORI_ORIGIN");
    const path = Array.isArray(context.params.path) ? context.params.path.join("/") : String(context.params.path ?? "");
    const target = new URL(`/api/${path}`, origin);
    target.search = incomingUrl.search;

    const upstream = await fetch(target, buildOriginRequestInit(
      context.request,
      buildOriginHeaders(context.request, env, principal),
    ));
    if (upstream.status >= 300 && upstream.status < 400) {
      throw new EdgeSecurityError(502, "ORIGIN_REDIRECT_REJECTED", "The private origin attempted a redirect");
    }
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: buildBrowserHeaders(upstream),
    });
  } catch (error) {
    return problem(error);
  }
};
