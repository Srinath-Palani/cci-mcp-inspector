interface StatusBadgeProps {
  state: string
  isActive: boolean
}

export function StatusBadge({ state, isActive }: StatusBadgeProps) {
  const baseClasses = 'px-2 py-1 rounded-full text-xs font-medium'

  // If not active, show "Not Connected" regardless of state
  if (!isActive) {
    return <span className={`${baseClasses} bg-gray-100 text-gray-800`}>Not Connected</span>
  }

  switch (state) {
    case 'discovering':
      return <span className={`${baseClasses} bg-blue-100 text-blue-800`}>Discovering</span>
    case 'authenticating':
      return <span className={`${baseClasses} bg-purple-100 text-purple-800`}>Authenticating</span>
    case 'connecting':
      return <span className={`${baseClasses} bg-yellow-100 text-yellow-800`}>Connecting</span>
    case 'loading':
      return <span className={`${baseClasses} bg-orange-100 text-orange-800`}>Loading</span>
    case 'ready':
      return <span className={`${baseClasses} bg-green-100 text-green-800`}>Connected</span>
    case 'failed':
      return <span className={`${baseClasses} bg-red-100 text-red-800`}>Failed</span>
    default:
      return <span className={`${baseClasses} bg-gray-100 text-gray-800`}>Not Connected</span>
  }
}

