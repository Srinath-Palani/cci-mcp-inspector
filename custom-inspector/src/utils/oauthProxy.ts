/**
 * Backend-proxied OAuth flow, as a plain function.
 *
 * The browser only talks to the local Inspector backend — the backend discovers the
 * authorization server (RFC 9728/8414), registers a client (RFC 7591) if needed, and
 * exchanges the code for tokens server-side, so authorization servers that block
 * browser CORS work normally.
 *
 * This lives outside the hook because a multi-server run authorizes each endpoint
 * separately: every server has its own OAuth grant, and there is no fixed number of
 * rows, so React's hook rules rule out one useOAuthProxy per row. useOAuthProxy now
 * wraps this same function for the single-server page.
 *
 * Flow: open the authorize URL in a popup → user approves → the AS redirects to the
 * backend callback → poll status until the token appears.
 */

const API_BASE_URL = 'http://localhost:8000'

export interface OAuthProxyToken {
  access_token: string
  refresh_token?: string
  token_type?: string
  scope?: string
  client_id?: string
  token_url?: string
  expires_at?: number
}

const POLL_INTERVAL_MS = 1000
const FLOW_TIMEOUT_MS = 10 * 60_000

/**
 * Run one endpoint's OAuth flow to completion.
 *
 * Rejects with a readable Error on popup blocking, user cancellation, backend
 * failure or timeout — the caller decides how to surface it, since in a batch the
 * failure belongs to one row rather than the whole run.
 *
 * `client` carries a pre-registered client_id/secret for authorization servers
 * that do not support dynamic client registration (Google, GitHub, Slack…).
 * When absent, the backend registers a client itself via RFC 7591.
 */
/**
 * fetch() rejects with a bare TypeError("Failed to fetch") when the request
 * never completes — backend down, wrong port, connection refused. Translate
 * that into a message that names the actual cause so the row does not just
 * say "Failed to fetch" with no indication that the API bridge is not running.
 */
async function fetchJson(url: string, init?: RequestInit): Promise<Record<string, any> | null> {
  let response: Response
  try {
    response = await fetch(url, init)
  } catch {
    throw new Error(
      `Cannot reach the Inspector backend at ${API_BASE_URL}. Start the API bridge (python api_bridge/main.py) and try again.`
    )
  }
  return response.json().catch(() => null)
}

export interface OAuthClientCredentials {
  client_id?: string
  client_secret?: string
}

export async function runOAuthProxyFlow(
  endpointUrl: string,
  client: OAuthClientCredentials = {}
): Promise<OAuthProxyToken> {
  const endpoint = endpointUrl.trim()
  if (!endpoint) {
    throw new Error('An endpoint URL is required before authorizing.')
  }

  const start = await fetchJson(`${API_BASE_URL}/api/oauth/start`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      endpoint_url: endpoint,
      client_id: client.client_id?.trim() || undefined,
      client_secret: client.client_secret?.trim() || undefined,
    }),
  })
  if (!start?.session_id || !start?.authorize_url) {
    throw new Error(start?.error || 'Failed to start OAuth flow')
  }

  const popup = window.open(start.authorize_url, `mcp-oauth-${start.session_id}`, 'width=520,height=680')
  if (!popup) {
    throw new Error('Popup blocked — allow popups for this site and try again.')
  }

  const deadline = Date.now() + FLOW_TIMEOUT_MS
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS))
    let status: Record<string, any> | null = null
    try {
      const statusRes = await fetch(`${API_BASE_URL}/api/oauth/status/${start.session_id}`)
      if (!statusRes.ok) continue
      status = await statusRes.json()
    } catch {
      // Backend went away mid-flow (restarted, crashed). Retrying a dead server
      // for 10 minutes is pointless — fail now with a clear message.
      throw new Error(
        `Lost connection to the Inspector backend at ${API_BASE_URL} during authorization. Restart the API bridge and authorize again.`
      )
    }
    if (!status) continue

    if (status.status === 'complete' && status.token) {
      try { popup.close() } catch { /* already closed */ }
      return status.token
    }
    if (status.status === 'error') {
      throw new Error(status.error_message || 'OAuth flow failed')
    }
    if (popup.closed) {
      // Closing the window can race the redirect, so give the callback a moment
      // before deciding the user gave up.
      await new Promise((r) => setTimeout(r, 2000))
      const final = await fetchJson(`${API_BASE_URL}/api/oauth/status/${start.session_id}`).catch(() => null)
      if (final?.status === 'complete' && final.token) {
        return final.token
      }
      throw new Error('Authorization window was closed before completing.')
    }
  }
  throw new Error('OAuth flow timed out after 10 minutes.')
}
