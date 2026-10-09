import { useMemo } from 'react'

/**
 * Hook to auto-generate server name from URL
 */
export function useServerName(serverUrl: string): string {
  return useMemo(() => {
    if (!serverUrl) return ''
    try {
      const url = new URL(serverUrl)
      const hostname = url.hostname.replace('www.', '')
      const parts = hostname.split('.')
      return parts.length > 1 ? parts[0] : hostname
    } catch {
      // If URL parsing fails, try to extract from string
      const match = serverUrl.match(/https?:\/\/(?:www\.)?([^\/]+)/)
      if (match) {
        const hostname = match[1]
        const parts = hostname.split('.')
        return parts.length > 1 ? parts[0] : hostname
      }
      return 'mcp_server'
    }
  }, [serverUrl])
}

