interface ServerAttributesProps {
  attributes: any
}

export function ServerAttributes({ attributes }: ServerAttributesProps) {
  if (!attributes || typeof attributes !== 'object') {
    return null
  }

  const formatAttributeValue = (value: any): string => {
    if (typeof value === 'boolean') {
      return value ? 'Yes' : 'No'
    }
    if (typeof value === 'string') {
      return value
    }
    if (typeof value === 'object' && value !== null) {
      // For nested objects, find the true values
      const trueKeys = Object.entries(value)
        .filter(([_, v]) => v === true)
        .map(([k, _]) => k)
      
      if (trueKeys.length > 0) {
        return trueKeys.join(', ')
      }
      
      // If no true values, show all key-value pairs
      return Object.entries(value)
        .map(([k, v]) => `${k}: ${v}`)
        .join(', ')
    }
    return String(value)
  }

  const formatAttributeName = (name: string): string => {
    // Convert snake_case to Title Case
    return name
      .split('_')
      .map(word => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ')
  }

  return (
    <div className="space-y-3">
      {Object.entries(attributes).map(([key, value]) => {
        if (value === null || value === undefined) {
          return null
        }

        return (
          <div key={key} className="p-3 bg-gray-50 rounded text-xs border border-gray-200">
            <div className="font-medium text-gray-900 mb-2">
              {formatAttributeName(key)}
            </div>
            {typeof value === 'object' && !Array.isArray(value) ? (
              <div className="space-y-1 ml-2">
                {Object.entries(value).map(([subKey, subValue]) => (
                  <div key={subKey} className="flex items-start">
                    <span className="text-gray-600 min-w-[120px]">
                      {formatAttributeName(subKey)}:
                    </span>
                    <span className="text-gray-800 font-medium">
                      {formatAttributeValue(subValue)}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-gray-800 font-medium ml-2">
                {formatAttributeValue(value)}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

