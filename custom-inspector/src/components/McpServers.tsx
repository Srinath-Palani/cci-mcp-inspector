import { useState, useCallback, useEffect, useRef } from 'react'
import { useMcpConnection, type TransportType } from '../utils/mcpConnection.js'
import {
  runInspection,
  cancelInspection,
  checkApiHealth,
  InspectionError,
  type InspectResponse,
  type JobStatus,
  type ApiErrorDetails,
} from '../utils/apiClient.js'
import { useAuthDetails } from '../hooks/useAuthDetails.js'
import { useServerName } from '../hooks/useServerName.js'
import { useConnectionDetails } from '../hooks/useConnectionDetails.js'
import { useOAuthProxy } from '../hooks/useOAuthProxy.js'
import { StatusBadge } from './StatusBadge.js'
import { ConnectionControls } from './ConnectionControls.js'
import { ConnectionLog } from './ConnectionLog.js'
import { RunInspectionButton } from './RunInspectionButton.js'
import { AuthPrompt } from './AuthPrompt.js'
import { ExportButtons } from './ExportButtons.js'
import { InspectionReport } from './InspectionReport.js'
import { ConnectionTabs, type ConnectionTab } from './ConnectionTabs.js'
import { StdioConnection } from './StdioConnection.js'
import { ErrorDisplay } from './ErrorDisplay.js'
import { InspectionProgress } from './InspectionProgress.js'
import { ConfigImport, type ImportedServer } from './ConfigImport.js'
import { MultiServerRun } from './MultiServerRun.js'
import { ReportPreview } from './ReportPreview.js'

export function McpServers() {
  const [activeTab, setActiveTab] = useState<ConnectionTab>(() => {
    return (sessionStorage.getItem('mcpConnectionTab') as ConnectionTab) || 'remote'
  })
  const [runMode, setRunMode] = useState<'single' | 'multiple'>(() => {
    return sessionStorage.getItem('mcpRunMode') === 'multiple' ? 'multiple' : 'single'
  })
  const [serverUrl, setServerUrl] = useState(() => {
    return sessionStorage.getItem('mcpServerUrl') || ''
  })
  const [transportType, setTransportType] = useState<TransportType>(() => {
    return (sessionStorage.getItem('mcpTransportType') as TransportType) || 'auto'
  })
  const [serverName, setServerName] = useState(() => {
    return sessionStorage.getItem('mcpServerName') || ''
  })
  const [serverDescription, setServerDescription] = useState(() => {
    return sessionStorage.getItem('mcpServerDescription') || ''
  })
  const [githubRepoLink, setGithubRepoLink] = useState(() => {
    return sessionStorage.getItem('mcpGithubRepoLink') || ''
  })
  const [distributionType, setDistributionType] = useState(() => {
    return sessionStorage.getItem('mcpDistributionType') || ''
  })
  const [bearerToken, setBearerToken] = useState(() => {
    return sessionStorage.getItem('mcpBearerToken') || ''
  })
  const [useProxy, setUseProxy] = useState(() => {
    return sessionStorage.getItem('mcpUseProxy') === 'true'
  })
  const [isRunningInspection, setIsRunningInspection] = useState(false)
  const [inspectionResult, setInspectionResult] = useState<InspectResponse | null>(null)
  const [inspectionError, setInspectionError] = useState<string | null>(null)
  const [inspectionErrorDetails, setInspectionErrorDetails] = useState<ApiErrorDetails | null>(null)
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null)
  const [isCancelling, setIsCancelling] = useState(false)
  const activeJobId = useRef<string | null>(null)
  const [apiAvailable, setApiAvailable] = useState<boolean | null>(null)
  // Bumped by "Clear All Data" so child stateful components (the multi-server
  // run) wipe themselves too — they keep their own state that storage clearing
  // alone does not touch.
  const [resetSignal, setResetSignal] = useState(0)
  // The multi-server row selected in the queue; its report previews in the
  // right-hand pane. Lifted here because the queue is on the left and the
  // preview is on the right.
  const [selectedBatchJob, setSelectedBatchJob] = useState<JobStatus | null>(null)
  const [selectedBatchGroupId, setSelectedBatchGroupId] = useState<string | null>(null)

  // Backend-proxied OAuth — works even when the authorization server blocks
  // browser CORS (the token exchange happens inside the FastAPI backend).
  // Declared above useMcpConnection so the issued token can feed the browser
  // Connect as a bearer credential.
  const oauthProxy = useOAuthProxy(serverUrl)

  // A backend-OAuth token is the credential for Connect too: when the proxy
  // flow holds a token, connect with it as the bearer and route through the
  // backend proxy (the server blocked browser CORS — that is why the proxy
  // flow was used). An explicitly typed bearer token still wins.
  const oauthBearerToken = oauthProxy.token?.access_token?.trim()
    ? oauthProxy.token.access_token
    : undefined
  const effectiveBearerToken = bearerToken.trim() ? bearerToken : oauthBearerToken
  const effectiveUseProxy = useProxy || Boolean(oauthBearerToken)

  // Use the extracted connection hook
  const {
    state,
    error: connectionError,
    authUrl,
    isActive,
    actualTransportType,
    usingProxy,
    log: connectionLog,
    connect,
    disconnect,
    clearStorage,
  } = useMcpConnection({
    url: serverUrl,
    transportType,
    // The Bearer Token field previously reached the backend only — the browser
    // client ignored it, so Connect always got 401 on token-protected servers.
    bearerToken: effectiveBearerToken,
    useProxy: effectiveUseProxy,
    // Must be explicit so use-mcp registers this as the redirect_uri with the
    // OAuth server. Without it, use-mcp defaults to window.location.href (the
    // root "/") and the OAuth callback lands on the blank form instead.
    callbackUrl: `${window.location.origin}/oauth/callback`,
    debug: false,
    autoRetry: false,
    autoReconnect: false, // Prevent auto-reconnect from opening a second OAuth popup
  })

  // Auto-disconnect when the user changes the server URL while still connected.
  // Without this, isActive stays true and "Clear stored authentication" appears
  // for servers the user has never connected to.
  // useProxy is watched alongside the URL: flipping it changes the URL handed to
  // use-mcp, and therefore the OAuth serverUrlHash its storage keys are derived
  // from, so the old connection must be torn down rather than reused.
  const prevServerUrlRef = useRef(serverUrl)
  const prevUseProxyRef = useRef(useProxy)
  useEffect(() => {
    const changed = prevServerUrlRef.current !== serverUrl || prevUseProxyRef.current !== useProxy
    if (changed && isActive) {
      disconnect()
      setInspectionResult(null)
      setInspectionError(null)
    }
    prevServerUrlRef.current = serverUrl
    prevUseProxyRef.current = useProxy
  }, [serverUrl, useProxy]) // intentionally omit isActive/disconnect to avoid loop

  // On mount: remove any OAuth PKCE state keys that were stored with a different
  // origin (e.g. localhost:5003 when the app is now on localhost:8000, or after a
  // dev-server restart). These stale keys cause ERR_CONNECTION_REFUSED because the
  // OAuth provider redirects back to the old callback URL that no longer has a server.
  useEffect(() => {
    const currentOrigin = window.location.origin
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith('mcp:auth:state_')) {
        const raw = localStorage.getItem(key)
        if (raw && !raw.includes(currentOrigin)) {
          localStorage.removeItem(key)
        }
      }
    }
  }, [])

  // Check API health on mount and when server URL changes
  useEffect(() => {
    if (serverUrl) {
      checkApiHealth().then(setApiAvailable).catch(() => setApiAvailable(false))
    }
  }, [serverUrl])

  // Extract authentication details using custom hook
  const authDetails = useAuthDetails(serverUrl, isActive, state, authUrl)

  // A token (or an error) belongs to the endpoint it was issued for. Changing
  // the URL without dropping it would send server A's token to server B.
  const prevOAuthUrlRef = useRef(serverUrl)
  useEffect(() => {
    if (prevOAuthUrlRef.current !== serverUrl) {
      oauthProxy.reset()
      prevOAuthUrlRef.current = serverUrl
    }
  }, [serverUrl, oauthProxy])

  // Auto-generate server name from URL if not provided (for initial suggestion)
  const autoServerName = useServerName(serverUrl)
  
  // Use auto-generated name as default if user hasn't entered one
  const effectiveServerName = serverName.trim() || autoServerName

  // Generate connection details using custom hook
  const connectionDetails = useConnectionDetails(
    serverUrl,
    transportType,
    actualTransportType,
    authDetails,
    effectiveServerName,
    serverDescription,
    githubRepoLink,
    distributionType,
    bearerToken
  )

  // Format JSON for display in textarea
  const connectionDetailsJson = JSON.stringify(connectionDetails, null, 2)


  // Handle connection
  const handleConnect = () => {
    if (!serverUrl.trim() || !serverName.trim()) return
    // Clear all pending OAuth PKCE state keys before starting a new flow.
    // These are one-use verifiers — a leftover key from a previous abandoned flow
    // (closed popup, crashed dev server, port change) causes the callback redirect
    // to go to a dead URL. Always start fresh here.
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith('mcp:auth:state_')) {
        localStorage.removeItem(key)
      }
    }
    connect()
  }

  // Handle disconnection — cancels a running inspection job (closing the MCP
  // session on the backend) and disconnects the browser-side session
  const handleDisconnect = useCallback(async () => {
    if (activeJobId.current) {
      setIsCancelling(true)
      await cancelInspection(activeJobId.current)
      activeJobId.current = null
      setIsCancelling(false)
    }
    disconnect()
    setInspectionResult(null)
    setInspectionError(null)
    setInspectionErrorDetails(null)
    setJobStatus(null)
    setIsRunningInspection(false)
  }, [disconnect])

  // Handle clear storage
  const handleClearStorage = () => {
    clearStorage()
    setInspectionResult(null)
    setInspectionError(null)
  }

  // Handle run inspection (job-based: start → poll phases → result; cancellable
  // via the Disconnect button while running)
  const handleRunInspection = useCallback(async () => {
    if (!connectionDetails.endpoint_url || !connectionDetails.name.trim()) {
      return
    }

    setIsRunningInspection(true)
    setInspectionError(null)
    setInspectionErrorDetails(null)
    setInspectionResult(null)
    setJobStatus(null)

    try {
      // A token obtained via the backend OAuth proxy takes precedence over
      // browser-side (use-mcp) tokens — it works for CORS-blocked servers.
      const details = oauthProxy.token
        ? {
            ...connectionDetails,
            authentication: {
              authenticated: true,
              has_tokens: true,
              tokens: { ...oauthProxy.token },
            },
          }
        : connectionDetails

      const result = await runInspection(details, (status) => {
        activeJobId.current = status.job_id
        setJobStatus(status)
      })
      setInspectionResult(result)
    } catch (error) {
      if (error instanceof InspectionError) {
        if (error.message !== 'Inspection cancelled') {
          setInspectionError(error.message)
          setInspectionErrorDetails(error.errorDetails ?? null)
        }
      } else {
        setInspectionError(error instanceof Error ? error.message : 'Unknown error occurred')
      }
    } finally {
      activeJobId.current = null
      setIsRunningInspection(false)
      setJobStatus(null)
    }
  }, [connectionDetails, state, oauthProxy.token])

  // Populate the remote form from an imported config file (single-server case)
  const handleImportedServer = useCallback((server: ImportedServer) => {
    setServerName(server.name)
    sessionStorage.setItem('mcpServerName', server.name)
    setServerDescription(server.description || `MCP Server: ${server.name}`)
    sessionStorage.setItem('mcpServerDescription', server.description || `MCP Server: ${server.name}`)
    if (server.endpoint_url) {
      setServerUrl(server.endpoint_url)
      sessionStorage.setItem('mcpServerUrl', server.endpoint_url)
    }
    if (server.repository) {
      setGithubRepoLink(server.repository)
      sessionStorage.setItem('mcpGithubRepoLink', server.repository)
    }
    if (server.distribution_type) {
      setDistributionType(server.distribution_type)
      sessionStorage.setItem('mcpDistributionType', server.distribution_type)
    }
    // A static token in the imported authentication block pre-fills the token field
    const auth = server.authentication as Record<string, unknown> | undefined
    const importedToken = (auth?.access_token || auth?.token) as string | undefined
    if (importedToken && !importedToken.includes('*')) {
      setBearerToken(importedToken)
      sessionStorage.setItem('mcpBearerToken', importedToken)
    }
    // Imported stdio configs belong on the stdio tab
    if (server.connection_type === 'stdio' && !server.endpoint_url) {
      setActiveTab('stdio')
      sessionStorage.setItem('mcpConnectionTab', 'stdio')
    }
    setInspectionResult(null)
    setInspectionError(null)
    setInspectionErrorDetails(null)
  }, [])


  // Handle tab change
  const handleTabChange = useCallback((tab: ConnectionTab) => {
    setActiveTab(tab)
    sessionStorage.setItem('mcpConnectionTab', tab)
  }, [])

  const handleRunModeChange = useCallback((mode: 'single' | 'multiple') => {
    setRunMode(mode)
    sessionStorage.setItem('mcpRunMode', mode)
    // A selected batch row belongs to the multiple-servers pane; leaving that
    // mode clears it so the right pane does not show a stale report.
    if (mode === 'single') setSelectedBatchJob(null)
  }, [])

  // Handle clear all localStorage
  const handleClearAllStorage = useCallback(() => {
    if (window.confirm('Are you sure you want to clear all stored data? This will reset all form fields and connections.')) {
      // Cancel any inspection still running on the backend before tearing the
      // UI down — otherwise the job keeps a server session open with no way to
      // reach it from the reset page.
      if (activeJobId.current) {
        void cancelInspection(activeJobId.current)
        activeJobId.current = null
      }

      // Clear all storage used by the app (form fields, use-mcp tokens/state)
      localStorage.clear()
      sessionStorage.clear()

      // Reset every piece of component state. Several of these were previously
      // left behind (job status, error details, run mode, proxy flag…), which
      // is why the button looked like it did nothing: storage was empty but the
      // page still showed the old run.
      setServerUrl('')
      setTransportType('auto')
      setServerName('')
      setServerDescription('')
      setGithubRepoLink('')
      setDistributionType('')
      setBearerToken('')
      setUseProxy(false)
      setRunMode('single')
      setActiveTab('remote')
      setInspectionResult(null)
      setInspectionError(null)
      setInspectionErrorDetails(null)
      setJobStatus(null)
      setIsRunningInspection(false)
      setIsCancelling(false)
      setSelectedBatchJob(null)

      // The backend-OAuth token lives in hook state, not storage — it survives
      // localStorage.clear() unless explicitly dropped.
      oauthProxy.reset()

      // Clear MCP connection storage and disconnect the browser session
      if (clearStorage) {
        clearStorage()
      }
      if (isActive) {
        disconnect()
      }

      // Tell the multi-server run (and anything else keyed off this) to reset.
      setResetSignal((n) => n + 1)
    }
  }, [clearStorage, disconnect, isActive, oauthProxy])

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[7fr_5fr] gap-4 items-start">
      {/* Left pane holds the run queue with full endpoint URLs, so it gets the
          wider share; the preview pane only needs enough room for tab content. */}
      {/* ── Left pane: configuration & run controls ─────────────────────── */}
      {/* min-w-0: grid items default to min-width:auto and refuse to shrink
          below their content width, so one long unbreakable error string would
          otherwise force this 7fr track wider than the viewport and stretch
          every child (cards, banner, progress bar, queue table) with it. */}
      <section className="rounded-lg bg-white p-4 border border-zinc-200 min-w-0">
      <div className="flex items-center justify-between mb-4">
        <span className="text-sm font-semibold">MCP Server Connection</span>
        {activeTab === 'remote' && <StatusBadge state={state} isActive={isActive} />}
      </div>

      <p className="text-gray-500 text-xs mb-3">
        Connect to Model Context Protocol (MCP) servers to inspect connection details.
      </p>

      {/* Clear Storage Button */}
      <div className="mb-3 flex justify-end">
        <button
          type="button"
          onClick={handleClearAllStorage}
          className="text-xs text-gray-500 hover:text-gray-700 hover:underline px-2 py-1 rounded"
          title="Clear all stored data (localStorage and sessionStorage)"
        >
          🗑️ Clear All Data
        </button>
      </div>

      {/* Tab Toggle */}
      <ConnectionTabs activeTab={activeTab} onTabChange={handleTabChange} />

      {/* Single vs multiple. Single is the default and behaves exactly as before —
          Connect, browser OAuth and the live log all belong to that mode. Multiple
          runs N inspections at a time through the backend and has no browser
          connection of its own. */}
      <div className="flex items-center gap-4 mb-3 text-xs">
        {(['single', 'multiple'] as const).map((mode) => (
          <label key={mode} className="flex items-center gap-1.5 cursor-pointer">
            <input
              type="radio"
              name="mcpRunMode"
              value={mode}
              checked={runMode === mode}
              onChange={() => handleRunModeChange(mode)}
              disabled={isActive || isRunningInspection}
            />
            <span className={runMode === mode ? 'text-gray-900 font-medium' : 'text-gray-500'}>
              {mode === 'single' ? 'Single server' : 'Multiple servers'}
            </span>
          </label>
        ))}
        {runMode === 'multiple' && (
          <span className="text-gray-400">
            {activeTab === 'remote'
              ? 'Each endpoint keeps its own API key, bearer token or OAuth grant.'
              : 'Each repository is resolved and launched separately.'}
          </span>
        )}
      </div>

      {runMode === 'multiple' && (
          <MultiServerRun
          kind={activeTab === 'remote' ? 'remote' : 'github'}
          connectionType={activeTab === 'remote' ? transportType : 'stdio'}
          resetSignal={resetSignal}
          selectedJobId={selectedBatchJob?.job_id ?? null}
          onSelectJob={setSelectedBatchJob}
          onGroupChange={setSelectedBatchGroupId}   // ← add
        />
      )}

      {runMode === 'single' && (activeTab === 'remote' ? (
        <div className="space-y-3">
          {/* Import from client config */}
          <ConfigImport onSingleServer={handleImportedServer} />

          {/* Server Name - Required */}
          <div>
            <label className="block text-xs font-medium text-gray-700 mb-1">
              Server Name <span className="text-red-500">*</span>
            </label>
            <input
              type="text"
              className="w-full p-2 border border-gray-200 rounded text-sm placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300"
              placeholder="Enter server name (e.g., Linear MCP Server)"
              value={serverName}
              onChange={(e) => {
                const newValue = e.target.value
                setServerName(newValue)
                sessionStorage.setItem('mcpServerName', newValue)
              }}
              disabled={isActive && state !== 'failed'}
              required
            />
          </div>

          {/* Server Description - Required */}
          <div>
            <label className="block text-xs font-medium text-gray-700 mb-1">
              Description <span className="text-red-500">*</span>
            </label>
            <textarea
              className="w-full p-2 border border-gray-200 rounded text-sm placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300 resize-none"
              placeholder="Enter server description"
              rows={2}
              value={serverDescription}
              onChange={(e) => {
                const newValue = e.target.value
                setServerDescription(newValue)
                sessionStorage.setItem('mcpServerDescription', newValue)
              }}
              disabled={isActive && state !== 'failed'}
              required
            />
          </div>

          {/* GitHub Repo Link - Optional */}
          <div>
            <label className="block text-xs font-medium text-gray-700 mb-1">
              GitHub Repo Link <span className="text-gray-400 font-normal">(optional)</span>
            </label>
            <input
              type="url"
              className="w-full p-2 border border-gray-200 rounded text-sm placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300"
              value={githubRepoLink}
              onChange={(e) => {
                const newValue = e.target.value
                setGithubRepoLink(newValue)
                sessionStorage.setItem('mcpGithubRepoLink', newValue)
              }}
              disabled={isActive && state !== 'failed'}
            />
          </div>

          {/* Distribution Type - Optional */}
          <div>
            <label className="block text-xs font-medium text-gray-700 mb-1">
              Distribution Type <span className="text-gray-400 font-normal">(optional)</span>
            </label>
            <select
              className="w-full p-2 border border-gray-200 rounded text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
              value={distributionType}
              onChange={(e) => {
                const newValue = e.target.value
                setDistributionType(newValue)
                sessionStorage.setItem('mcpDistributionType', newValue)
              }}
              disabled={isActive && state !== 'failed'}
            >
              <option value="">Select distribution type</option>
              <option value="official">Official</option>
              <option value="community">Community</option>
            </select>
          </div>

          {/* Bearer Token / API Key - Optional, for servers that use static tokens instead of OAuth */}
          <div>
            <label className="block text-xs font-medium text-gray-700 mb-1">
              Bearer Token / API Key <span className="text-gray-400 font-normal">(optional)</span>
            </label>
            <input
              type="password"
              className="w-full p-2 border border-gray-200 rounded text-sm placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300"
              placeholder="Enter API key or Bearer token (for non-OAuth servers)"
              value={bearerToken}
              onChange={(e) => {
                const newValue = e.target.value
                setBearerToken(newValue)
                sessionStorage.setItem('mcpBearerToken', newValue)
              }}
              disabled={isActive && state !== 'failed'}
            />
            {bearerToken && (
              <p className="text-xs text-gray-500 mt-1">Token will be sent as <code className="bg-gray-100 px-1 rounded">Authorization: Bearer ...</code></p>
            )}
          </div>

          {/* Connection Controls */}
          <ConnectionControls
            serverUrl={serverUrl}
            transportType={transportType}
            state={state}
            isActive={isActive}
            useProxy={useProxy}
            onServerUrlChange={setServerUrl}
            onTransportTypeChange={setTransportType}
            onUseProxyChange={(next) => {
              setUseProxy(next)
              sessionStorage.setItem('mcpUseProxy', String(next))
            }}
            onConnect={handleConnect}
            onDisconnect={handleDisconnect}
            isConnectDisabled={!serverName.trim()}
          />

          {usingProxy && isActive && (
            <p className="text-xs text-gray-500">
              Connected through the backend proxy at <code className="bg-gray-100 px-1 rounded">/mcp-proxy</code>
              {actualTransportType && <> using {actualTransportType === 'http' ? 'Streamable HTTP' : 'SSE'}</>}.
            </p>
          )}

          {/* Connection error — shown when state is 'failed' with a readable message */}
          {state === 'failed' && connectionError && (
            <ErrorDisplay error={connectionError} />
          )}

          {/* Browser client log — the info/debug detail the use-mcp patch keeps
              out of the console, which is what makes a failed Connect diagnosable */}
          {isActive && <ConnectionLog log={connectionLog} />}

          {/* Live inspection progress with Disconnect (cancel) */}
          {isRunningInspection && (
            <InspectionProgress
              status={jobStatus}
              onDisconnect={handleDisconnect}
              isCancelling={isCancelling}
            />
          )}

          {/* Run Inspection Button - Show when URL + name are filled; backend bypasses CORS */}
          {(!!serverUrl && !!effectiveServerName) && !isRunningInspection && (
            <RunInspectionButton
              isRunning={isRunningInspection}
              apiAvailable={apiAvailable}
              onRun={handleRunInspection}
              isDisabled={!serverName.trim() || !serverDescription.trim()}
            />
          )}

          {/* Authentication Link if needed */}
          {authUrl && <AuthPrompt authUrl={authUrl} />}

          {/* Backend OAuth proxy — for authorization servers that block browser
              CORS. Shown only when there is genuine evidence OAuth is needed:
              the browser client surfaced an auth URL, a run failed with an
              OAuth/auth classification, or the user already holds a proxy token
              (so they can re-run after expiry). The old condition also fired on
              `oauthProxy.error`, which left the box on screen after any failed
              attempt even for servers that do not use OAuth at all. */}
          {!!serverUrl && !isRunningInspection && (
            authUrl ||
            oauthProxy.token ||
            inspectionErrorDetails?.error_type === 'OAUTH_REQUIRED' ||
            inspectionErrorDetails?.error_type === 'AUTH_INVALID_TOKEN'
          ) && (
            <div className="p-3 bg-purple-50 border border-purple-200 rounded space-y-2">
              <div className="text-xs font-medium text-purple-900">
                {oauthProxy.token
                  ? '✓ Authenticated via Inspector backend — Connect or run the inspection.'
                  : 'This server requires OAuth. If the browser flow fails (CORS), authenticate through the Inspector backend instead:'}
              </div>

              {/* A held token can expire or belong to a previous URL — offer a
                  fresh authorization without making the user clear storage. */}
              {oauthProxy.token ? (
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => { void oauthProxy.startOAuth() }}
                    disabled={oauthProxy.isAuthenticating}
                    className="px-4 py-2 bg-[#f4731c] text-white text-xs font-semibold rounded-full hover:bg-[#e0640f] disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
                  >
                    {oauthProxy.isAuthenticating ? 'Waiting for authorization…' : 'Re-authenticate (backend proxy)'}
                  </button>
                  <button
                    type="button"
                    onClick={() => oauthProxy.reset()}
                    className="text-xs text-gray-500 hover:text-gray-700 hover:underline"
                  >
                    Clear token
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => { void oauthProxy.startOAuth() }}
                  disabled={oauthProxy.isAuthenticating}
                  className="px-4 py-2 bg-[#f4731c] text-white text-xs font-semibold rounded-full hover:bg-[#e0640f] disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
                >
                  {oauthProxy.isAuthenticating ? 'Waiting for authorization…' : 'Authenticate (backend proxy)'}
                </button>
              )}

              {oauthProxy.error && (
                <p className="text-xs text-red-600">{oauthProxy.error}</p>
              )}

              {/* The browser popup flow stays one click away: when use-mcp
                  surfaced an auth URL, offer to open it again — this is the path
                  back after the user picked OAuth, the popup was closed or
                  blocked, and nothing further happened. */}
              {authUrl && (
                <button
                  type="button"
                  onClick={() => window.open(authUrl, 'mcp-oauth-browser', 'width=520,height=680')}
                  className="block text-xs text-purple-700 hover:text-purple-900 hover:underline"
                >
                  Retry the browser OAuth flow instead
                </button>
              )}
            </div>
          )}

          {/* Inspection Error (typed, from backend classifier when available) */}
          {inspectionError && (
            <ErrorDisplay error={inspectionError} errorDetails={inspectionErrorDetails} />
          )}

          {/* Clear Storage Button */}
          {isActive && (
            <button
              onClick={handleClearStorage}
              className="text-xs text-orange-600 hover:text-orange-800 hover:underline"
            >
              Clear stored authentication
            </button>
          )}
        </div>
      ) : (
        <StdioConnection
          serverName={serverName}
          serverDescription={serverDescription}
          onServerNameChange={setServerName}
          onServerDescriptionChange={setServerDescription}
        />
      ))}
      </section>

      {/* ── Right pane: report preview & downloads ────────────────────────
          Lives beside the inspector so a finished report is read next to the
          controls that produced it, not far down the page. Rendered for every
          mode; the empty state explains what lands here. */}
      <aside className="rounded-lg bg-white p-4 border border-zinc-200 lg:sticky lg:top-4 min-w-0">
        <div className="flex items-center justify-between mb-3">
          <span className="text-sm font-semibold">Report Preview</span>
          {inspectionResult?.success && <span className="text-xs text-green-700">✓ Ready</span>}
        </div>

        {/* Single-server result */}
        {runMode === 'single' && (
          inspectionResult ? (
            <div className="space-y-4">
              {/* Export Buttons */}
              {inspectionResult.success && inspectionResult.report_data && (
                <ExportButtons
                  serverName={inspectionResult.report_data.server_name || connectionDetails.name}
                  reportPaths={inspectionResult.report_paths}
                />
              )}
              <InspectionReport inspectionResult={inspectionResult} />

              {/* Connection Details — kept with the result it describes */}
              {state === 'ready' && (
                <div className="border-t border-gray-200 pt-3">
                  <label className="font-medium text-xs block mb-2">Connection Details</label>
                  <textarea
                    readOnly
                    value={connectionDetailsJson}
                    className="w-full h-[300px] p-3 border border-gray-200 rounded text-xs font-mono bg-gray-50 resize-none focus:outline-none focus:ring-1 focus:ring-blue-300"
                    placeholder="Connection details will appear here after connecting..."
                  />
                </div>
              )}
            </div>
          ) : (
            <p className="text-xs text-gray-400 leading-relaxed">
              Run an inspection and the report — summary, tools, resources, prompts and
              downloads — appears here, beside the controls.
              {activeTab === 'remote' && ' For multiple servers, pick a finished row in the queue to preview it here.'}
            </p>
          )
        )}

        {/* Multi-server result: the selected queue row previews here. */}
        {runMode === 'multiple' && (
          selectedBatchJob ? (
             <ReportPreview
              job={selectedBatchJob}
              onClose={() => setSelectedBatchJob(null)}
              batchGroupId={selectedBatchGroupId}
            />
          ) : (
            <p className="text-xs text-gray-400 leading-relaxed">
              The run queue and combined downloads are on the left. Select any finished
              row in the queue to read its full report — summary, capabilities and
              downloads — here.
            </p>
          )
        )}
      </aside>
    </div>
  )
}
