import { useState } from 'react'

type ErrorCategory = 'cors' | 'auth' | 'network' | 'timeout' | 'protocol' | 'env' | 'config' | 'server' | 'unknown'

/** Structured error payload from the backend (preferred over regex categorization). */
export interface ErrorDetails {
  error_type: string
  title: string
  description: string
  suggestions: Array<{ text: string; action?: string }>
  technical_detail?: string
  stage?: string
  status_code?: number
}

interface ParsedError {
  category: ErrorCategory
  title: string
  message: string
  suggestions: string[]
  stage?: string
  errorType?: string
}

/** Map a backend error_type to a display category (badge color group). */
// No CORS entry: CORS is a browser-only failure the backend cannot observe, so no
// backend error_type maps to it. Browser-side CORS failures arrive as a plain
// error string and are handled by categorize() below.
const ERROR_TYPE_CATEGORY: Record<string, ErrorCategory> = {
  OAUTH_REQUIRED: 'auth',
  AUTH_INVALID_TOKEN: 'auth',
  AUTH_FORBIDDEN: 'auth',
  CONNECTION_TIMEOUT: 'timeout',
  DNS_FAILURE: 'network',
  TLS_ERROR: 'network',
  TRANSPORT_MISMATCH: 'protocol',
  ENV_VARS_MISSING: 'env',
  ENV_VARS_PLACEHOLDER: 'env',
  CONFIG_INVALID: 'config',
  SERVER_ERROR: 'server',
  UNKNOWN: 'unknown',
}

function fromBackend(details: ErrorDetails): ParsedError {
  return {
    category: ERROR_TYPE_CATEGORY[details.error_type] ?? 'unknown',
    title: details.title,
    message: details.description,
    suggestions: details.suggestions.map((s) => s.text),
    stage: details.stage,
    errorType: details.error_type,
  }
}

/** Fallback regex categorization when the backend did not supply error_details. */
function categorize(error: string): ParsedError {
  if (/failed to fetch|networkerror|load failed|cors|cross.origin/i.test(error)) {
    return {
      category: 'cors',
      title: 'CORS Block',
      message: 'The browser blocked the connection due to Cross-Origin policy.',
      suggestions: [
        'Remote MCP servers reject direct browser connections.',
        'Use the "Run Inspection" button — it routes through the backend proxy and bypasses CORS.',
        'For OAuth servers, use the "Authenticate" button — the backend completes the flow without CORS restrictions.',
      ],
    }
  }

  if (/401|403|unauthorized|forbidden/i.test(error)) {
    return {
      category: 'auth',
      title: 'Authentication Failed',
      message: 'The server rejected the request (401/403).',
      suggestions: [
        'Re-authenticate via the OAuth button.',
        'Check that your token has not expired.',
        'Ensure the token has the required scopes.',
      ],
    }
  }

  if (/timeout|timed out|etimedout/i.test(error)) {
    return {
      category: 'timeout',
      title: 'Connection Timeout',
      message: 'The server did not respond in time.',
      suggestions: [
        'Check that the server URL is correct and the server is running.',
        'Try again — the server may be temporarily unavailable.',
      ],
    }
  }

  if (/econnrefused|connection refused|enotfound|cannot connect/i.test(error)) {
    return {
      category: 'network',
      title: 'Connection Refused',
      message: 'Could not reach the MCP server.',
      suggestions: [
        'Verify the server URL and port are correct.',
        'For local servers, confirm the server process is running.',
      ],
    }
  }

  if (/protocol|invalid response|unexpected token|json/i.test(error)) {
    return {
      category: 'protocol',
      title: 'Protocol Error',
      message: 'The server returned an unexpected response.',
      suggestions: [
        'Confirm the endpoint supports the MCP protocol.',
        'Try switching transport: SSE (/sse) vs Streamable HTTP (/mcp).',
      ],
    }
  }

  return {
    category: 'unknown',
    title: 'Inspection Failed',
    message: error.split('\n')[0] || error,
    suggestions: [],
  }
}

const BADGE_STYLES: Record<ErrorCategory, string> = {
  cors:     'bg-orange-100 text-orange-800 border-orange-300',
  auth:     'bg-red-100    text-red-800    border-red-300',
  network:  'bg-yellow-100 text-yellow-800 border-yellow-300',
  timeout:  'bg-yellow-100 text-yellow-800 border-yellow-300',
  protocol: 'bg-blue-100   text-blue-800   border-blue-300',
  env:      'bg-purple-100 text-purple-800 border-purple-300',
  config:   'bg-blue-100   text-blue-800   border-blue-300',
  server:   'bg-red-100    text-red-800    border-red-300',
  unknown:  'bg-gray-100   text-gray-800   border-gray-300',
}

interface ErrorDisplayProps {
  error: string
  /** Structured error from the backend; preferred over the regex fallback when present. */
  errorDetails?: ErrorDetails | null
}

export function ErrorDisplay({ error, errorDetails }: ErrorDisplayProps) {
  const [showDetails, setShowDetails] = useState(false)
  const [copied, setCopied] = useState(false)
  const parsed = errorDetails ? fromBackend(errorDetails) : categorize(error)
  const technicalText = errorDetails?.technical_detail || error

  const handleCopy = () => {
    navigator.clipboard.writeText(technicalText).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  return (
    <div className="p-3 bg-red-50 border border-red-200 rounded space-y-2">
      {/* Header row */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs font-medium text-red-900">Inspection Failed</span>
        <span className={`text-xs font-medium px-2 py-0.5 rounded border ${BADGE_STYLES[parsed.category]}`}>
          {parsed.title}
        </span>
        {parsed.errorType && (
          <span className="text-xs font-mono text-red-400">{parsed.errorType}</span>
        )}
      </div>

      {/* Human-readable message */}
      <p className="text-xs text-red-800">{parsed.message}</p>

      {/* Suggestions */}
      {parsed.suggestions.length > 0 && (
        <ul className="text-xs text-red-700 space-y-0.5 list-disc list-inside">
          {parsed.suggestions.map((s, i) => (
            <li key={i}>{s}</li>
          ))}
        </ul>
      )}

      {/* Stage indicator */}
      {parsed.stage && parsed.stage !== 'unknown' && (
        <p className="text-xs text-red-500">Failed during: {parsed.stage.replace(/_/g, ' ')}</p>
      )}

      {/* Actions */}
      <div className="flex gap-2 pt-1">
        <button
          className="text-xs text-red-600 underline hover:no-underline"
          onClick={() => setShowDetails((v) => !v)}
        >
          {showDetails ? 'Hide technical details' : 'Show technical details'}
        </button>
        <button
          className="text-xs text-red-600 underline hover:no-underline"
          onClick={handleCopy}
        >
          {copied ? 'Copied!' : 'Copy error'}
        </button>
      </div>

      {/* Raw error (collapsible) */}
      {showDetails && (
        <pre className="text-xs text-red-800 bg-red-100 border border-red-200 rounded p-2 overflow-x-auto whitespace-pre-wrap break-words max-h-48 overflow-y-auto">
          {technicalText}
        </pre>
      )}
    </div>
  )
}
