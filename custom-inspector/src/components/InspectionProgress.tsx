/**
 * Live inspection progress: phase checklist + progress bar + Disconnect.
 *
 * Shown while a job is running. Disconnect cancels the backend job (closing
 * the MCP session) and returns the UI to idle.
 */

import type { JobStatus } from '../utils/apiClient.js'

const PHASES: Array<{ key: string; label: string }> = [
  { key: 'load_config', label: 'Load configuration' },
  { key: 'auth_discovery', label: 'Authentication discovery' },
  { key: 'mcp_discovery', label: 'Connect & discover capabilities' },
  { key: 'llm_analysis', label: 'LLM analysis' },
  { key: 'attribute_extraction', label: 'Attribute extraction' },
  { key: 'generate_report', label: 'Report generation' },
]

interface InspectionProgressProps {
  status: JobStatus | null
  onDisconnect: () => void
  isCancelling?: boolean
}

export function InspectionProgress({ status, onDisconnect, isCancelling }: InspectionProgressProps) {
  const currentPhase = status?.current_phase ?? 'pending'
  const progress = status?.progress ?? 0
  const currentIndex = PHASES.findIndex((p) => p.key === currentPhase)

  return (
    <div className="p-3 bg-blue-50 border border-blue-200 rounded space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-green-500 animate-pulse" />
          <span className="text-xs font-medium text-blue-900">
            Connected — {status?.phase_label ?? 'Starting inspection…'}
          </span>
        </div>
        <button
          type="button"
          onClick={onDisconnect}
          disabled={isCancelling}
          className="px-3 py-1.5 bg-red-600 text-white text-xs font-medium rounded hover:bg-red-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
        >
          {isCancelling ? 'Disconnecting…' : 'Disconnect'}
        </button>
      </div>

      {/* Progress bar */}
      <div className="w-full h-1.5 bg-blue-100 rounded-full overflow-hidden">
        <div
          className="h-full bg-blue-600 rounded-full transition-all duration-500"
          style={{ width: `${Math.round(progress * 100)}%` }}
        />
      </div>

      {/* Phase checklist */}
      <ul className="space-y-0.5">
        {PHASES.map((phase, i) => {
          const isDone = currentPhase === 'done' || (currentIndex >= 0 && i < currentIndex)
          const isCurrent = phase.key === currentPhase
          return (
            <li key={phase.key} className="flex items-center gap-2 text-xs">
              <span className="w-4 text-center">
                {isDone ? '✅' : isCurrent ? '⏳' : '○'}
              </span>
              <span className={isCurrent ? 'font-medium text-blue-900' : isDone ? 'text-blue-700' : 'text-blue-400'}>
                {phase.label}
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
