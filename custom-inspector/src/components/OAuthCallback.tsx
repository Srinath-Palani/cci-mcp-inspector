import { useEffect, useState } from 'react'
import { handleOAuthCallback } from '../utils/oauthHandler.js'

export function OAuthCallback() {
  const [status, setStatus] = useState<'processing' | 'done' | 'error'>('processing')
  const [errorMessage, setErrorMessage] = useState<string>('')

  useEffect(() => {
    const queryParams = new URLSearchParams(window.location.search)
    const state = queryParams.get('state')
    const stateKey = state ? `mcp:auth:state_${state}` : null

    // Pre-check: if the OAuth state is not in localStorage, use-mcp will replace
    // document.body.innerHTML with raw HTML before we can show our own error UI.
    // Detect it here first so we can render a friendly message instead.
    if (!state || (stateKey && !localStorage.getItem(stateKey))) {
      setStatus('error')
      setErrorMessage(
        'The authentication session has expired or was already used. Please close this window and click Connect again in the main window to start a fresh login.'
      )
      return
    }

    // State is present — proceed. onMcpAuthorization handles success (closes the window)
    // and other errors (replaces body HTML). We just trigger it.
    handleOAuthCallback().catch(() => {
      // Safety net: onMcpAuthorization normally handles errors internally,
      // but catch here in case of unexpected rejections.
      setStatus('error')
      setErrorMessage('Authentication failed. Please close this window and try again.')
    })

    // Fallback close: in case onMcpAuthorization doesn't close the window automatically.
    const timer = setTimeout(() => {
      if (window.opener) window.close()
    }, 1500)

    return () => clearTimeout(timer)
  }, [])

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center p-4">
      <div className="text-center max-w-md">
        {status === 'processing' && (
          <>
            <h1 className="text-2xl font-bold text-gray-900 mb-4">Authenticating...</h1>
            <p className="text-gray-600 mb-2">Completing your authentication.</p>
            <p className="text-sm text-gray-500">This window will close automatically.</p>
          </>
        )}
        {status === 'done' && (
          <>
            <h1 className="text-2xl font-bold text-gray-900 mb-4">✅ Authenticated</h1>
            <p className="text-gray-600 mb-2">You can close this window.</p>
          </>
        )}
        {status === 'error' && (
          <>
            <h1 className="text-2xl font-bold text-red-700 mb-4">Authentication Error</h1>
            {errorMessage ? (
              <p className="text-gray-700 mb-6 text-sm leading-relaxed">{errorMessage}</p>
            ) : (
              <p className="text-gray-600 mb-6">Something went wrong. Please try connecting again.</p>
            )}
            <button
              onClick={() => window.close()}
              className="px-4 py-2 bg-gray-800 text-white rounded hover:bg-gray-700 text-sm"
            >
              Close this window
            </button>
          </>
        )}
      </div>
    </div>
  )
}
