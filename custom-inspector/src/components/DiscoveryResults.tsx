interface DiscoveryResultsProps {
  tools?: any[]
  resources?: any[]
  prompts?: any[]
}

export function DiscoveryResults({ tools = [], resources = [], prompts = [] }: DiscoveryResultsProps) {
  if (tools.length === 0 && resources.length === 0 && prompts.length === 0) {
    return null
  }

  return (
    <div className="border-t border-gray-200 pt-4 mt-4">
      <h3 className="text-sm font-semibold mb-3">Discovery Results</h3>

      {tools.length > 0 && (
        <div className="mb-4">
          <h4 className="text-xs font-medium text-gray-700 mb-2">Tools ({tools.length})</h4>
          <div className="space-y-2 max-h-40 overflow-y-auto">
            {tools.map((tool, idx) => (
              <div key={idx} className="p-2 bg-gray-50 rounded text-xs">
                <div className="font-medium">{tool.name}</div>
                {tool.description && (
                  <div className="text-gray-600 mt-1">{tool.description}</div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {resources.length > 0 && (
        <div className="mb-4">
          <h4 className="text-xs font-medium text-gray-700 mb-2">Resources ({resources.length})</h4>
          <div className="space-y-2 max-h-40 overflow-y-auto">
            {resources.map((resource, idx) => (
              <div key={idx} className="p-2 bg-gray-50 rounded text-xs">
                <div className="font-medium">{resource.name || resource.uri}</div>
                {resource.description && (
                  <div className="text-gray-600 mt-1">{resource.description}</div>
                )}
                {resource.uri && (
                  <div className="text-gray-500 mt-1 font-mono text-xs">{resource.uri}</div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {prompts.length > 0 && (
        <div className="mb-4">
          <h4 className="text-xs font-medium text-gray-700 mb-2">Prompts ({prompts.length})</h4>
          <div className="space-y-2 max-h-40 overflow-y-auto">
            {prompts.map((prompt, idx) => (
              <div key={idx} className="p-2 bg-gray-50 rounded text-xs">
                <div className="font-medium">{prompt.name}</div>
                {prompt.description && (
                  <div className="text-gray-600 mt-1">{prompt.description}</div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

