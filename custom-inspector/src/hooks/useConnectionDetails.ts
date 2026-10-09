import { useMemo } from 'react'
import type { ConnectionDetails } from '../utils/apiClient.js'
import type { TransportType } from '../utils/mcpConnection.js'
import type { AuthDetails } from './useAuthDetails.js'

/**
 * Hook to generate connection details for API
 */
export function useConnectionDetails(
  serverUrl: string,
  transportType: TransportType,
  actualTransportType: 'http' | 'sse' | undefined,
  authDetails: AuthDetails,
  serverName: string,
  serverDescription?: string,
  githubRepoLink?: string,
  distributionType?: string,
  bearerToken?: string
): ConnectionDetails {
  return useMemo(() => {
    if (!serverUrl) {
      return {
        name: '',
        description: '',
        connection_type: transportType,
        endpoint_url: '',
      }
    }

    // If a static bearer token is provided, use it as access_token (overrides localStorage OAuth tokens)
    const effectiveTokens = bearerToken?.trim()
      ? { ...(authDetails.tokens || {}), access_token: bearerToken.trim() }
      : authDetails.tokens
    const effectiveHasTokens = bearerToken?.trim() ? true : authDetails.has_tokens

    const authObject: ConnectionDetails['authentication'] = {
      authenticated: authDetails.authenticated,
      has_tokens: effectiveHasTokens,
      ...(authDetails.auth_url && { auth_url: authDetails.auth_url }),
      ...(effectiveTokens && { tokens: effectiveTokens }),
    }

    // Use actual transport type if available (when auto was selected), otherwise use the selected type
    const displayTransportType = actualTransportType || transportType

    return {
      name: serverName,
      description: serverDescription || `MCP Server: ${serverName}`,
      connection_type: displayTransportType,
      endpoint_url: serverUrl,
      authentication: authObject,
      ...(githubRepoLink?.trim() && { github_repo_link: githubRepoLink.trim() }),
      ...(distributionType?.trim() && { distribution_type: distributionType.trim() }),
    }
  }, [serverUrl, transportType, actualTransportType, authDetails, serverName, serverDescription, githubRepoLink, distributionType, bearerToken])
}

