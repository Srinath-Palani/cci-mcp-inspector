/**
 * Import from client config — JSON file import for both remote and stdio tabs.
 *
 * Accepts two formats (auto-detected):
 * 1. Inspector format: {"servers": [{name, connection_type, command, args, env,
 *    endpoint_url, repository, authentication, skip, distribution_type}, ...]}
 *    — the same schema the CLI consumes via --config mcp.json
 * 2. Standard client config: {"mcpServers": {"name": {command, args, env}}}
 *    (Claude Desktop / Cursor style; a "url" key means a remote server)
 *
 * A single-server file populates the active form; a multi-server file opens a
 * picker (skip:1 entries unchecked, mirroring the CLI --all semantics) and
 * runs the selection through the backend batch endpoint with live per-server
 * status and the same aggregated CSV/HTML the CLI produces.
 */

import { useCallback, useRef, useState } from 'react'
import {
  startBatchInspection,
  getBatchStatus,
  type BatchStatus,
  type InspectRequest,
} from '../utils/apiClient.js'

export interface ImportedServer {
  name: string
  description?: string
  connection_type: string
  command?: string
  args?: string[]
  env?: Record<string, string>
  endpoint_url?: string
  repository?: string
  distribution_type?: string
  skip?: number
  authentication?: Record<string, unknown>
}

/** Parse a config file's text into a normalized server list. Throws with a readable message. */
export function parseClientConfig(text: string): ImportedServer[] {
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch (e) {
    const detail = e instanceof Error ? e.message : String(e)
    throw new Error(
      `Invalid JSON: ${detail}. Check for trailing commas, missing quotes, or unclosed brackets.`
    )
  }
  if (typeof data !== 'object' || data === null) {
    throw new Error('Config file must contain a JSON object.')
  }

  const obj = data as Record<string, unknown>

  // Format 1: Inspector format {"servers": [...]}
  if (Array.isArray(obj.servers)) {
    const servers = (obj.servers as Array<Record<string, unknown>>).map((s, i) => {
      if (!s.name || typeof s.name !== 'string') {
        throw new Error(`servers[${i}] is missing a "name" field.`)
      }
      return normalizeServer(s as unknown as ImportedServer)
    })
    if (servers.length === 0) throw new Error('The "servers" list is empty.')
    return servers
  }

  // Format 2: client config {"mcpServers": {name: {...}}}
  if (obj.mcpServers && typeof obj.mcpServers === 'object') {
    const entries = Object.entries(obj.mcpServers as Record<string, Record<string, unknown>>)
    if (entries.length === 0) throw new Error('The "mcpServers" object is empty.')
    return entries.map(([name, cfg]) => {
      const url = (cfg.url || cfg.endpoint_url) as string | undefined
      return normalizeServer({
        name,
        description: (cfg.description as string) || `MCP Server: ${name}`,
        connection_type: url ? 'auto' : 'stdio',
        command: cfg.command as string | undefined,
        args: (cfg.args as string[]) || undefined,
        env: (cfg.env as Record<string, string>) || undefined,
        endpoint_url: url,
      })
    })
  }

  throw new Error(
    'Unrecognized config format. Expected {"servers": [...]} (Inspector format) or {"mcpServers": {...}} (client config format).'
  )
}

function normalizeServer(s: ImportedServer): ImportedServer {
  return {
    ...s,
    description: s.description || `MCP Server: ${s.name}`,
    connection_type: (s.connection_type || (s.endpoint_url ? 'auto' : 'stdio')).toLowerCase(),
  }
}

function toInspectRequest(s: ImportedServer): InspectRequest {
  return {
    name: s.name,
    description: s.description,
    connection_type: s.connection_type,
    endpoint_url: s.endpoint_url,
    command: s.command,
    args: s.args,
    env: s.env,
    repository: s.repository,
    distribution_type: s.distribution_type,
  }
}

interface ConfigImportProps {
  /** Called when the file contains exactly one server — populate the form. */
  onSingleServer: (server: ImportedServer) => void
}

export function ConfigImport({ onSingleServer }: ConfigImportProps) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [importError, setImportError] = useState<string | null>(null)
  const [servers, setServers] = useState<ImportedServer[] | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [batchStatus, setBatchStatus] = useState<BatchStatus | null>(null)
  const [batchErrors, setBatchErrors] = useState<Array<{ name: string; error: string }>>([])
  const [isRunning, setIsRunning] = useState(false)

  const handleFile = useCallback(
    (file: File) => {
      setImportError(null)
      setServers(null)
      setBatchStatus(null)
      setBatchErrors([])
      const reader = new FileReader()
      reader.onload = () => {
        try {
          const parsed = parseClientConfig(String(reader.result))
          if (parsed.length === 1) {
            onSingleServer(parsed[0])
          } else {
            setServers(parsed)
            // Pre-select servers without skip:1 — mirrors CLI --all semantics
            setSelected(new Set(parsed.map((s, i) => (s.skip === 1 ? -1 : i)).filter((i) => i >= 0)))
          }
        } catch (e) {
          setImportError(e instanceof Error ? e.message : String(e))
        }
      }
      reader.onerror = () => setImportError('Could not read the file.')
      reader.readAsText(file)
    },
    [onSingleServer]
  )

  const handleRunSelected = useCallback(async () => {
    if (!servers) return
    const chosen = servers.filter((_, i) => selected.has(i))
    if (chosen.length === 0) return

    setIsRunning(true)
    setBatchStatus(null)
    setBatchErrors([])

    try {
      const start = await startBatchInspection(chosen.map(toInspectRequest))
      setBatchErrors(start.errors || [])

      // Poll until the whole group is done
      while (true) {
        await new Promise((r) => setTimeout(r, 1500))
        const status = await getBatchStatus(start.group_id)
        setBatchStatus(status)
        if (status.state === 'done') break
      }
    } catch (e) {
      setImportError(e instanceof Error ? e.message : String(e))
    } finally {
      setIsRunning(false)
    }
  }, [servers, selected])

  const toggle = (i: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })
  }

  return (
    <div className="space-y-2">
      <input
        ref={fileRef}
        type="file"
        accept=".json,application/json"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) handleFile(file)
          e.target.value = '' // allow re-importing the same file
        }}
      />
      <button
        type="button"
        onClick={() => fileRef.current?.click()}
        className="px-4 py-1.5 bg-[#f4731c] text-white text-xs font-semibold rounded-full hover:bg-[#e0640f] transition-colors"
      >
        Import JSON file
      </button>

      {importError && (
        <div className="p-2 bg-red-50 border border-red-200 rounded">
          <div className="text-xs font-medium text-red-900">Config import failed</div>
          <div className="text-xs text-red-800 mt-0.5">{importError}</div>
        </div>
      )}

      {/* Multi-server picker */}
      {servers && (
        <div className="p-3 bg-gray-50 border border-gray-200 rounded space-y-2">
          <div className="text-xs font-medium text-gray-700">
            {servers.length} servers found — select which to inspect:
          </div>
          <ul className="space-y-1">
            {servers.map((s, i) => (
              <li key={i} className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={selected.has(i)}
                  onChange={() => toggle(i)}
                  disabled={isRunning}
                />
                <span className="font-medium">{s.name}</span>
                <span className="text-gray-400">
                  {s.connection_type === 'stdio'
                    ? `stdio: ${s.command ?? '?'}`
                    : s.endpoint_url ?? 'remote'}
                </span>
                {s.skip === 1 && <span className="text-orange-500">(skip flag set)</span>}
              </li>
            ))}
          </ul>
          <button
            type="button"
            onClick={handleRunSelected}
            disabled={isRunning || selected.size === 0}
            className="px-4 py-2 bg-blue-600 text-white text-xs font-medium rounded hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
          >
            {isRunning ? 'Inspecting…' : `Run inspection for ${selected.size} server(s)`}
          </button>
        </div>
      )}

      {/* Per-entry validation errors from the batch start */}
      {batchErrors.length > 0 && (
        <div className="p-2 bg-orange-50 border border-orange-200 rounded space-y-1">
          {batchErrors.map((e, i) => (
            <div key={i} className="text-xs text-orange-800">
              <span className="font-medium">{e.name}:</span> {e.error}
            </div>
          ))}
        </div>
      )}

      {/* Batch progress / results */}
      {batchStatus && (
        <div className="p-3 bg-blue-50 border border-blue-200 rounded space-y-1">
          <div className="text-xs font-medium text-blue-900">
            Batch inspection {batchStatus.state === 'done' ? 'complete' : 'running…'}
          </div>
          <ul className="space-y-0.5">
            {batchStatus.jobs.map((j) => (
              <li key={j.job_id} className="flex items-center gap-2 text-xs text-blue-800">
                <span>
                  {j.state === 'done' ? '✅' : j.state === 'error' ? '❌' : j.state === 'cancelled' ? '⚪' : '⏳'}
                </span>
                <span className="font-medium">{j.server_name}</span>
                <span className="text-blue-500">
                  {j.state === 'running' ? j.phase_label : j.state}
                </span>
                {j.error && <span className="text-red-600 truncate">{j.error.split('\n')[0]}</span>}
              </li>
            ))}
          </ul>
          {batchStatus.aggregated && (
            <div className="text-xs text-blue-800 pt-1">
              Aggregated reports: <code>{batchStatus.aggregated.csv}</code> ·{' '}
              <code>{batchStatus.aggregated.html}</code>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
