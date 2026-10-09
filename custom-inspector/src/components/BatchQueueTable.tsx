/**
 * Live queue for a multi-server run.
 *
 * Rows stay in the order they were entered and keep their identity (R1, G2, …) for
 * the whole run, so a row remains recognisable before its server name is known —
 * GitHub targets only get a name after the repo is resolved.
 *
 * "Waiting for a free slot" is shown as its own state: with N running at a time the
 * difference between queued and stalled matters, and conflating them is what makes a
 * bounded run look broken.
 */

import type { JobStatus } from '../utils/apiClient.js'

const stateStyles: Record<string, { icon: string; className: string }> = {
  queued: { icon: '⏳', className: 'text-gray-500' },
  pending: { icon: '⏳', className: 'text-gray-500' },
  running: { icon: '🔄', className: 'text-blue-600' },
  done: { icon: '✅', className: 'text-green-700' },
  error: { icon: '❌', className: 'text-red-600' },
  cancelled: { icon: '⛔', className: 'text-gray-500' },
}

function timing(job: JobStatus): string {
  if (job.state === 'queued' || job.state === 'pending') {
    return job.queued_seconds ? `queued ${job.queued_seconds}s` : 'queued'
  }
  if (job.elapsed_seconds != null) {
    return `${job.elapsed_seconds}s`
  }
  return ''
}

function label(job: JobStatus): string {
  if (job.state === 'error') {
    return job.error_details?.error_type || job.error || 'Failed'
  }
  if (job.state === 'cancelled') {
    // task_settled=false means the request already in flight is still unwinding on
    // its worker thread — cancellation is a request, not an instant stop.
    return job.task_settled === false ? 'Cancelling…' : 'Cancelled'
  }
  return job.phase_label || job.current_phase
}

interface BatchQueueTableProps {
  jobs: JobStatus[]
  selectedJobId?: string | null
  onSelect: (job: JobStatus) => void
  /** Cancel one queued/running job. Absent → no per-row cancel button. */
  onCancelJob?: (job: JobStatus) => void
  /** Job ids with a cancel in flight — shows a spinner instead of the button. */
  cancellingIds?: ReadonlySet<string>
  /** Re-run one failed/cancelled job in place. Absent → no per-row retry button. */
  onRetryJob?: (job: JobStatus) => void
  /** Job ids with a retry in flight — disables that row's retry button. */
  retryingIds?: ReadonlySet<string>
  /**
   * Download one finished server's own report files (attributes CSV /
   * capabilities CSV) straight from its row. Absent → no per-row CSV buttons.
   */
  onDownloadJob?: (job: JobStatus, format: 'csv' | 'attributes_csv' | 'capabilities_csv') => void
}

export function BatchQueueTable({ jobs, selectedJobId, onSelect, onCancelJob, cancellingIds, onRetryJob, retryingIds, onDownloadJob }: BatchQueueTableProps) {
  if (jobs.length === 0) return null

  return (
    // Fixed layout keeps Status/Time/Reports from being squeezed off the edge
    // when the pane is narrow; the container scrolls sideways instead of
    // clipping the download buttons. min-w stays under the left grid column
    // (~44rem at max-w-7xl) so the Retry/action column is visible without
    // horizontal scrolling at the default two-pane layout.
    <div className="border border-gray-200 rounded overflow-x-auto">
      <table className="w-full text-xs table-fixed min-w-[32rem]">
        <thead className="bg-gray-50 text-gray-500">
          <tr>
            <th className="text-left px-2 py-1.5 font-medium w-10">#</th>
            <th className="text-left px-2 py-1.5 font-medium">Server</th>
            <th className="text-left px-2 py-1.5 font-medium w-40">Status</th>
            <th className="text-right px-2 py-1.5 font-medium w-14">Time</th>
            {onDownloadJob && <th className="text-right px-2 py-1.5 font-medium w-28">Reports</th>}
            {(onCancelJob || onRetryJob) && <th className="w-14" />}
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => {
            const style = stateStyles[job.state] || stateStyles.pending
            const openable = job.state === 'done' || job.state === 'error'
            const selected = selectedJobId === job.job_id
            const cancellable =
              job.state === 'queued' || job.state === 'pending' || job.state === 'running'
            const retryable = job.state === 'error' || job.state === 'cancelled'
            const cancelling = cancellingIds?.has(job.job_id) ?? false
            const retrying = retryingIds?.has(job.job_id) ?? false
            return (
              <tr
                key={job.job_id}
                onClick={() => openable && onSelect(job)}
                className={`border-t border-gray-100 ${
                  selected ? 'bg-blue-50' : openable ? 'hover:bg-gray-50 cursor-pointer' : ''
                }`}
                title={openable ? 'Show this report below' : undefined}
              >
                <td className="px-2 py-1.5 font-mono text-gray-400">{job.identity || '—'}</td>
                <td className="px-2 py-1.5">
                  <div className="text-gray-900 truncate" title={job.target || job.server_name}>
                    {job.server_name}
                  </div>
                  {job.target && job.target !== job.server_name && (
                    // break-all so the full endpoint URL wraps and stays readable
                    // instead of truncating mid-host.
                    <div className="text-gray-400 break-all" title={job.target}>{job.target}</div>
                  )}
                </td>
                <td className={`px-2 py-1.5 ${style.className}`} title={label(job)}>
                  <span className="mr-1">{style.icon}</span>
                  {label(job)}
                </td>
                <td className="px-2 py-1.5 text-right text-gray-500 whitespace-nowrap">{timing(job)}</td>
                {onDownloadJob && (
                  <td className="px-2 py-1 text-right whitespace-nowrap">
                    {job.state === 'done' && (
                      <>
                        <button
                          type="button"
                          onClick={(e) => {
                            // Row click opens the report preview; downloads must
                            // not bubble up to it.
                            e.stopPropagation()
                            onDownloadJob(job, 'attributes_csv')
                          }}
                          className="px-1.5 py-0.5 text-blue-700 hover:bg-blue-50 rounded"
                          title={`Attribute key-value CSV for ${job.server_name}`}
                        >
                          Attr CSV
                        </button>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation()
                            onDownloadJob(job, 'capabilities_csv')
                          }}
                          className="px-1.5 py-0.5 text-blue-700 hover:bg-blue-50 rounded ml-1"
                          title={`Tool capabilities CSV for ${job.server_name}`}
                        >
                          Caps CSV
                        </button>
                      </>
                    )}
                  </td>
                )}
                {(onCancelJob || onRetryJob) && (
                  <td className="px-1 py-1 text-right whitespace-nowrap">
                    {onRetryJob && retryable && (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation()
                          onRetryJob(job)
                        }}
                        disabled={retrying}
                        className="px-1.5 py-0.5 text-blue-700 hover:bg-blue-50 rounded disabled:opacity-50 disabled:cursor-not-allowed"
                        title={`Re-run ${job.server_name || job.target || 'this server'} only`}
                        aria-label={`Retry ${job.server_name || job.target || 'this server'}`}
                      >
                        {retrying ? '…' : '↻ Retry'}
                      </button>
                    )}
                    {onCancelJob && cancellable && (
                      <button
                        type="button"
                        onClick={(e) => {
                          // Row click opens the report for finished jobs; the cancel
                          // button must not bubble up to it.
                          e.stopPropagation()
                          onCancelJob(job)
                        }}
                        disabled={cancelling}
                        className="px-1.5 py-0.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded disabled:opacity-50 disabled:cursor-not-allowed"
                        title={`Cancel ${job.server_name || job.target || 'this server'}`}
                        aria-label={`Cancel ${job.server_name || job.target || 'this server'}`}
                      >
                        {cancelling ? '…' : '✕'}
                      </button>
                    )}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
