/**
 * Extracted MCP connection and OAuth logic
 * This module handles OAuth authentication and MCP server connection
 * with support for auto/http/sse transport types
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMcp, type UseMcpResult } from 'use-mcp/react'
import { toProxyUrl } from './mcpProxyUrl.js'

/**
 * Placeholder target used while the user has not asked to connect.
 *
 * `useMcp` has no "disabled" mode — its mount effect calls `connect()`
 * unconditionally on every url change. Passing '' made `strict-url-sanitise`
 * throw `Invalid url to pass to open():`, which use-mcp logged at `error` level
 * and turned into `state: 'failed'` on every mount and every disconnect.
 *
 * A loopback address on port 1 parses cleanly instead of throwing. The vendored
 * use-mcp patch additionally treats it as "do not connect", so no request is
 * made at all; the sentinel keeps the failure harmless even if the patch is not
 * applied.
 */
export const DISABLED_URL = 'http://127.0.0.1:1/'

/**
 * use-mcp fields added by patches/use-mcp@0.0.21.patch.
 *
 * `successfulTransport` reports which transport actually connected. Reading it
 * replaces scraping log text for 'client connected via', which broke silently
 * whenever the library reworded a log line.
 */
type PatchedUseMcpResult = UseMcpResult & {
  successfulTransport?: 'http' | 'sse' | null
}

// Derive the entity types from use-mcp's own result type — avoids a direct
// dependency on @modelcontextprotocol/sdk, which is not installed here.
type Tool = UseMcpResult['tools'][number]
type Resource = UseMcpResult['resources'][number]
type ResourceTemplate = UseMcpResult['resourceTemplates'][number]
type Prompt = UseMcpResult['prompts'][number]

export type TransportType = 'auto' | 'http' | 'sse'

export interface McpConnectionOptions {
  url: string
  transportType?: TransportType
  clientName?: string
  clientUri?: string
  callbackUrl?: string
  debug?: boolean
  autoRetry?: boolean | number
  autoReconnect?: boolean | number
  preventAutoAuth?: boolean
  /**
   * Static bearer token to send as `Authorization: Bearer <token>`.
   *
   * Without this the UI's Bearer Token field was backend-only — the browser
   * client silently ignored it and every token-protected server returned 401.
   */
  bearerToken?: string
  /**
   * Route the connection through the backend's same-origin /mcp-proxy route,
   * which bypasses the CORS block that remote MCP servers otherwise trigger.
   *
   * Note: use-mcp's native browser OAuth cannot work in proxy mode — it derives
   * its storage keys and the RFC 8707 `resource` parameter from the URL it is
   * given, which is the proxy URL. Use a bearer token (see `bearerToken`) or the
   * backend OAuth flow for authenticated servers.
   */
  useProxy?: boolean
  onPopupWindow?: (url: string, features: string, window: Window | null) => void
}

export interface McpConnectionState {
  state: UseMcpResult['state']
  tools: Tool[]
  resources: Resource[]
  resourceTemplates: ResourceTemplate[]
  prompts: Prompt[]
  error: string | undefined
  log: UseMcpResult['log']
  authUrl: string | undefined
  actualTransportType?: 'http' | 'sse' // The actual transport type that succeeded (when transportType was 'auto')
  callTool: (name: string, args?: Record<string, unknown>) => Promise<any>
  listResources: () => Promise<void>
  readResource: (uri: string) => Promise<{ contents: Array<any> }>
  listPrompts: () => Promise<void>
  getPrompt: (name: string, args?: Record<string, string>) => Promise<{ messages: Array<any> }>
  retry: () => void
  disconnect: () => void
  authenticate: () => void
  clearStorage: () => void
}

const DEFAULT_CONNECTION_STATE: McpConnectionState = {
  state: 'discovering',
  tools: [],
  resources: [],
  resourceTemplates: [],
  prompts: [],
  error: undefined,
  log: [],
  authUrl: undefined,
  callTool: (_name: string, _args?: Record<string, unknown>) => Promise.resolve(undefined),
  listResources: () => Promise.resolve(),
  readResource: (_uri: string) => Promise.resolve({ contents: [] }),
  listPrompts: () => Promise.resolve(),
  getPrompt: (_name: string, _args?: Record<string, string>) => Promise.resolve({ messages: [] }),
  retry: () => {},
  disconnect: () => {},
  authenticate: () => {},
  clearStorage: () => {},
}

/**
 * Hook that manages MCP connection with OAuth and transport type selection
 * Extracts the connection logic from the component
 */
export function useMcpConnection(options: McpConnectionOptions) {
  const {
    url,
    transportType = 'auto',
    clientName,
    clientUri,
    callbackUrl,
    debug = false,
    autoReconnect = 3000,
    preventAutoAuth = false,
    bearerToken,
    useProxy = false,
    onPopupWindow,
  } = options

  const [isActive, setIsActive] = useState(false)
  const [connectionState, setConnectionState] = useState<McpConnectionState>(DEFAULT_CONNECTION_STATE)
  const [actualTransportType, setActualTransportType] = useState<'http' | 'sse' | undefined>(undefined)
  const [shouldConnect, setShouldConnect] = useState(false)

  // True only while a real target has been handed to useMcp. Everything derived
  // from mcpResult is gated on this so the DISABLED_URL attempt cannot leak a
  // 'failed' state or an 'Invalid url' error into the UI.
  const isConnecting = shouldConnect && isActive && url.trim().length > 0

  // Fall back to the direct URL if the rewrite fails: a malformed URL should
  // surface as the connection error it is, not as a crash inside a memo.
  const resolvedUrl = useMemo(() => {
    if (!useProxy || !url.trim()) return url
    try {
      return toProxyUrl(url)
    } catch {
      return url
    }
  }, [url, useProxy])

  const usingProxy = useProxy && resolvedUrl !== url
  const targetUrl = isConnecting ? resolvedUrl : DISABLED_URL

  // Identity-stable so it does not retrigger useMcp's option-change effect.
  const customHeaders = useMemo(
    () => (bearerToken?.trim() ? { Authorization: `Bearer ${bearerToken.trim()}` } : undefined),
    [bearerToken],
  )

  // Use the useMcp hook internally
  // Only pass URL when we want to connect, and disable autoReconnect when not active
  const mcpResult = useMcp({
    url: targetUrl,
    clientName,
    clientUri,
    callbackUrl,
    debug,
    autoRetry: false, // Disable auto retry to prevent unwanted reconnections
    autoReconnect: isConnecting ? autoReconnect : false, // Disable auto reconnect when not active or disconnected
    transportType,
    customHeaders,
    // A static bearer token is the credential — never start an OAuth popup on
    // top of it, and never auto-auth while pointed at DISABLED_URL.
    preventAutoAuth: !isConnecting || preventAutoAuth || Boolean(customHeaders),
    onPopupWindow,
  }) as PatchedUseMcpResult

  // Which transport actually connected.
  //
  // Read from use-mcp's own `successfulTransport` (exposed by our patch), which
  // is set at the same moment the client connects. The previous implementation
  // searched log text for 'client connected via' / 'sse fallback' and so was
  // coupled to the library's exact wording; the log fallback below is kept only
  // for the case where the patch is missing.
  useEffect(() => {
    if (!isConnecting) {
      setActualTransportType(undefined)
      return
    }

    if (mcpResult.successfulTransport) {
      setActualTransportType(mcpResult.successfulTransport)
      return
    }

    if (mcpResult.successfulTransport === null) {
      // Patch applied and a connection attempt is in flight but has not
      // succeeded yet — do not guess from logs.
      return
    }

    // Legacy fallback: patch not applied, so infer from log text.
    const logMessages = [...mcpResult.log].reverse().map((entry) => entry.message.toLowerCase())
    const connectionMessage = logMessages.find((msg) => msg.includes('client connected via'))
    if (connectionMessage) {
      setActualTransportType(connectionMessage.includes('http') ? 'http' : 'sse')
      return
    }
    if (logMessages.some((msg) => msg.includes('sse fallback') || msg.includes('attempting sse'))) {
      setActualTransportType('sse')
    }
  }, [isConnecting, mcpResult.successfulTransport, mcpResult.log])

  // Reset once per connect attempt: until useMcp reports something other than
  // 'failed', any 'failed' we see still belongs to the DISABLED_URL attempt that
  // ran before the real URL was handed over.
  const sawFreshStateRef = useRef(false)

  // Update connection state when MCP result changes
  useEffect(() => {
    // Gate on isConnecting, not isActive: isActive alone turns true a render
    // before the real URL reaches useMcp, which briefly surfaced the sentinel's
    // stale 'failed' / 'Invalid url' state in the UI.
    if (isConnecting) {
      if (!sawFreshStateRef.current) {
        if (mcpResult.state === 'failed') return
        sawFreshStateRef.current = true
      }

      // Determine actual transport type: if transportType is 'auto', use detected type, otherwise use the selected type
      const resolvedTransportType: 'http' | 'sse' | undefined =
        transportType === 'auto' ? actualTransportType : transportType === 'http' || transportType === 'sse' ? transportType : undefined

      setConnectionState({
        state: mcpResult.state,
        tools: mcpResult.tools,
        resources: mcpResult.resources,
        resourceTemplates: mcpResult.resourceTemplates,
        prompts: mcpResult.prompts,
        error: mcpResult.error,
        log: mcpResult.log,
        authUrl: mcpResult.authUrl,
        actualTransportType: resolvedTransportType,
        callTool: mcpResult.callTool,
        listResources: mcpResult.listResources,
        readResource: mcpResult.readResource,
        listPrompts: mcpResult.listPrompts,
        getPrompt: mcpResult.getPrompt,
        retry: mcpResult.retry,
        disconnect: mcpResult.disconnect,
        authenticate: mcpResult.authenticate,
        clearStorage: mcpResult.clearStorage,
      })
    } else {
      setConnectionState(DEFAULT_CONNECTION_STATE)
    }
  }, [
    isConnecting,
    transportType,
    actualTransportType,
    mcpResult.state,
    mcpResult.tools,
    mcpResult.resources,
    mcpResult.resourceTemplates,
    mcpResult.prompts,
    mcpResult.error,
    mcpResult.log,
    mcpResult.authUrl,
    mcpResult.callTool,
    mcpResult.listResources,
    mcpResult.readResource,
    mcpResult.listPrompts,
    mcpResult.getPrompt,
    mcpResult.retry,
    mcpResult.disconnect,
    mcpResult.authenticate,
    mcpResult.clearStorage,
  ])

  const connect = useCallback(() => {
    if (!url.trim()) return
    sawFreshStateRef.current = false // Discard any pre-connect 'failed' state
    setIsActive(true)
    setShouldConnect(true) // Enable connection
  }, [url])

  const disconnect = useCallback(() => {
    // First disconnect the underlying connection
    mcpResult.disconnect()
    // Then disable connection flags to prevent reconnection
    sawFreshStateRef.current = false
    setShouldConnect(false)
    setIsActive(false)
    setConnectionState(DEFAULT_CONNECTION_STATE)
    setActualTransportType(undefined) // Reset transport type
  }, [mcpResult])

  const retry = useCallback(() => {
    mcpResult.retry()
  }, [mcpResult])

  const authenticate = useCallback(() => {
    mcpResult.authenticate()
  }, [mcpResult])

  const clearStorage = useCallback(() => {
    mcpResult.clearStorage()
    if (isActive) {
      disconnect()
    }
  }, [mcpResult, isActive, disconnect])

  return {
    ...connectionState,
    isActive,
    usingProxy,
    connect,
    disconnect,
    retry,
    authenticate,
    clearStorage,
  }
}

