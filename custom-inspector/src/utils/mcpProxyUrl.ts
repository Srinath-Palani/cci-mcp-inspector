/**
 * Helpers for routing a browser MCP connection through the backend's
 * same-origin /mcp-proxy route.
 *
 * Remote MCP servers almost never send CORS headers, so a direct browser
 * connection fails with "Failed to fetch" before any MCP is exchanged. Rewriting
 * the target to a path on this app's own origin removes the cross-origin
 * condition; the FastAPI backend does the actual fetch.
 *
 * The scheme is a path segment, not a query parameter, because the MCP SDK and
 * strict-url-sanitise both rebuild URLs from the pathname — see the module
 * docstring in api_bridge/mcp_proxy.py.
 */

export const PROXY_PREFIX = '/mcp-proxy'

/**
 * Whether /mcp-proxy is reachable from the page.
 *
 * The vite dev server forwards /mcp-proxy to the FastAPI backend (see
 * vite.config.ts). A production build is not served by that backend, so the path
 * would 404 — offer the toggle only in dev.
 */
export function isProxyAvailable(): boolean {
  return import.meta.env.DEV
}

/**
 * Rewrite an MCP endpoint URL to its /mcp-proxy equivalent on this origin.
 *
 * Returns an absolute URL so that use-mcp's OAuth storage keys and the SDK's
 * origin comparisons stay well defined.
 *
 * @throws if the input is not a parseable http(s) URL.
 */
export function toProxyUrl(raw: string, origin: string = window.location.origin): string {
  const parsed = new URL(raw.trim())

  const scheme = parsed.protocol.replace(/:$/, '').toLowerCase()
  if (scheme !== 'http' && scheme !== 'https') {
    throw new Error(`Only http and https URLs can be proxied (got "${parsed.protocol}")`)
  }
  if (parsed.username || parsed.password) {
    throw new Error('Credentials in the URL cannot be proxied')
  }

  // parsed.host keeps a non-default port and drops a default one, which is
  // exactly what the backend expects in the authority segment.
  const path = parsed.pathname || '/'
  return `${origin}${PROXY_PREFIX}/${scheme}/${parsed.host}${path}${parsed.search}`
}

/** True when `url` already points at this app's proxy route. */
export function isProxyUrl(url: string): boolean {
  try {
    return new URL(url, window.location.origin).pathname.startsWith(`${PROXY_PREFIX}/`)
  } catch {
    return false
  }
}
