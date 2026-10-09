/**
 * Protocol version support, as measured rather than inferred.
 *
 * The backend offers each known protocol version in its own raw initialize request
 * (src/utility/protocol_version_prober.py) because the MCP SDK's ClientSession can
 * only ever offer its own latest version. Each probe therefore has a real verdict,
 * and the three verdicts are kept visually distinct:
 *
 *   ✓  the server echoed that version, or volunteered it while down-negotiating
 *   ✗  the server answered and declined it
 *   ?  no verdict — timeout, TLS failure, unreadable reply
 *
 * Rendering "?" as "✗" would claim evidence that does not exist, which is the whole
 * reason the backend keeps `supported` tri-state.
 */

interface ProtocolVersionProbe {
  version: string
  supported?: boolean | null
  server_reported?: string | null
  status_code?: number | null
  failure_kind?: string | null
  jsonrpc_error?: { code?: number; message?: string } | null
  error?: string | null
}

interface ProtocolVersionData {
  detected_version?: string | null
  negotiated_version?: string | null
  client_protocol_version?: string | null
  latest_supported?: string | null
  supported_versions?: string[]
  probes?: ProtocolVersionProbe[]
  probe_error?: string | null
  evidence_source?: string | null
}

function verdict(probe: ProtocolVersionProbe) {
  if (probe.supported === true) {
    return { icon: '✓', className: 'text-green-700', row: 'bg-green-50', text: 'Supported' }
  }
  if (probe.supported === false) {
    return { icon: '✗', className: 'text-red-600', row: '', text: 'Declined by server' }
  }
  return { icon: '?', className: 'text-amber-600', row: 'bg-amber-50/50', text: 'Undetermined' }
}

function reason(probe: ProtocolVersionProbe): string {
  if (probe.supported === true) {
    return probe.server_reported && probe.server_reported !== probe.version
      ? `server replied ${probe.server_reported}`
      : 'echoed by the server'
  }
  if (probe.jsonrpc_error?.code != null) {
    return `JSON-RPC ${probe.jsonrpc_error.code}: ${probe.jsonrpc_error.message || 'rejected'}`
  }
  if (probe.error) return probe.error
  if (probe.failure_kind) {
    return probe.status_code ? `${probe.failure_kind} (HTTP ${probe.status_code})` : probe.failure_kind
  }
  return '—'
}

interface ProtocolVersionMatrixProps {
  protocolVersion?: ProtocolVersionData | null
}

export function ProtocolVersionMatrix({ protocolVersion }: ProtocolVersionMatrixProps) {
  if (!protocolVersion) return null

  const probes = protocolVersion.probes || []
  const supported = protocolVersion.supported_versions || []

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="border border-gray-200 rounded p-2">
          <div className="text-gray-500">Latest supported</div>
          <div className="font-mono text-gray-900">
            {protocolVersion.latest_supported || <span className="text-gray-400">not established</span>}
          </div>
        </div>
        <div className="border border-gray-200 rounded p-2">
          <div className="text-gray-500">Negotiated in the live session</div>
          <div className="font-mono text-gray-900">
            {protocolVersion.negotiated_version || <span className="text-gray-400">no handshake</span>}
          </div>
        </div>
        <div className="border border-gray-200 rounded p-2">
          <div className="text-gray-500">Offered by this Inspector</div>
          <div className="font-mono text-gray-900">
            {protocolVersion.client_protocol_version || <span className="text-gray-400">—</span>}
          </div>
        </div>
        <div className="border border-gray-200 rounded p-2">
          <div className="text-gray-500">Evidence</div>
          <div className="text-gray-900">
            {protocolVersion.evidence_source || <span className="text-gray-400">—</span>}
          </div>
        </div>
      </div>

      {protocolVersion.probe_error && (
        <div className="text-xs bg-amber-50 border border-amber-200 rounded p-2 text-amber-800">
          No version could be confirmed: {protocolVersion.probe_error}
        </div>
      )}

      {probes.length > 0 ? (
        <table className="w-full text-xs border border-gray-200 rounded overflow-hidden">
          <thead className="bg-gray-50 text-gray-500">
            <tr>
              <th className="text-left px-2 py-1.5 font-medium w-8"> </th>
              <th className="text-left px-2 py-1.5 font-medium">Version offered</th>
              <th className="text-left px-2 py-1.5 font-medium">Verdict</th>
              <th className="text-left px-2 py-1.5 font-medium">Why</th>
            </tr>
          </thead>
          <tbody>
            {probes.map((probe) => {
              const v = verdict(probe)
              return (
                <tr key={probe.version} className={`border-t border-gray-100 ${v.row}`}>
                  <td className={`px-2 py-1.5 text-center font-bold ${v.className}`}>{v.icon}</td>
                  <td className="px-2 py-1.5 font-mono text-gray-900">{probe.version}</td>
                  <td className={`px-2 py-1.5 ${v.className}`}>{v.text}</td>
                  <td className="px-2 py-1.5 text-gray-500">{reason(probe)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      ) : (
        supported.length > 0 && (
          <div className="text-xs text-gray-600">
            Confirmed versions: <span className="font-mono">{supported.join(', ')}</span>
          </div>
        )
      )}

      <p className="text-xs text-gray-400">
        ✓ echoed by the server · ✗ answered and declined · ? no verdict (timeout, TLS or
        unreadable reply)
      </p>
    </div>
  )
}
