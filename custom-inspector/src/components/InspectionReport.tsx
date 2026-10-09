import type { InspectResponse } from '../utils/apiClient.js'
import { ServerAttributes } from './ServerAttributes.js'
import { ErrorDisplay } from './ErrorDisplay.js'

interface InspectionReportProps {
  inspectionResult: InspectResponse
}

export function InspectionReport({ inspectionResult }: InspectionReportProps) {
  if (inspectionResult.success && inspectionResult.report_data) {
    const data = inspectionResult.report_data

    return (
      <div className="space-y-4">
        {/* Summary */}
        <div className="p-3 bg-green-50 border border-green-200 rounded">
          <div className="text-xs font-medium text-green-900 mb-2">✅ Inspection Completed Successfully</div>
          <div className="text-xs text-green-800 space-y-1">
            <div><strong>Server:</strong> {data.server_name}</div>
            <div><strong>Connection Type:</strong> {data.connection_type}</div>
            <div><strong>Timestamp:</strong> {data.discovery_timestamp}</div>
            <div className="mt-2">
              <strong>Statistics:</strong> {data.statistics.total_tools} tools,{' '}
              {data.statistics.total_resources} resources,{' '}
              {data.statistics.total_prompts} prompts
            </div>
          </div>
        </div>

        {/* Tools from Inspection */}
        {data.tools && data.tools.length > 0 && (
          <div>
            <h4 className="text-xs font-medium text-gray-700 mb-2">
              Tools ({data.tools.length})
            </h4>
            <div className="space-y-2 max-h-60 overflow-y-auto">
              {data.tools.map((tool: any, idx: number) => (
                <div key={idx} className="p-3 bg-gray-50 rounded text-xs border border-gray-200">
                  <div className="font-medium mb-1">{tool.name}</div>
                  {tool.description && (
                    <div className="text-gray-600 mb-2">{tool.description}</div>
                  )}
                  {tool.inputSchema && (
                    <details className="mt-2">
                      <summary className="cursor-pointer text-gray-700 font-medium">Input Schema</summary>
                      <pre className="mt-2 p-2 bg-white rounded text-xs overflow-x-auto">
                        {JSON.stringify(tool.inputSchema, null, 2)}
                      </pre>
                    </details>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Resources from Inspection */}
        {data.resources && data.resources.length > 0 && (
          <div>
            <h4 className="text-xs font-medium text-gray-700 mb-2">
              Resources ({data.resources.length})
            </h4>
            <div className="space-y-2 max-h-60 overflow-y-auto">
              {data.resources.map((resource: any, idx: number) => (
                <div key={idx} className="p-3 bg-gray-50 rounded text-xs border border-gray-200">
                  <div className="font-medium mb-1">{resource.name || resource.uri}</div>
                  {resource.description && (
                    <div className="text-gray-600 mb-2">{resource.description}</div>
                  )}
                  {resource.uri && (
                    <div className="text-gray-500 font-mono text-xs">{resource.uri}</div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Prompts from Inspection */}
        {data.prompts && data.prompts.length > 0 && (
          <div>
            <h4 className="text-xs font-medium text-gray-700 mb-2">
              Prompts ({data.prompts.length})
            </h4>
            <div className="space-y-2 max-h-60 overflow-y-auto">
              {data.prompts.map((prompt: any, idx: number) => (
                <div key={idx} className="p-3 bg-gray-50 rounded text-xs border border-gray-200">
                  <div className="font-medium mb-1">{prompt.name}</div>
                  {prompt.description && (
                    <div className="text-gray-600">{prompt.description}</div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Server Attributes */}
        {data.server_attributes && (
          <div>
            <h4 className="text-xs font-medium text-gray-700 mb-2">Server Attributes</h4>
            <ServerAttributes attributes={data.server_attributes} />
          </div>
        )}
      </div>
    )
  }

  return inspectionResult.error
    ? <ErrorDisplay error={inspectionResult.error} />
    : <div className="p-3 bg-red-50 border border-red-200 rounded text-xs text-red-900">Inspection failed with no error details.</div>
}

