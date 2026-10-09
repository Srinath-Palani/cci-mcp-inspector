/**
 * API Client for MCP Inspector API Bridge
 * Handles communication with the backend inspection service
 */

const API_BASE_URL = 'http://localhost:8000'

/**
 * fetch() rejects with TypeError("Failed to fetch") whenever the request never
 * completes — API bridge not running, port wrong, connection refused. Catching
 * it once here lets every endpoint report the real cause instead of the raw
 * browser message, which reads like a bug in the button the user clicked.
 */
function backendUnreachableError(): Error {
  return new Error(
    `Cannot reach the Inspector backend at ${API_BASE_URL}. Start the API bridge (python api_bridge/main.py) and try again.`
  )
}

export interface ConnectionDetails {
  name: string
  description: string
  connection_type: string
  endpoint_url?: string
  authentication?: {
    authenticated: boolean
    has_tokens: boolean
    auth_url?: string
    tokens?: {
      access_token?: string
      token_type?: string
      expires_in?: number
      scope?: string
      refresh_token?: string
      client_id?: string
      token_url?: string
    }
  }
  // Stdio connection fields
  command?: string
  args?: string[]
  env?: Record<string, string>
  repository?: string  // GitHub repository URL for stdio servers
  // Optional metadata fields (used for remote endpoint)
  github_repo_link?: string
  distribution_type?: string
}

export interface GenerateStdioConfigRequest {
  repository_url: string
  server_name?: string
}

export interface GenerateStdioConfigResponse {
  success: boolean
  config?: {
    name: string
    description: string
    connection_type: string
    repository?: string
    command: string
    args: string[]
  }
  env_vars?: string[]
  error?: string
}

export interface TestStdioConnectionRequest {
  name: string
  command: string
  args: string[]
  env?: Record<string, string>
}

export interface TestStdioConnectionResponse {
  success: boolean
  connected: boolean
  error?: string
}

export interface InspectRequest {
  name?: string
  description?: string
  connection_type: string
  endpoint_url?: string
  authentication?: ConnectionDetails['authentication']
  // Stdio connection fields
  command?: string
  args?: string[]
  env?: Record<string, string>
  repository?: string  // GitHub repository URL for stdio servers
  // Optional metadata fields (used for remote endpoint)
  github_repo_link?: string
  distribution_type?: string
  // Names of env vars the server requires — backend validates values and
  // fills gaps from its own environment (.env)
  required_env_vars?: string[]
}

/** Structured error payload from the backend error classifier. */
export interface ApiErrorDetails {
  error_type: string
  title: string
  description: string
  suggestions: Array<{ text: string; action?: string }>
  technical_detail?: string
  stage?: string
  status_code?: number
}

export interface InspectResponse {
  success: boolean
  report_path?: string
  report_paths?: {
    json?: string
    markdown?: string
    html?: string
    /** Attribute checklist CSV — one row per checked attribute. */
    csv?: string
    /** Capability CSV — one row per tool / resource / prompt. A different report. */
    capabilities_csv?: string
    txt?: string
  }
  report_data?: {
    server_name: string
    connection_type: string
    discovery_timestamp: string
    server_attributes: any
    capabilities: any
    tools: any[]
    resources: any[]
    prompts: any[]
    analysis: any
    statistics: {
      total_tools: number
      total_resources: number
      total_prompts: number
    }
    metadata: any
  }
  error?: string
  error_details?: ApiErrorDetails
}

export interface ExportResponse {
  success: boolean
  report_path?: string
  formats?: string[]
  error?: string
}

/**
 * Check if API is available
 */
export async function checkApiHealth(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE_URL}/`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
      },
    })
    return response.ok
  } catch (error) {
    return false
  }
}

/**
 * Generate stdio configuration from GitHub repository
 */
export async function generateStdioConfig(
  request: GenerateStdioConfigRequest
): Promise<GenerateStdioConfigResponse> {
  const response = await fetch(`${API_BASE_URL}/api/generate-stdio-config`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(request),
  })

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ error: 'Unknown error' }))
    throw new Error(errorData.detail || errorData.error || `HTTP ${response.status}`)
  }

  return response.json()
}

/**
 * Test stdio connection
 */
export async function testStdioConnection(
  request: TestStdioConnectionRequest
): Promise<TestStdioConnectionResponse> {
  const response = await fetch(`${API_BASE_URL}/api/test-stdio-connection`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(request),
  })

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ error: 'Unknown error' }))
    throw new Error(errorData.detail || errorData.error || `HTTP ${response.status}`)
  }

  return response.json()
}

// ── Job-based inspection API ─────────────────────────────────────────────────

export interface InspectStartResponse {
  job_id?: string | null
  status: 'started' | 'error'
  error?: string | null
  error_details?: ApiErrorDetails | null
}

export interface JobStatus {
  job_id: string
  server_name: string
  // 'queued' means admitted to a multi-server run but waiting for a free slot —
  // accepted, not yet started. Distinct from 'pending' (created, not dispatched).
  state: 'pending' | 'queued' | 'running' | 'done' | 'cancelled' | 'error'
  current_phase: string
  phase_label: string
  progress: number
  result?: InspectResponse | null
  error?: string | null
  error_details?: ApiErrorDetails | null
  /** What the user typed for this row (URL or repo), set for multi-server runs. */
  target?: string | null
  target_kind?: 'remote' | 'github' | 'stdio' | 'invalid' | null
  /** Stable row label: R1/R2 for endpoints, G1/G2 for repos, X1 for a bad line. */
  identity?: string | null
  /** 0-based position in the submitted list, so rows keep the entered order. */
  position?: number | null
  queued_seconds?: number
  elapsed_seconds?: number | null
  cancel_requested?: boolean
  task_settled?: boolean
}

/** Thrown when the backend reports a structured inspection failure. */
export class InspectionError extends Error {
  errorDetails?: ApiErrorDetails
  constructor(message: string, errorDetails?: ApiErrorDetails | null) {
    super(message)
    this.name = 'InspectionError'
    this.errorDetails = errorDetails ?? undefined
  }
}

function buildInspectRequest(connectionDetails: ConnectionDetails | InspectRequest): InspectRequest {
  return {
    name: connectionDetails.name,
    description: connectionDetails.description,
    connection_type: connectionDetails.connection_type,
    endpoint_url: connectionDetails.endpoint_url,
    authentication: connectionDetails.authentication,
    command: connectionDetails.command,
    args: connectionDetails.args,
    env: connectionDetails.env,
    repository: connectionDetails.repository,
    github_repo_link: connectionDetails.github_repo_link,
    distribution_type: connectionDetails.distribution_type,
    required_env_vars: (connectionDetails as InspectRequest).required_env_vars,
  }
}

/**
 * Start an inspection job. Returns the job_id immediately; the job runs in
 * the backend and is polled via getInspectionStatus / cancelled via
 * cancelInspection. Throws InspectionError (with error_details) when the
 * request is rejected up-front (validation, env vars, etc.).
 */
export async function startInspection(
  connectionDetails: ConnectionDetails | InspectRequest
): Promise<string> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/api/inspect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(buildInspectRequest(connectionDetails)),
    })
  } catch {
    throw new InspectionError(backendUnreachableError().message)
  }

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ error: 'Unknown error' }))
    throw new InspectionError(errorData.detail || errorData.error || `HTTP ${response.status}`)
  }

  const data: InspectStartResponse = await response.json()
  if (data.status !== 'started' || !data.job_id) {
    throw new InspectionError(data.error || 'Failed to start inspection', data.error_details)
  }
  return data.job_id
}

/** Poll the state of a running inspection job. */
export async function getInspectionStatus(jobId: string): Promise<JobStatus> {
  const response = await fetch(`${API_BASE_URL}/api/inspect/${jobId}/status`)
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }))
    throw new Error(errorData.detail || `HTTP ${response.status}`)
  }
  return response.json()
}

/** Cancel a running inspection job (the Disconnect button). */
export async function cancelInspection(jobId: string): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/inspect/${jobId}/cancel`, { method: 'POST' })
    if (!response.ok) return false
    const data = await response.json().catch(() => null)
    return data?.success !== false
  } catch {
    return false
  }
}

/**
 * Cancel one job inside a batch group. Uses the same endpoint as a single-job
 * cancel; the row flips to 'cancelled' immediately and the next batch-status
 * poll confirms it.
 */
export async function cancelBatchJob(jobId: string): Promise<boolean> {
  return cancelInspection(jobId)
}

/**
 * Run MCP server inspection to completion: start a job, poll until it reaches
 * a terminal state, and return the final report. `onProgress` receives each
 * status snapshot for live phase display; `signal`-less cancellation is done
 * by calling cancelInspection(jobId) from the onProgress consumer.
 */
export async function runInspection(
  connectionDetails: ConnectionDetails | InspectRequest,
  onProgress?: (status: JobStatus) => void
): Promise<InspectResponse> {
  const jobId = await startInspection(connectionDetails)

  // Poll every second; a stalled job (no phase change for 6 minutes) is
  // treated as a timeout so the UI never spins forever.
  const STALL_LIMIT_MS = 6 * 60_000
  let lastPhase = ''
  let lastChange = Date.now()

  while (true) {
    await new Promise((r) => setTimeout(r, 1000))
    const status = await getInspectionStatus(jobId)
    onProgress?.(status)

    if (status.current_phase !== lastPhase) {
      lastPhase = status.current_phase
      lastChange = Date.now()
    }

    if (status.state === 'done' && status.result) {
      return status.result
    }
    if (status.state === 'cancelled') {
      throw new InspectionError('Inspection cancelled')
    }
    if (status.state === 'error') {
      throw new InspectionError(status.error || 'Inspection failed', status.error_details)
    }
    if (Date.now() - lastChange > STALL_LIMIT_MS) {
      await cancelInspection(jobId)
      throw new InspectionError(
        'Inspection stalled — no progress for 6 minutes. The server is not responding to the MCP handshake.'
      )
    }
  }
}

// ── Batch inspection (Import from client config) ────────────────────────────

export interface BatchJobRef {
  name: string
  job_id: string
  target?: string | null
  target_kind?: string | null
  identity?: string | null
  position?: number | null
}

export interface BatchStartResponse {
  group_id: string
  concurrency?: number
  jobs: BatchJobRef[]
  errors: Array<{ name: string; error: string; error_details?: ApiErrorDetails | null }>
  /** Targets dropped because the same server was listed twice. */
  duplicates_removed?: string[]
}

export interface BatchCounts {
  queued: number
  running: number
  done: number
  error: number
  cancelled: number
  pending: number
}

export interface BatchStatus {
  group_id: string
  state: 'running' | 'done'
  jobs: JobStatus[]
  aggregated?: { csv?: string; capabilities_csv?: string; html?: string; json?: string } | null
  /** How many run at once. Absent for groups started before this was added. */
  concurrency?: number
  total?: number
  counts?: BatchCounts
  /** The aggregate only covers jobs that completed — cancelled/failed rows are not in it. */
  aggregate_covers?: { reports: number; total: number } | null
}

export type AuthMode = 'none' | 'bearer' | 'api_key' | 'oauth'

/**
 * One server's own credential.
 *
 * Each remote server authenticates separately, so this is per target and is never
 * shared across a run: `bearer` sends Authorization: Bearer, `api_key` sends the
 * token under its own header (X-API-Key unless `header` says otherwise), and
 * `oauth` carries the tokens from a completed backend OAuth flow for that endpoint.
 */
export interface BatchTargetAuth {
  mode: AuthMode
  token?: string
  header?: string
  tokens?: {
    access_token?: string
    refresh_token?: string
    client_id?: string
    token_url?: string
    expires_at?: number
    expires_in?: number
  }
}

/** One target for a multi-server run: a remote endpoint or a GitHub repository. */
export interface BatchTargetInput {
  target: string
  /** Stated explicitly so the backend never has to guess which tab this came from. */
  kind: 'remote' | 'github'
  name?: string
  connection_type?: string
  /** This target's own credential. Not inherited from the run or other targets. */
  auth?: BatchTargetAuth
  env?: Record<string, string>
  required_env_vars?: string[]
  authentication?: ConnectionDetails['authentication']
  distribution_type?: string
  github_repo_link?: string
}

export interface BatchRunOptions {
  /** How many servers to inspect at once. Backend clamps to 1–5. */
  concurrency?: number
  /** Default transport for remote targets that do not specify one. */
  connection_type?: string
  /** Env values applied to every target. Per-run only — never written to .env. */
  env?: Record<string, string>
}

// Hard ceiling of 5, mirroring MAX_BATCH_CONCURRENCY in api_bridge/main.py. The
// backend clamps independently, so these staying in sync is a UI concern only —
// the select must not offer a value the server will silently lower.
export const MIN_BATCH_CONCURRENCY = 1
export const MAX_BATCH_CONCURRENCY = 5
export const DEFAULT_BATCH_CONCURRENCY = 5

/** Start one inspection job per imported server config. */
export async function startBatchInspection(servers: InspectRequest[]): Promise<BatchStartResponse> {
  const response = await fetch(`${API_BASE_URL}/api/inspect/batch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ servers }),
  })
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }))
    throw new Error(errorData.detail || `HTTP ${response.status}`)
  }
  return response.json()
}

/**
 * Start a multi-server run from raw targets (endpoint URLs or GitHub repos).
 *
 * At most `concurrency` inspections run at once; the rest report state 'queued'.
 * Targets that cannot be used still come back as jobs — they fail with structured
 * error_details, so every submitted line keeps a row.
 */
export async function startBatchTargets(
  targets: BatchTargetInput[],
  options: BatchRunOptions = {}
): Promise<BatchStartResponse> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/api/inspect/batch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        targets,
        concurrency: options.concurrency ?? DEFAULT_BATCH_CONCURRENCY,
        connection_type: options.connection_type ?? 'auto',
        env: options.env,
      }),
    })
  } catch {
    throw backendUnreachableError()
  }
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }))
    throw new Error(errorData.detail || `HTTP ${response.status}`)
  }
  return response.json()
}

/** Cancel every unfinished job in a group: queued ones never start. */
export async function cancelBatch(groupId: string): Promise<{ cancelled: number; jobs?: JobStatus[] }> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/inspect/batch/${groupId}/cancel`, {
      method: 'POST',
    })
    if (!response.ok) return { cancelled: 0 }
    return response.json()
  } catch {
    return { cancelled: 0 }
  }
}

// ── Environment configuration (read-only) ───────────────────────────────────

export interface EnvVarStatus {
  name: string
  description?: string
  required: boolean
  set: boolean
  placeholder: boolean
  /** Length only. The API never returns env values. */
  value_length: number
  source?: '.env file' | 'process environment' | null
}

export interface EnvConfig {
  dotenv_path: string
  dotenv_exists: boolean
  template_path?: string | null
  variables: EnvVarStatus[]
  missing_required: string[]
  resolution_order: string[]
  note: string
}

/**
 * Read the backend's env var status. Status only — values are never returned,
 * and this endpoint cannot change anything: .env is edited on disk by the user.
 */
export async function getEnvConfig(): Promise<EnvConfig> {
  const response = await fetch(`${API_BASE_URL}/api/config/env`)
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`)
  }
  return response.json()
}

/** Poll a batch group's per-job statuses + aggregated report paths. */
export async function getBatchStatus(groupId: string): Promise<BatchStatus> {
  const response = await fetch(`${API_BASE_URL}/api/inspect/batch/${groupId}/status`)
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }))
    throw new Error(errorData.detail || `HTTP ${response.status}`)
  }
  return response.json()
}

/**
 * Re-run one failed or cancelled row in place. The backend re-queues that job
 * alone and rebuilds the combined report once it settles, so the caller only
 * needs to keep polling the group status.
 */
export async function retryBatchJob(groupId: string, jobId: string): Promise<void> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/api/inspect/batch/${groupId}/retry/${jobId}`, {
      method: 'POST',
    })
  } catch {
    throw backendUnreachableError()
  }
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }))
    throw new Error(errorData.detail || `HTTP ${response.status}`)
  }
}

// ── Auth discovery (what does this endpoint require?) ───────────────────────

export interface AuthDiscoveryResult {
  success: boolean
  auth_required: boolean
  /** Auth type detected from the handshake challenge: 'oauth2' | 'bearer_token' | 'api_key' | null. */
  auth_type?: string | null
  /** Discovered OAuth config when the server publishes metadata; null otherwise. */
  auth_config?: Record<string, unknown> | null
  metadata_available?: boolean
  recommendations?: string[]
  source?: string | null
  error?: string
}

/**
 * Probe one endpoint and report what authentication it wants — the backend
 * tries an unauthenticated connection and the RFC 9728 metadata endpoints.
 * This is what powers the per-row Connect button: the user should never have
 * to guess the auth mode.
 */
export async function discoverAuth(endpointUrl: string): Promise<AuthDiscoveryResult> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}/api/discover-auth`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ endpoint_url: endpointUrl }),
    })
  } catch {
    throw backendUnreachableError()
  }
  return response.json().catch(() => ({ success: false, auth_required: false, error: `HTTP ${response.status}` }))
}

/**
 * The report formats a finished inspection can produce. Mirrors `format_map` in
 * api_bridge/main.py — three different CSVs: `attributes_csv` is the attribute
 * key-value list, `csv` is the attribute checklist (Yes/No per option), and
 * `capabilities_csv` is the tool listing. They are separate reports.
 */
export type ReportFormat = 'json' | 'markdown' | 'html' | 'csv' | 'attributes_csv' | 'capabilities_csv' | 'txt'

/** Real file extension per format, for the browser's save dialog. */
export const REPORT_FILE_EXT: Record<ReportFormat, string> = {
  json: 'json',
  markdown: 'md',
  html: 'html',
  csv: 'csv',
  attributes_csv: 'csv',
  capabilities_csv: 'csv',
  txt: 'txt',
}

/**
 * Download the combined report covering every server in a batch group.
 *
 * Only servers that finished successfully appear in it — check `aggregate_covers`
 * on the status payload to see how much of the group that is.
 */
/** The combined-batch formats that can be downloaded. */
export type BatchDownloadFormat = 'csv' | 'capabilities_csv' | 'html' | 'json'

/** File extension per combined format (capabilities_csv is a CSV). */
const BATCH_FILE_EXT: Record<BatchDownloadFormat, string> = {
  csv: 'csv',
  capabilities_csv: 'csv',
  html: 'html',
  json: 'json',
}

export async function downloadBatchReport(groupId: string, format: BatchDownloadFormat): Promise<void> {
  let response: Response
  try {
    response = await fetch(
      `${API_BASE_URL}/api/inspect/batch/${encodeURIComponent(groupId)}/download/${format}`
    )
  } catch {
    throw backendUnreachableError()
  }
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }))
    throw new Error(errorData.detail || `Failed to download combined report: HTTP ${response.status}`)
  }

  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  const stem = format === 'capabilities_csv' ? 'capabilities' : 'combined'
  a.download = `mcp_batch_${groupId.slice(0, 8)}_${stem}_${Date.now()}.${BATCH_FILE_EXT[format]}`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

/**
 * Download ONE server's report from inside a batch group — the per-server
 * counterpart of downloadBatchReport. `format` is the same set as single
 * inspections: `csv` = attribute checklist, `capabilities_csv` = capabilities.
 */
export async function downloadBatchServerReport(
  groupId: string,
  serverName: string,
  format: ReportFormat
): Promise<void> {
  let response: Response
  try {
    response = await fetch(
      `${API_BASE_URL}/api/inspect/batch/${encodeURIComponent(groupId)}/server/${encodeURIComponent(serverName)}/download/${format}`
    )
  } catch {
    throw backendUnreachableError()
  }
  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }))
    throw new Error(errorData.detail || `Failed to download ${serverName} report: HTTP ${response.status}`)
  }

  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  const stem = format === 'capabilities_csv' ? 'capabilities'
    : format === 'attributes_csv' ? 'attributes'
    : format === 'csv' ? 'checklist'
    : format
  a.download = `${serverName}_${stem}.${REPORT_FILE_EXT[format]}`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

/**
 * Download a report from the server in the specified format
 * Uses the server-generated reports (more comprehensive than client-side generation)
 */
export async function downloadServerReport(
  serverName: string,
  format: ReportFormat = 'json'
): Promise<void> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/reports/${encodeURIComponent(serverName)}/${format}`, {
      method: 'GET',
    })

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }))
      throw new Error(errorData.detail || `Failed to download report: HTTP ${response.status}`)
    }

    // Get the blob and trigger download
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    // Extension must be the real one: "capabilities_csv" is a CSV, and "markdown"
    // is a .md — using the format key verbatim would save files the OS cannot open.
    const stem = format === 'capabilities_csv' ? 'capabilities' : 'report'
    a.download = `mcp_inspection_${stem}_${serverName}_${Date.now()}.${REPORT_FILE_EXT[format]}`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (error) {
    throw new Error(`Failed to download server report: ${error instanceof Error ? error.message : String(error)}`)
  }
}

/**
 * List all available reports for a server
 */
export async function listAvailableReports(serverName: string): Promise<{
  server_name: string
  reports: Record<string, { path: string; download_url: string; size: number }>
  count: number
}> {
  const response = await fetch(`${API_BASE_URL}/api/reports/${encodeURIComponent(serverName)}`, {
    method: 'GET',
  })

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({ detail: `HTTP ${response.status}` }))
    throw new Error(errorData.detail || `Failed to list reports: HTTP ${response.status}`)
  }

  return response.json()
}

/**
 * Download report data and trigger browser download
 */
export function downloadReportData(
  reportData: InspectResponse['report_data'],
  format: 'json' | 'markdown' | 'html' = 'json'
): void {
  if (!reportData) {
    throw new Error('No report data to download')
  }

  let content: string
  let mimeType: string
  let extension: string

  switch (format) {
    case 'json':
      content = JSON.stringify(reportData, null, 2)
      mimeType = 'application/json'
      extension = 'json'
      break
    case 'markdown':
      // Convert report to markdown format
      content = generateMarkdownReport(reportData)
      mimeType = 'text/markdown'
      extension = 'md'
      break
    case 'html':
      // Convert report to HTML format
      content = generateHtmlReport(reportData)
      mimeType = 'text/html'
      extension = 'html'
      break
    default:
      content = JSON.stringify(reportData, null, 2)
      mimeType = 'application/json'
      extension = 'json'
  }

  const blob = new Blob([content], { type: mimeType })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `mcp_inspection_report_${reportData.server_name}_${Date.now()}.${extension}`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

/**
 * Generate markdown report from inspection data
 */
function generateMarkdownReport(data: InspectResponse['report_data']): string {
  if (!data) return ''

  let md = `# MCP Server Inspection Report\n\n`
  md += `**Server Name:** ${data.server_name}\n\n`
  md += `**Connection Type:** ${data.connection_type}\n\n`
  md += `**Discovery Timestamp:** ${data.discovery_timestamp}\n\n`
  md += `**Statistics:**\n`
  md += `- Tools: ${data.statistics.total_tools}\n`
  md += `- Resources: ${data.statistics.total_resources}\n`
  md += `- Prompts: ${data.statistics.total_prompts}\n\n`

  if (data.tools && data.tools.length > 0) {
    md += `## Tools\n\n`
    data.tools.forEach((tool: any) => {
      md += `### ${tool.name || 'Unknown Tool'}\n\n`
      if (tool.description) md += `${tool.description}\n\n`
      if (tool.inputSchema) {
        md += `**Input Schema:**\n\`\`\`json\n${JSON.stringify(tool.inputSchema, null, 2)}\n\`\`\`\n\n`
      }
    })
  }

  if (data.resources && data.resources.length > 0) {
    md += `## Resources\n\n`
    data.resources.forEach((resource: any) => {
      md += `### ${resource.name || 'Unknown Resource'}\n\n`
      if (resource.description) md += `${resource.description}\n\n`
      if (resource.uri) md += `**URI:** ${resource.uri}\n\n`
    })
  }

  if (data.prompts && data.prompts.length > 0) {
    md += `## Prompts\n\n`
    data.prompts.forEach((prompt: any) => {
      md += `### ${prompt.name || 'Unknown Prompt'}\n\n`
      if (prompt.description) md += `${prompt.description}\n\n`
    })
  }

  if (data.server_attributes) {
    md += `## Server Attributes\n\n`
    md += `\`\`\`json\n${JSON.stringify(data.server_attributes, null, 2)}\n\`\`\`\n\n`
  }

  return md
}

/**
 * Generate HTML report from inspection data
 */
function generateHtmlReport(data: InspectResponse['report_data']): string {
  if (!data) return ''

  let html = `<!DOCTYPE html>
<html>
<head>
  <title>MCP Inspection Report - ${data.server_name}</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; line-height: 1.6; }
    h1 { color: #333; }
    h2 { color: #555; margin-top: 30px; }
    h3 { color: #777; margin-top: 20px; }
    pre { background: #f4f4f4; padding: 10px; border-radius: 5px; overflow-x: auto; }
    .stat { display: inline-block; margin: 10px 20px 10px 0; }
    .stat-label { font-weight: bold; color: #666; }
  </style>
</head>
<body>
  <h1>MCP Server Inspection Report</h1>
  <p><strong>Server Name:</strong> ${data.server_name}</p>
  <p><strong>Connection Type:</strong> ${data.connection_type}</p>
  <p><strong>Discovery Timestamp:</strong> ${data.discovery_timestamp}</p>
  <div>
    <span class="stat"><span class="stat-label">Tools:</span> ${data.statistics.total_tools}</span>
    <span class="stat"><span class="stat-label">Resources:</span> ${data.statistics.total_resources}</span>
    <span class="stat"><span class="stat-label">Prompts:</span> ${data.statistics.total_prompts}</span>
  </div>
`

  if (data.tools && data.tools.length > 0) {
    html += `  <h2>Tools</h2>\n`
    data.tools.forEach((tool: any) => {
      html += `    <h3>${tool.name || 'Unknown Tool'}</h3>\n`
      if (tool.description) html += `    <p>${tool.description}</p>\n`
      if (tool.inputSchema) {
        html += `    <p><strong>Input Schema:</strong></p>\n`
        html += `    <pre>${JSON.stringify(tool.inputSchema, null, 2)}</pre>\n`
      }
    })
  }

  if (data.resources && data.resources.length > 0) {
    html += `  <h2>Resources</h2>\n`
    data.resources.forEach((resource: any) => {
      html += `    <h3>${resource.name || 'Unknown Resource'}</h3>\n`
      if (resource.description) html += `    <p>${resource.description}</p>\n`
      if (resource.uri) html += `    <p><strong>URI:</strong> ${resource.uri}</p>\n`
    })
  }

  if (data.prompts && data.prompts.length > 0) {
    html += `  <h2>Prompts</h2>\n`
    data.prompts.forEach((prompt: any) => {
      html += `    <h3>${prompt.name || 'Unknown Prompt'}</h3>\n`
      if (prompt.description) html += `    <p>${prompt.description}</p>\n`
    })
  }

  if (data.server_attributes) {
    html += `  <h2>Server Attributes</h2>\n`
    html += `    <pre>${JSON.stringify(data.server_attributes, null, 2)}</pre>\n`
  }

  html += `</body>\n</html>`
  return html
}

