/**
 * Backend-proxied OAuth flow for the single-server page.
 *
 * The flow itself lives in utils/oauthProxy.ts so a multi-server run can authorize
 * each of its endpoints separately; this hook only adds the React state the
 * single-server UI renders.
 */

import { useCallback, useState } from 'react'
import { runOAuthProxyFlow, type OAuthProxyToken } from '../utils/oauthProxy.js'

export type { OAuthProxyToken }

export interface OAuthProxyState {
  isAuthenticating: boolean
  error: string | null
  token: OAuthProxyToken | null
}

export function useOAuthProxy(endpointUrl: string) {
  const [state, setState] = useState<OAuthProxyState>({
    isAuthenticating: false,
    error: null,
    token: null,
  })

  const startOAuth = useCallback(async (): Promise<OAuthProxyToken | null> => {
    if (!endpointUrl.trim()) return null

    setState({ isAuthenticating: true, error: null, token: null })
    try {
      const token = await runOAuthProxyFlow(endpointUrl)
      setState({ isAuthenticating: false, error: null, token })
      return token
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setState({ isAuthenticating: false, error: message, token: null })
      return null
    }
  }, [endpointUrl])

  const reset = useCallback(() => {
    setState({ isAuthenticating: false, error: null, token: null })
  }, [])

  return { ...state, startOAuth, reset }
}
