/**
 * Extracted OAuth handling logic
 * This module handles OAuth callback processing
 */

import { onMcpAuthorization } from 'use-mcp'

/**
 * Handles OAuth callback when redirected from OAuth provider
 * Call this in your OAuth callback component/page
 */
export async function handleOAuthCallback() {
  await onMcpAuthorization()
}

