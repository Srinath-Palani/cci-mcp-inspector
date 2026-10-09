/**
 * Inspect several MCP servers in one run, N at a time.
 *
 * The backend runs at most `concurrency` inspections simultaneously (1–10, default
 * 5) and holds the rest in state "queued", so submitting ten endpoints does not open
 * ten sessions, ten LLM calls and — for GitHub targets — ten subprocess launches at
 * once. Every submitted row gets a job, including rows the backend cannot use: those
 * fail individually with a structured error instead of taking down the run.
 *
 * Credentials are per row and never shared. Nothing is persisted: tokens live in
 * component state for the lifetime of the page, and the backend writes nothing to
 * .env.
 */

import { useCallback, useEffect, useState } from 'react'
import {
  cancelBatch,
  cancelBatchJob,
  downloadBatchReport,
  downloadBatchServerReport,
  getBatchStatus,
  retryBatchJob,
  startBatchTargets,
  DEFAULT_BATCH_CONCURRENCY,
  MAX_BATCH_CONCURRENCY,
  MIN_BATCH_CONCURRENCY,
  type BatchDownloadFormat,
  type BatchStatus,
  type JobStatus,
} from '../utils/apiClient.js'
import {
  BatchTargetRows,
  incompleteAuthRows,
  makeRow,
  missingNameRows,
  rowsToTargets,
  type TargetKind,
  type TargetRow,
} from './BatchTargetRows.js'
import { BatchQueueTable } from './BatchQueueTable.js'

const POLL_INTERVAL_MS = 1500

const concurrencyOptions = Array.from(
  { length: MAX_BATCH_CONCURRENCY - MIN_BATCH_CONCURRENCY + 1 },
  (_, i) => MIN_BATCH_CONCURRENCY + i
)

interface MultiServerRunProps {
  kind: TargetKind
  /** Transport for remote targets that do not override it (the tab's setting). */
  connectionType?: string
  /**
   * Bump to reset the run: clears rows, the group and all status back to the
   * initial empty form. Wired to "Clear All Data" so a wipe does not leave a
   * finished batch on screen over blank storage.
   */
  resetSignal?: number
  /**
   * Selection is controlled by the parent so the selected row's report can be
   * rendered in the right-hand preview pane instead of inline below the queue.
   */
  selectedJobId?: string | null
  onSelectJob?: (job: JobStatus | null) => void
  /**
   * The preview pane downloads through the batch per-server endpoint (single-
   * inspection downloads 404 for batch rows), so it needs the group id. Reported
   * whenever a run starts.
   */
  onGroupChange?: (groupId: string | null) => void
}

export function MultiServerRun({
  kind,
  connectionType = 'auto',
  resetSignal = 0,
  selectedJobId: selectedJobIdProp,
  onSelectJob,
  onGroupChange,
}: MultiServerRunProps) {
  const [rows, setRows] = useState<TargetRow[]>([makeRow()])
  const [concurrency, setConcurrency] = useState(DEFAULT_BATCH_CONCURRENCY)
  const [groupId, setGroupId] = useState<string | null>(null)
  const [status, setStatus] = useState<BatchStatus | null>(null)
  const [startError, setStartError] = useState<string | null>(null)
  const [duplicates, setDuplicates] = useState<string[]>([])
  const [internalSelectedJobId, setInternalSelectedJobId] = useState<string | null>(null)
  // Controlled when the parent renders the preview (right pane); internal otherwise.
  const selectedJobId = selectedJobIdProp !== undefined ? selectedJobIdProp : internalSelectedJobId
  const [isStarting, setIsStarting] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)
  const [cancellingIds, setCancellingIds] = useState<ReadonlySet<string>>(new Set())
  const [retryingIds, setRetryingIds] = useState<ReadonlySet<string>>(new Set())

  const targets = rowsToTargets(rows, kind)
  const incomplete = incompleteAuthRows(rows)
  const missingName = missingNameRows(rows)
  const isRunning = status?.state === 'running'
  const counts = status?.counts

  // Clear All Data: drop the whole run — rows, group, statuses and errors — and
  // stop the poll. Runs are per-page state only; nothing on the backend needs
  // cleaning up because a done group is inert and a running one is cancelled first.
  useEffect(() => {
    if (resetSignal === 0) return
    if (groupId && status?.state === 'running') {
      void cancelBatch(groupId)
    }
    setRows([makeRow()])
    setGroupId(null)
    onGroupChange?.(null)
    setStatus(null)
    setStartError(null)
    setDuplicates([])
    setInternalSelectedJobId(null)
    onSelectJob?.(null)
    setDownloadError(null)
    setCancellingIds(new Set())
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetSignal])

  // Poll while the group is unfinished. Depending on the two primitive fields rather
  // than on `status` keeps the interval from being torn down and rebuilt every tick,
  // and returning early once the group is done stops the requests for good.
  const statusGroupId = status?.group_id
  const statusState = status?.state

  useEffect(() => {
    if (!groupId) return
    if (statusGroupId === groupId && statusState === 'done') return

    let cancelled = false
    const poll = async () => {
      try {
        const next = await getBatchStatus(groupId)
        if (cancelled) return
        setStatus(next)
      } catch {
        // A failed poll is transient (the backend restarting, a dropped request);
        // the next tick retries. Dropping the run entirely would be worse than a gap.
      }
    }

    poll()
    const timer = setInterval(poll, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [groupId, statusGroupId, statusState])

  const handleStart = useCallback(async () => {
    setStartError(null)
    setDuplicates([])
    setInternalSelectedJobId(null)
    onSelectJob?.(null)

    if (targets.length === 0) {
      setStartError(kind === 'remote' ? 'Add at least one endpoint URL.' : 'Add at least one repository URL.')
      return
    }

    if (missingName.length > 0) {
      setStartError(
        missingName.length === 1
          ? 'One server is missing a name — the Name field is required for every row.'
          : `${missingName.length} servers are missing a name — the Name field is required for every row.`
      )
      return
    }

    setIsStarting(true)
    try {
      const started = await startBatchTargets(targets, {
        concurrency,
        connection_type: connectionType,
      })
      setDuplicates(started.duplicates_removed || [])
      setStatus(null)
      setGroupId(started.group_id)
      onGroupChange?.(started.group_id)
    } catch (e) {
      setStartError(e instanceof Error ? e.message : String(e))
    } finally {
      setIsStarting(false)
    }
  }, [targets, kind, concurrency, connectionType, onSelectJob, missingName.length])

  // Apply a post-cancel snapshot to local state immediately. The per-job cancel
  // endpoint returns nothing, so the row would otherwise keep showing "running"
  // until the next 1.5s poll tick — which reads as "cancel did nothing".
  const applyCancelledJob = useCallback((jobId: string) => {
    setStatus((prev) => {
      if (!prev) return prev
      const jobs = prev.jobs.map((j) =>
        j.job_id === jobId &&
        (j.state === 'queued' || j.state === 'pending' || j.state === 'running')
          ? { ...j, state: 'cancelled' as const, cancel_requested: true }
          : j
      )
      const counts = { queued: 0, running: 0, done: 0, error: 0, cancelled: 0, pending: 0 }
      for (const j of jobs) counts[j.state] = (counts[j.state] ?? 0) + 1
      return { ...prev, jobs, counts }
    })
  }, [])

  const handleCancelJob = useCallback(
    async (job: JobStatus) => {
      setCancellingIds((prev) => new Set(prev).add(job.job_id))
      try {
        const ok = await cancelBatchJob(job.job_id)
        if (ok) applyCancelledJob(job.job_id)
      } finally {
        setCancellingIds((prev) => {
          const next = new Set(prev)
          next.delete(job.job_id)
          return next
        })
      }
    },
    [applyCancelledJob]
  )

  const handleCancelAll = useCallback(async () => {
    if (!groupId) return
    const result = await cancelBatch(groupId)
    // The backend returns the post-cancel snapshot; merge it so every row flips
    // to "cancelled" now instead of on the next poll tick.
    if (result.jobs) {
      setStatus((prev) => {
        if (!prev) return prev
        const counts = { queued: 0, running: 0, done: 0, error: 0, cancelled: 0, pending: 0 }
        for (const j of result.jobs!) counts[j.state] = (counts[j.state] ?? 0) + 1
        return { ...prev, jobs: result.jobs!, counts }
      })
    }
  }, [groupId])

  // Re-run one failed/cancelled row. The backend marks it queued and rebuilds
  // the aggregate when it settles; flipping the local group state back to
  // "running" re-arms the poll, which picks the new row state up on its own.
  const handleRetryJob = useCallback(
    async (job: JobStatus) => {
      if (!groupId) return
      setStartError(null)
      setRetryingIds((prev) => new Set(prev).add(job.job_id))
      try {
        await retryBatchJob(groupId, job.job_id)
        setStatus((prev) => (prev ? { ...prev, state: 'running' } : prev))
      } catch (e) {
        setStartError(e instanceof Error ? e.message : String(e))
      } finally {
        setRetryingIds((prev) => {
          const next = new Set(prev)
          next.delete(job.job_id)
          return next
        })
      }
    },
    [groupId]
  )

  const handleDownloadCombined = useCallback(
    async (format: BatchDownloadFormat) => {
      if (!groupId) return
      setDownloadError(null)
      try {
        await downloadBatchReport(groupId, format)
      } catch (e) {
        setDownloadError(e instanceof Error ? e.message : String(e))
      }
    },
    [groupId]
  )

  const handleDownloadJob = useCallback(
    async (job: JobStatus, format: 'csv' | 'attributes_csv' | 'capabilities_csv') => {
      if (!groupId || !job.server_name) return
      setDownloadError(null)
      try {
        await downloadBatchServerReport(groupId, job.server_name, format)
      } catch (e) {
        setDownloadError(e instanceof Error ? e.message : String(e))
      }
    },
    [groupId]
  )

  const jobs: JobStatus[] = status?.jobs || []
  const settled = (counts?.done || 0) + (counts?.error || 0) + (counts?.cancelled || 0)
  const total = status?.total || jobs.length

  return (
    <div className="space-y-3">
      <BatchTargetRows kind={kind} rows={rows} onChange={setRows} disabled={isRunning} />

      <div className="flex items-center gap-3 flex-wrap">
        <label className="text-xs text-gray-700 flex items-center gap-1.5">
          Run at a time
          <select
            className="p-1.5 border border-gray-200 rounded text-xs focus:outline-none focus:ring-1 focus:ring-blue-300"
            value={concurrency}
            onChange={(e) => setConcurrency(Number(e.target.value))}
            disabled={isRunning}
          >
            {concurrencyOptions.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <span className="text-xs text-gray-400">
          {targets.length} {targets.length === 1 ? 'server' : 'servers'} ready
          {missingName.length > 0 && (
            <span className="text-red-600">
              {' '}
              · {missingName.length} missing a name
            </span>
          )}
          {kind === 'github' && concurrency > 5 && (
            <span className="text-amber-600">
              {' '}
              · repositories launch local subprocesses; 5 or fewer is easier on the machine
            </span>
          )}
        </span>

        <button
          type="button"
          onClick={handleStart}
          disabled={isStarting || isRunning || targets.length === 0}
          className="ml-auto px-3 py-1.5 bg-blue-600 text-white text-sm rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isStarting ? 'Starting…' : isRunning ? 'Running…' : `Inspect ${targets.length || ''} ${targets.length === 1 ? 'server' : 'servers'}`.trim()}
        </button>
      </div>

      {incomplete.length > 0 && (
        <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2">
          {incomplete.length === 1 ? 'One server has' : `${incomplete.length} servers have`} an
          authentication method selected but no credential yet — they will be inspected
          unauthenticated, which usually returns 401.
        </p>
      )}

      {startError && (
        <p className="text-xs text-red-700 bg-red-50 border border-red-200 rounded p-2">{startError}</p>
      )}

      {duplicates.length > 0 && (
        <p className="text-xs text-gray-600 bg-gray-50 border border-gray-200 rounded p-2">
          Skipped {duplicates.length} duplicate {duplicates.length === 1 ? 'entry' : 'entries'}:{' '}
          <span className="font-mono">{duplicates.join(', ')}</span>
        </p>
      )}

      {status && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-xs text-gray-600">
            <span>
              {settled}/{total} finished
            </span>
            {status.concurrency && <span className="text-gray-400">· {status.concurrency} at a time</span>}
            {counts && (
              <span className="text-gray-400">
                · {counts.running} running · {counts.queued} queued
                {counts.error > 0 && <span className="text-red-600"> · {counts.error} failed</span>}
                {counts.cancelled > 0 && <span> · {counts.cancelled} cancelled</span>}
              </span>
            )}
            {isRunning && (
              <button
                type="button"
                onClick={handleCancelAll}
                className="ml-auto px-2 py-1 text-xs rounded border border-gray-200 text-gray-700 hover:bg-gray-50"
              >
                Cancel all
              </button>
            )}
          </div>

          <div className="w-full bg-gray-100 rounded h-1.5 overflow-hidden">
            <div
              className="bg-blue-500 h-full transition-all"
              style={{ width: total > 0 ? `${(settled / total) * 100}%` : '0%' }}
            />
          </div>

          <BatchQueueTable
            jobs={jobs}
            selectedJobId={selectedJobId}
            onSelect={(job) => {
              const next = job.job_id === selectedJobId ? null : job
              setInternalSelectedJobId(next?.job_id ?? null)
              onSelectJob?.(next)
            }}
            onCancelJob={handleCancelJob}
            cancellingIds={cancellingIds}
            onRetryJob={handleRetryJob}
            retryingIds={retryingIds}
            onDownloadJob={groupId ? handleDownloadJob : undefined}
          />

          {status.state === 'done' && (
            <div className="border border-gray-200 rounded p-2 space-y-1.5 bg-gray-50/60">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs font-medium text-gray-700">Combined report:</span>
                {status.aggregated ? (
                  <>
                    {([
                      ['csv', 'Checklist CSV', 'Attribute checklist for all servers — one row set per server'],
                      ['capabilities_csv', 'Capabilities CSV', 'Every tool, resource and prompt across all servers — one row each'],
                      ['html', 'HTML', 'All servers in one HTML page'],
                      ['json', 'Raw JSON', 'Raw combined report data as JSON'],
                    ] as Array<[BatchDownloadFormat, string, string]>)
                      .filter(([format]) => status.aggregated?.[format])
                      .map(([format, label, title]) => (
                        <button
                          key={format}
                          type="button"
                          onClick={() => handleDownloadCombined(format)}
                          className="px-3 py-1 bg-blue-100 hover:bg-blue-200 text-blue-900 rounded text-xs font-medium transition-colors"
                          title={title}
                        >
                          {label}
                        </button>
                      ))}
                  </>
                ) : (
                  <span className="text-xs text-gray-500">
                    not written — no server in this run produced a report
                  </span>
                )}
              </div>

              {status.aggregate_covers && (
                <p className="text-xs text-gray-500">
                  Covers {status.aggregate_covers.reports} of {status.aggregate_covers.total}{' '}
                  {status.aggregate_covers.total === 1 ? 'server' : 'servers'}
                  {status.aggregate_covers.reports < status.aggregate_covers.total &&
                    ' — failed and cancelled rows contribute no data'}
                  . For one server on its own, select its row above.
                </p>
              )}

              {downloadError && (
                <p className="text-xs text-red-700">{downloadError}</p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
