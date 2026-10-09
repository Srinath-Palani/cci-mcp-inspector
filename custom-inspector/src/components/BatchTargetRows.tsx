/**
 * The editable target list for a multi-server run.
 *
 * Each row is one MCP server with its own credential — its own API key, bearer
 * token or OAuth grant — because that is how remote servers actually work. Nothing
 * is shared between rows, and a row left on "No auth" sends no credential at all.
 *
 * Paste is supported: pasting several lines into an empty URL field fills one row
 * per line, which is how a list of endpoints normally arrives.
 */

import { useCallback } from 'react'
import { discoverAuth, type AuthMode, type BatchTargetAuth, type BatchTargetInput } from '../utils/apiClient.js'
import { runOAuthProxyFlow } from '../utils/oauthProxy.js'

export type TargetKind = 'remote' | 'github'

export interface TargetRow {
  /** Stable key for React; not sent to the backend. */
  id: string
  target: string
  name: string
  authMode: AuthMode
  token: string
  header: string
  /** Tokens from a completed OAuth flow for this row's endpoint. */
  oauthTokens?: BatchTargetAuth['tokens']
  oauthState: 'idle' | 'running' | 'done' | 'error'
  oauthError?: string
  /** Auto-detect (Connect button) progress and outcome for this row. */
  connectState: 'idle' | 'probing' | 'authorizing' | 'done' | 'error'
  connectMessage?: string
  /**
   * Pre-registered OAuth app credentials for servers whose authorization
   * server cannot register clients dynamically (RFC 7591 unsupported).
   */
  oauthClientId: string
  oauthClientSecret: string
}

export function makeRow(target = ''): TargetRow {
  return {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    target,
    name: '',
    authMode: 'none',
    token: '',
    header: 'X-API-Key',
    oauthState: 'idle',
    connectState: 'idle',
    oauthClientId: '',
    oauthClientSecret: '',
  }
}

/** Rows that have a target, in entry order, with each row's own credential attached. */
export function rowsToTargets(rows: TargetRow[], kind: TargetKind): BatchTargetInput[] {
  return rows
    .filter((row) => row.target.trim().length > 0)
    .map((row) => {
      const target: BatchTargetInput = { target: row.target.trim(), kind }
      if (row.name.trim()) target.name = row.name.trim()

      if (row.authMode === 'bearer' && row.token.trim()) {
        target.auth = { mode: 'bearer', token: row.token.trim() }
      } else if (row.authMode === 'api_key' && row.token.trim()) {
        target.auth = { mode: 'api_key', token: row.token.trim(), header: row.header.trim() || 'X-API-Key' }
      } else if (row.authMode === 'oauth' && row.oauthTokens?.access_token) {
        target.auth = { mode: 'oauth', tokens: row.oauthTokens }
      }
      return target
    })
}

/** Rows whose selected auth mode has no usable value yet — surfaced before running. */
export function incompleteAuthRows(rows: TargetRow[]): TargetRow[] {
  return rows.filter((row) => {
    if (!row.target.trim()) return false
    if (row.authMode === 'bearer' || row.authMode === 'api_key') return !row.token.trim()
    if (row.authMode === 'oauth') return !row.oauthTokens?.access_token
    return false
  })
}

/** Rows that have a target but no name — the name is required for each server. */
export function missingNameRows(rows: TargetRow[]): TargetRow[] {
  return rows.filter((row) => row.target.trim().length > 0 && !row.name.trim())
}

const authLabels: Record<AuthMode, string> = {
  none: 'No auth',
  bearer: 'Bearer token',
  api_key: 'API key header',
  oauth: 'OAuth',
}

/** True when an OAuth failure is specifically the missing-pre-registered-client case. */
function isClientRegistrationError(message: string): boolean {
  return message.includes('dynamic client registration') || message.includes('client_id was provided')
}

interface BatchTargetRowsProps {
  kind: TargetKind
  rows: TargetRow[]
  onChange: (rows: TargetRow[]) => void
  disabled?: boolean
}

export function BatchTargetRows({ kind, rows, onChange, disabled }: BatchTargetRowsProps) {
  const isRemote = kind === 'remote'

  const update = useCallback(
    (id: string, patch: Partial<TargetRow>) => {
      onChange(rows.map((row) => (row.id === id ? { ...row, ...patch } : row)))
    },
    [rows, onChange]
  )

  const removeRow = useCallback(
    (id: string) => {
      const next = rows.filter((row) => row.id !== id)
      onChange(next.length > 0 ? next : [makeRow()])
    },
    [rows, onChange]
  )

  const addRow = useCallback(() => onChange([...rows, makeRow()]), [rows, onChange])

  // Pasting a list into an empty field fills a row per line rather than jamming
  // every URL into one field.
  const handlePaste = useCallback(
    (row: TargetRow, event: React.ClipboardEvent<HTMLInputElement>) => {
      const text = event.clipboardData.getData('text')
      const lines = text
        .split(/[\n,]+/)
        .map((line) => line.trim())
        .filter(Boolean)
      if (lines.length < 2) return

      event.preventDefault()
      const newRows = lines.map((line) => makeRow(line))
      const index = rows.findIndex((r) => r.id === row.id)
      const next = [...rows]
      next.splice(index, row.target.trim() ? 0 : 1, ...newRows)
      onChange(next)
    },
    [rows, onChange]
  )

  const authorize = useCallback(
    async (row: TargetRow) => {
      if (!row.target.trim()) {
        update(row.id, { oauthState: 'error', oauthError: 'Enter the endpoint URL first.' })
        return
      }
      update(row.id, { oauthState: 'running', oauthError: undefined })
      try {
        const token = await runOAuthProxyFlow(row.target.trim(), {
          client_id: row.oauthClientId,
          client_secret: row.oauthClientSecret,
        })
        update(row.id, { oauthState: 'done', oauthTokens: token, oauthError: undefined })
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e)
        update(row.id, {
          oauthState: 'error',
          oauthError: message,
          // If the probe previously reported "no auth needed", that verdict is
          // stale the moment the user asks for OAuth and it fails — drop the green
          // message so the row shows one story instead of two contradictory ones.
          ...(row.connectState === 'done'
            ? { connectState: 'idle' as const, connectMessage: undefined }
            : {}),
        })
      }
    },
    [update]
  )

  /**
   * Figure out what auth this server wants and set the row up for it, so the
   * user never has to guess the mode:
   *  - answers without auth  → "No auth" is selected for them;
   *  - OAuth metadata found  → the OAuth popup flow starts automatically and
   *    the captured token lands on this row;
   *  - 401 with no metadata  → the token field opens for a bearer/API key.
   */
  const connect = useCallback(
    async (row: TargetRow) => {
      const endpoint = row.target.trim()
      if (!endpoint) {
        update(row.id, { connectState: 'error', connectMessage: 'Enter the endpoint URL first.' })
        return
      }

      // A credential already in place is left alone — Connect re-checks and
      // confirms rather than discarding a working token.
      update(row.id, { connectState: 'probing', connectMessage: undefined })

      let discovery
      try {
        discovery = await discoverAuth(endpoint)
      } catch (e) {
        update(row.id, {
          connectState: 'error',
          connectMessage: e instanceof Error ? e.message : String(e),
        })
        return
      }

      if (!discovery.success) {
        update(row.id, {
          connectState: 'error',
          connectMessage: discovery.error || 'Could not reach the server to check its auth.',
        })
        return
      }

      if (!discovery.auth_required) {
        update(row.id, {
          authMode: 'none',
          connectState: 'done',
          connectMessage: 'No auth needed — this server answered without credentials.',
          // Clear any stale OAuth error from an earlier Authorize attempt — the
          // probe just proved no credential is needed, so showing both "no OAuth
          // metadata" (red) and "no auth needed" (green) reads as a contradiction.
          oauthState: 'idle',
          oauthError: undefined,
        })
        return
      }

      if (discovery.metadata_available || discovery.auth_config || discovery.auth_type === 'oauth2') {
        // The server publishes OAuth metadata (or challenged with an OAuth
        // WWW-Authenticate header): run the flow right away rather than making
        // the user select "OAuth" and press Authorize themselves.
        update(row.id, {
          authMode: 'oauth',
          connectState: 'authorizing',
          connectMessage: 'OAuth required — complete the sign-in in the popup…',
        })
        try {
          const token = await runOAuthProxyFlow(endpoint, {
            client_id: row.oauthClientId,
            client_secret: row.oauthClientSecret,
          })
          update(row.id, {
            oauthState: 'done',
            oauthTokens: token,
            oauthError: undefined,
            connectState: 'done',
            connectMessage: 'OAuth connected — token held for this server only.',
          })
        } catch (e) {
          const message = e instanceof Error ? e.message : String(e)
          update(row.id, {
            connectState: 'error',
            connectMessage: isClientRegistrationError(message)
              ? 'This server needs a pre-registered OAuth app — paste its Client ID (and secret if it has one) below, then Re-check.'
              : message,
          })
        }
        return
      }

      // Auth is required but the server says nothing about OAuth — the only
      // thing left to ask for is a static credential. Pre-select the detected
      // mode (API key vs bearer) so the header field matches what the server
      // actually expects.
      const staticMode = discovery.auth_type === 'api_key' ? 'api_key' : 'bearer'
      update(row.id, {
        authMode: staticMode,
        connectState: 'done',
        connectMessage:
          staticMode === 'api_key'
            ? 'This server expects an API key (no OAuth flow published) — paste the key below.'
            : 'This server needs a token, but publishes no OAuth flow — paste a bearer token or API key below.',
      })
    },
    [update]
  )

  // min-w-0 on the column and every card: CSS grid/flex items refuse to shrink
  // below their content width by default, so one long unbreakable string (a
  // backend error naming endpoint + RFCs) would otherwise stretch the whole
  // pane and push the queue table's action columns off-screen.
  return (
    <div className="space-y-2 min-w-0">
      {rows.map((row, index) => (
        <div key={row.id} className="border border-gray-200 rounded p-2 bg-white space-y-2 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-xs font-mono text-gray-400 w-6 shrink-0">
              {isRemote ? 'R' : 'G'}
              {index + 1}
            </span>
            <input
              type="text"
              className={`flex-1 min-w-0 p-2 border rounded text-sm placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300 ${
                !row.target.trim() && row.name.trim()
                  ? 'border-red-300 bg-red-50/40'
                  : 'border-gray-200'
              }`}
              placeholder={
                isRemote ? 'https://mcp.example.com/mcp *' : 'https://github.com/owner/repo *'
              }
              title={isRemote ? 'Endpoint URL is required' : 'Repository URL is required'}
              value={row.target}
              onChange={(e) => update(row.id, { target: e.target.value })}
              onPaste={(e) => handlePaste(row, e)}
              disabled={disabled}
              required
            />
            <input
              type="text"
              className={`w-1/4 min-w-0 p-2 border rounded text-sm placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300 ${
                row.target.trim() && !row.name.trim()
                  ? 'border-red-300 bg-red-50/40'
                  : 'border-gray-200'
              }`}
              placeholder="Name *"
              title="Name is required for each server"
              value={row.name}
              onChange={(e) => update(row.id, { name: e.target.value })}
              disabled={disabled}
              required
            />
            <button
              type="button"
              onClick={() => removeRow(row.id)}
              className="text-gray-400 hover:text-red-600 px-1 text-sm"
              title="Remove this server"
              disabled={disabled}
            >
              ✕
            </button>
          </div>

          {/* Per-server credential. Only remote endpoints authenticate over HTTP;
              a GitHub repo is launched locally and takes its secrets as env vars. */}
          {isRemote && (
            <div className="space-y-1.5 pl-8 min-w-0">
              <div className="flex items-center gap-2 flex-wrap min-w-0">
                <button
                  type="button"
                  onClick={() => connect(row)}
                  disabled={
                    disabled ||
                    !row.target.trim() ||
                    row.connectState === 'probing' ||
                    row.connectState === 'authorizing'
                  }
                  className="px-2.5 py-1 text-xs rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
                  title="Probe this server and set up the right authentication automatically"
                >
                  {row.connectState === 'probing'
                    ? 'Checking…'
                    : row.connectState === 'authorizing'
                      ? 'Authorizing…'
                      : row.connectState === 'done'
                        ? 'Re-check'
                        : 'Connect'}
                </button>

                <select
                  className="p-1.5 border border-gray-200 rounded text-xs focus:outline-none focus:ring-1 focus:ring-blue-300"
                  value={row.authMode}
                  onChange={(e) => update(row.id, { authMode: e.target.value as AuthMode })}
                  disabled={disabled}
                  title="Override the detected auth method"
                >
                  {(Object.keys(authLabels) as AuthMode[]).map((mode) => (
                    <option key={mode} value={mode}>
                      {authLabels[mode]}
                    </option>
                  ))}
                </select>

                {row.authMode === 'api_key' && (
                  <input
                    type="text"
                    className="w-32 p-1.5 border border-gray-200 rounded text-xs placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300"
                    placeholder="X-API-Key"
                    value={row.header}
                    onChange={(e) => update(row.id, { header: e.target.value })}
                    disabled={disabled}
                  />
                )}

                {(row.authMode === 'bearer' || row.authMode === 'api_key') && (
                  <input
                    type="password"
                    className="flex-1 p-1.5 border border-gray-200 rounded text-xs placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300"
                    placeholder={row.authMode === 'bearer' ? 'Bearer token for this server' : 'API key for this server'}
                    value={row.token}
                    onChange={(e) => update(row.id, { token: e.target.value })}
                    disabled={disabled}
                    autoComplete="off"
                  />
                )}

                {row.authMode === 'oauth' && (
                  // min-w-0 lets the error span actually truncate inside the wrap;
                  // without it flex children refuse to shrink below content width
                  // and a long backend message pushes the whole card wider.
                  <div className="flex items-center gap-2 flex-1 flex-wrap min-w-0">
                    <button
                      type="button"
                      onClick={() => authorize(row)}
                      disabled={disabled || row.oauthState === 'running'}
                      className="px-2 py-1 text-xs rounded border border-blue-200 text-blue-700 hover:bg-blue-50 disabled:opacity-50 shrink-0"
                    >
                      {row.oauthState === 'running'
                        ? 'Authorizing…'
                        : row.oauthState === 'done'
                          ? 'Re-authorize'
                          : 'Authorize'}
                    </button>
                    <input
                      type="text"
                      className="w-40 p-1.5 border border-gray-200 rounded text-xs placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300"
                      placeholder="Client ID (if required)"
                      title="Pre-registered OAuth app client_id — needed when the server cannot register clients automatically"
                      value={row.oauthClientId}
                      onChange={(e) => update(row.id, { oauthClientId: e.target.value })}
                      disabled={disabled}
                      autoComplete="off"
                    />
                    <input
                      type="password"
                      className="w-36 p-1.5 border border-gray-200 rounded text-xs placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300"
                      placeholder="Client secret (optional)"
                      title="Pre-registered OAuth app client_secret, if the app has one"
                      value={row.oauthClientSecret}
                      onChange={(e) => update(row.id, { oauthClientSecret: e.target.value })}
                      disabled={disabled}
                      autoComplete="off"
                    />
                    {row.oauthState === 'done' && (
                      <span className="text-xs text-green-700">✓ token held for this server only</span>
                    )}
                  </div>
                )}
              </div>

              {/* OAuth failures render on their own wrapping line like the connect
                  message — an inline flex span cannot wrap, and truncate needs
                  min-w-0 on every flex ancestor, so a long backend error would
                  otherwise stretch the card wider than the pane. */}
              {row.oauthState === 'error' && row.oauthError && (
                <p className="text-xs w-full break-words text-red-600" title={row.oauthError}>
                  ✕ {row.oauthError}
                </p>
              )}

              {row.connectMessage && (
                // w-full forces the message onto its own line below the button
                // row, and break-words wraps long backend messages (endpoint URL
                // + RFC references) inside the card instead of stretching the
                // pane wider than the grid column.
                <p
                  className={`text-xs w-full break-words ${
                    row.connectState === 'error'
                      ? 'text-red-600'
                      : row.connectState === 'done'
                        ? 'text-green-700'
                        : 'text-gray-500'
                  }`}
                >
                  {row.connectState === 'done' ? '✓ ' : row.connectState === 'error' ? '✕ ' : ''}
                  {row.connectMessage}
                </p>
              )}
            </div>
          )}
        </div>
      ))}

      <button
        type="button"
        onClick={addRow}
        disabled={disabled}
        className="text-xs text-blue-600 hover:text-blue-800 hover:underline disabled:opacity-50"
      >
        + Add {isRemote ? 'endpoint' : 'repository'}
      </button>
    </div>
  )
}
