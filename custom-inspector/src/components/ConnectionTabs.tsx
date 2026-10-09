export type ConnectionTab = 'remote' | 'stdio'

interface ConnectionTabsProps {
  activeTab: ConnectionTab
  onTabChange: (tab: ConnectionTab) => void
}

export function ConnectionTabs({ activeTab, onTabChange }: ConnectionTabsProps) {
  return (
    <div className="flex border-b border-gray-200 mb-4">
      <button
        type="button"
        onClick={() => onTabChange('remote')}
        className={`px-4 py-2 text-sm font-medium transition-colors ${
          activeTab === 'remote'
            ? 'border-b-2 border-blue-500 text-blue-600'
            : 'text-gray-500 hover:text-gray-700'
        }`}
      >
        Remote
      </button>
      <button
        type="button"
        onClick={() => onTabChange('stdio')}
        className={`px-4 py-2 text-sm font-medium transition-colors ${
          activeTab === 'stdio'
            ? 'border-b-2 border-blue-500 text-blue-600'
            : 'text-gray-500 hover:text-gray-700'
        }`}
      >
        Stdio
      </button>
    </div>
  )
}

