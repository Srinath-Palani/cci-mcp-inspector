/**
 * Report preview for one row of a multi-server run.
 *
 * Shown in place below the queue rather than in a modal, so the run stays visible
 * while a finished report is read. Everything here is already in the poll payload —
 * opening a preview makes no extra request, and closing it loses nothing.
 *
 * A failed row previews too: its structured error is the report.
 */

import { useState } from 'react'
import type { JobStatus } from '../utils/apiClient.js'
import { ServerAttributes } from './ServerAttributes.js'
import { ErrorDisplay } from './ErrorDisplay.js'
import { ExportButtons } from './ExportButtons.js'
import { ProtocolVersionMatrix } from './ProtocolVersionMatrix.js'

type PreviewTab = 'overview' | 'protocol' | 'capabilities'

interface ReportPreviewProps {
  job: JobStatus
  onClose: () => void
  batchGroupId?: string | null
}

export function ReportPreview({ job, onClose, batchGroupId }: ReportPreviewProps) {
  const [tab, setTab] = useState<PreviewTab>('overview')

  const report = job.result?.report_data
  const attributes = report?.server_attributes
  const protocolVersion = attributes?.protocol_version
  const stats = report?.statistics

  const tabs: Array<{ id: PreviewTab; label: string; enabled: boolean }> = [
    { id: 'overview', label: 'Overview', enabled: true },
    { id: 'protocol', label: 'Protocol versions', enabled: Boolean(protocolVersion) },
    { id: 'capabilities', label: 'Capabilities', enabled: Boolean(report) },
  ]

  return (
    <div className="border border-blue-200 rounded bg-white">
      <div className="flex items-center justify-between px-3 py-2 border-b border-gray-200 bg-blue-50/50">
        <div className="min-w-0">
          <div className="text-sm font-medium text-gray-900 truncate">
            <span className="font-mono text-gray-400 mr-2">{job.identity}</span>
            {job.server_name}
          </div>
          {job.target && (
            <div className="text-xs text-gray-500 truncate" title={job.target}>
              {job.target}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="text-gray-400 hover:text-gray-700 text-sm px-1"
          title="Close preview"
        >
          ✕
        </button>
      </div>

      {job.state === 'error' ? (
        <div className="p-3">
          <ErrorDisplay error={job.error || 'Inspection failed'} errorDetails={job.error_details} />
        </div>
      ) : (
        <>
          <div className="flex border-b border-gray-200 px-2">
            {tabs.map((t) => (
              <button
                key={t.id}
                type="button"
                disabled={!t.enabled}
                onClick={() => setTab(t.id)}
                className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                  tab === t.id
                    ? 'border-b-2 border-blue-500 text-blue-600'
                    : t.enabled
                      ? 'text-gray-500 hover:text-gray-700'
                      : 'text-gray-300 cursor-not-allowed'
                }`}
                title={t.enabled ? undefined : 'Not available for this report'}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="p-3 space-y-3">
            {tab === 'overview' && (
              <>
                {stats && (
                  <div className="grid grid-cols-3 gap-2 text-xs">
                    <div className="border border-gray-200 rounded p-2">
                      <div className="text-gray-500">Tools</div>
                      <div className="text-lg text-gray-900">{stats.total_tools}</div>
                    </div>
                    <div className="border border-gray-200 rounded p-2">
                      <div className="text-gray-500">Resources</div>
                      <div className="text-lg text-gray-900">{stats.total_resources}</div>
                    </div>
                    <div className="border border-gray-200 rounded p-2">
                      <div className="text-gray-500">Prompts</div>
                      <div className="text-lg text-gray-900">{stats.total_prompts}</div>
                    </div>
                  </div>
                )}
                {attributes ? (
                  <ServerAttributes attributes={attributes} />
                ) : (
                  <p className="text-xs text-gray-500">
                    This run produced no attribute checklist — the inspection finished but
                    attribute extraction did not.
                  </p>
                )}
                {/*
                  serverName must be the name that created the output directory,
                  which is server_config["name"] — the value job.server_name is
                  renamed to once the entry resolves. report_data.server_name is the
                  server's own self-reported name and can differ (notably for GitHub
                  rows, where the config name comes from the repo), which would make
                  every download 404.
                */}
                 <ExportButtons
                  serverName={job.server_name}
                  reportPaths={job.result?.report_paths}
                  batchGroupId={batchGroupId}
                />
              </>
            )}

            {tab === 'protocol' && <ProtocolVersionMatrix protocolVersion={protocolVersion} />}

            {tab === 'capabilities' && report && (
              <div className="space-y-3 text-xs">
                {(['tools', 'resources', 'prompts'] as const).map((key) => {
                  const items = (report[key] || []) as Array<{ name?: string; description?: string }>
                  return (
                    <div key={key}>
                      <div className="font-medium text-gray-700 mb-1 capitalize">
                        {key} ({items.length})
                      </div>
                      {items.length === 0 ? (
                        <p className="text-gray-400">None exposed</p>
                      ) : (
                        <ul className="space-y-0.5 max-h-48 overflow-y-auto">
                          {items.map((item, index) => (
                            <li key={`${item.name}-${index}`} className="text-gray-600">
                              <span className="font-mono text-gray-900">{item.name || '(unnamed)'}</span>
                              {item.description && ` — ${item.description}`}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
