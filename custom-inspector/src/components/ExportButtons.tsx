import { downloadServerReport, downloadBatchServerReport, type ReportFormat } from '../utils/apiClient.js'

interface ExportButtonsProps {
  serverName: string
  reportPaths?: Partial<Record<ReportFormat, string>>
  /**
   * Set for reports from a batch run: downloads must go through the batch
   * per-server endpoint — /api/reports/{name}/{format} only knows single
   * inspections and 404s for batch rows.
   */
  batchGroupId?: string | null
}

// Three distinct CSVs, so none may be labelled a bare "CSV": the attribute
// key-value list, the tool capability list, and the Yes/No checklist.
const reportLabels: Record<ReportFormat, string> = {
  attributes_csv: 'Attributes CSV',
  capabilities_csv: 'Capabilities CSV',
  csv: 'Checklist CSV',
  json: 'JSON',
  txt: 'Raw Text',
  markdown: 'Markdown',
  html: 'HTML',
}

const reportTitles: Record<ReportFormat, string> = {
  attributes_csv: 'Attributes — one row per attribute with its value',
  capabilities_csv: 'Capabilities — one row per tool',
  csv: 'Attribute checklist — one row per checked attribute option',
  json: 'Full inspection report as JSON',
  txt: 'Raw output from every inspection phase',
  markdown: 'Attribute checklist as Markdown',
  html: 'Attribute report as a standalone HTML page',
}

// Display order requested by the user: Attributes CSV first, HTML last.
const formatOrder: ReportFormat[] = [
  'attributes_csv', 'capabilities_csv', 'csv', 'json', 'txt', 'markdown', 'html',
]

export function ExportButtons({ serverName, reportPaths, batchGroupId }: ExportButtonsProps) {
  const handleDownload = async (format: ReportFormat) => {
    if (!reportPaths || !reportPaths[format]) {
      console.warn(`Report format ${format} not available`)
      return
    }

    try {
      if (batchGroupId) {
        await downloadBatchServerReport(batchGroupId, serverName, format)
      } else {
        await downloadServerReport(serverName, format)
      }
    } catch (error) {
      console.error(`Failed to download ${format} report:`, error)
      alert(`Failed to download ${reportLabels[format]} report: ${error instanceof Error ? error.message : String(error)}`)
    }
  }

  if (!reportPaths || Object.keys(reportPaths).length === 0) {
    return (
      <div className="flex gap-2 items-center">
        <span className="text-xs font-medium text-gray-500">No reports available</span>
      </div>
    )
  }

  const availableFormats = formatOrder.filter((format) => reportPaths[format])

  return (
    <div className="flex gap-2 items-center flex-wrap">
      <span className="text-xs font-medium text-gray-700">Download Reports:</span>
      {availableFormats.map((format) => (
        <button
          key={format}
          className="px-3 py-1 bg-blue-100 hover:bg-blue-200 text-blue-900 rounded text-xs font-medium transition-colors"
          onClick={() => handleDownload(format)}
          title={reportTitles[format]}
        >
          {reportLabels[format]}
        </button>
      ))}
    </div>
  )
}

