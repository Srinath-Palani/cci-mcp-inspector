import { isProxyAvailable } from '../utils/mcpProxyUrl.js'
import type { TransportType } from '../utils/mcpConnection.js'

interface ConnectionControlsProps {
  serverUrl: string
  transportType: TransportType
  state: string
  isActive: boolean
  useProxy: boolean
  onServerUrlChange: (url: string) => void
  onTransportTypeChange: (type: TransportType) => void
  onUseProxyChange: (useProxy: boolean) => void
  onConnect: () => void
  onDisconnect: () => void
  isConnectDisabled?: boolean
}

export function ConnectionControls({
  serverUrl,
  transportType,
  state,
  isActive,
  useProxy,
  onServerUrlChange,
  onTransportTypeChange,
  onUseProxyChange,
  onConnect,
  onDisconnect,
  isConnectDisabled = false,
}: ConnectionControlsProps) {
  const locked = isActive && state !== 'failed'
  const proxyAvailable = isProxyAvailable()

  return (
    <div className="space-y-2">
    <div className="flex gap-2 flex-wrap">
      <input
        type="text"
        className="flex-1 min-w-0 p-2 border border-gray-200 rounded text-sm placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300"
        placeholder="Enter MCP server URL"
        value={serverUrl}
        onChange={(e) => {
          const newValue = e.target.value
          onServerUrlChange(newValue)
          sessionStorage.setItem('mcpServerUrl', newValue)
        }}
        disabled={isActive && state !== 'failed'}
      />
      <select
        className="p-2 border border-gray-200 rounded text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
        value={transportType}
        onChange={(e) => {
          const newValue = e.target.value as TransportType
          onTransportTypeChange(newValue)
          sessionStorage.setItem('mcpTransportType', newValue)
        }}
        disabled={isActive && state !== 'failed'}
      >
        <option value="auto">Auto</option>
        <option value="http">Streamable HTTP (/mcp)</option>
        <option value="sse">SSE (/sse)</option>
      </select>

      {state === 'ready' || (isActive && state !== 'failed') ? (
        <button
          className="px-4 py-2 bg-orange-100 hover:bg-orange-200 text-orange-900 rounded text-sm font-medium whitespace-nowrap shrink-0"
          onClick={onDisconnect}
        >
          Disconnect
        </button>
      ) : (
        <button
          className="bg-blue-600 hover:bg-blue-700 text-white rounded py-2 px-4 text-sm font-medium disabled:opacity-50 whitespace-nowrap shrink-0"
          onClick={onConnect}
          disabled={isActive || !serverUrl.trim() || isConnectDisabled}
        >
          Connect
        </button>
      )}
    </div>

      {/* CORS bypass. Remote MCP servers rarely send Access-Control-Allow-Origin,
          so a direct browser connection fails with "Failed to fetch"; routing
          through the backend removes the cross-origin condition entirely. */}
      <label
        className={`flex items-start gap-2 text-xs ${proxyAvailable ? 'text-gray-600' : 'text-gray-400'}`}
        title={
          proxyAvailable
            ? 'Routes the browser connection through the Inspector backend at /mcp-proxy, bypassing CORS. Browser OAuth does not work in this mode — use a Bearer Token or the backend OAuth flow.'
            : 'Only available when running the vite dev server, which forwards /mcp-proxy to the backend.'
        }
      >
        <input
          type="checkbox"
          className="mt-0.5"
          checked={useProxy && proxyAvailable}
          disabled={!proxyAvailable || locked}
          onChange={(e) => onUseProxyChange(e.target.checked)}
        />
        <span>
          Connect through backend proxy (bypass CORS)
          {useProxy && proxyAvailable && (
            <span className="block text-gray-400">
              Browser OAuth is unavailable in proxy mode — use a Bearer Token, or run the
              inspection, which authenticates on the backend.
            </span>
          )}
          {!proxyAvailable && <span className="block">Requires the dev server.</span>}
        </span>
      </label>
    </div>
  )
}

