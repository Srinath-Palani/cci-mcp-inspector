import { useMemo, useState } from 'react'

export interface ConnectionLogEntry {
  level: string
  message: string
  timestamp: number
}

interface ConnectionLogProps {
  log: ConnectionLogEntry[]
}

const LEVEL_STYLE: Record<string, string> = {
  error: 'text-red-600',
  warn: 'text-amber-600',
  info: 'text-gray-700',
  debug: 'text-gray-400',
}

function formatTime(timestamp: number): string {
  const d = new Date(timestamp)
  const pad = (n: number, width = 2) => String(n).padStart(width, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`
}

/**
 * Collapsible view of the browser MCP client's connection log.
 *
 * The vendored use-mcp patch keeps `info`/`debug` messages out of the browser
 * console, so without this panel the transport negotiation and OAuth steps were
 * invisible — which is exactly the detail needed to diagnose a failed Connect.
 */
export function ConnectionLog({ log }: ConnectionLogProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [showDebug, setShowDebug] = useState(false)

  const visible = useMemo(
    () => (showDebug ? log : log.filter((entry) => entry.level !== 'debug')),
    [log, showDebug],
  )

  if (log.length === 0) return null

  const errorCount = log.filter((entry) => entry.level === 'error' || entry.level === 'warn').length

  return (
    <div className="mt-3 border border-gray-200 rounded-md bg-white">
      <button
        type="button"
        onClick={() => setIsOpen((open) => !open)}
        className="w-full flex items-center justify-between px-3 py-2 text-xs text-gray-600 hover:bg-gray-50"
      >
        <span>
          Connection log <span className="text-gray-400">({log.length} entries</span>
          {errorCount > 0 && <span className="text-amber-600">, {errorCount} warning/error</span>}
          <span className="text-gray-400">)</span>
        </span>
        <span className="text-gray-400">{isOpen ? '▲' : '▼'}</span>
      </button>

      {isOpen && (
        <div className="border-t border-gray-200">
          <label className="flex items-center gap-2 px-3 py-1.5 text-xs text-gray-500">
            <input
              type="checkbox"
              checked={showDebug}
              onChange={(e) => setShowDebug(e.target.checked)}
            />
            Show debug messages
          </label>
          <pre className="max-h-64 overflow-auto px-3 pb-2 text-[11px] leading-relaxed font-mono whitespace-pre-wrap break-words">
            {visible.map((entry, i) => (
              <div key={`${entry.timestamp}-${i}`} className={LEVEL_STYLE[entry.level] ?? 'text-gray-700'}>
                <span className="text-gray-400">{formatTime(entry.timestamp)} </span>
                <span className="uppercase">{entry.level}</span> {entry.message}
              </div>
            ))}
          </pre>
        </div>
      )}
    </div>
  )
}
