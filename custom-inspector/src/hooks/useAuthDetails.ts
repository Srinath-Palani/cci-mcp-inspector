import { useMemo } from 'react'

/**
 * Hash a string (matches browser-provider implementation)
 */
function hashString(str: string): string {
  let hash = 0
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i)
    hash = (hash << 5) - hash + char
    hash = hash & hash // Convert to 32-bit integer
  }
  return Math.abs(hash).toString(16)
}

export interface AuthDetails {
  authenticated: boolean
  has_tokens: boolean
  auth_url?: string
  tokens?: Record<string, unknown>
}

/**
 * Hook to extract authentication details from localStorage
 */
export function useAuthDetails(
  serverUrl: string,
  isActive: boolean,
  state: string,
  authUrl?: string
): AuthDetails {
  return useMemo(() => {
    if (!serverUrl || !isActive) {
      return { authenticated: false, has_tokens: false, tokens: undefined }
    }

    // Check for tokens in localStorage
    // The storage key format is: mcp:auth_<hash>_tokens
    const storageKeyPrefix = 'mcp:auth'
    const serverUrlHash = hashString(serverUrl)
    const tokenKey = `${storageKeyPrefix}_${serverUrlHash}_tokens`
    const tokensJson = localStorage.getItem(tokenKey)
    const hasTokens = !!tokensJson

    let tokens: Record<string, unknown> | undefined = undefined
    if (tokensJson) {
      try {
        tokens = JSON.parse(tokensJson) as Record<string, unknown>
      } catch (e) {
        console.warn('Failed to parse OAuth tokens:', e)
      }
    }

    // Get client information to extract client_id
    const clientInfoKey = `${storageKeyPrefix}_${serverUrlHash}_client_info`
    const clientInfoJson = localStorage.getItem(clientInfoKey)
    if (clientInfoJson) {
      try {
        const clientInfo = JSON.parse(clientInfoJson) as Record<string, unknown>
        // Add client_id to tokens if it exists
        if (clientInfo.client_id && tokens) {
          tokens.client_id = clientInfo.client_id
        } else if (clientInfo.client_id && !tokens) {
          // Create tokens object with client_id if tokens don't exist yet
          tokens = { client_id: clientInfo.client_id }
        }
      } catch (e) {
        console.warn('Failed to parse client information:', e)
      }
    }

    return {
      authenticated: state === 'ready' && hasTokens,
      has_tokens: hasTokens,
      auth_url: authUrl,
      tokens,
    }
  }, [serverUrl, isActive, state, authUrl])
}

