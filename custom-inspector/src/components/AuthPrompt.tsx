interface AuthPromptProps {
  authUrl: string
}

export function AuthPrompt({ authUrl }: AuthPromptProps) {
  return (
    <div className="p-3 bg-orange-50 border border-orange-200 rounded">
      <p className="text-xs mb-2">Authentication required. Please click the link below:</p>
      <a
        href={authUrl}
        target="_blank"
        rel="noopener noreferrer"
        className="text-xs text-orange-700 hover:text-orange-800 underline"
      >
        Authenticate in new window
      </a>
    </div>
  )
}

