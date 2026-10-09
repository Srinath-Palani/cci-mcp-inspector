import { useState, useCallback, useRef } from 'react'
import {
  generateStdioConfig,
  testStdioConnection,
  runInspection,
  cancelInspection,
  InspectionError,
  type GenerateStdioConfigResponse,
  type InspectResponse,
  type JobStatus,
  type ApiErrorDetails,
} from '../utils/apiClient.js'
import { StatusBadge } from './StatusBadge.js'
import { RunInspectionButton } from './RunInspectionButton.js'
import { ExportButtons } from './ExportButtons.js'
import { InspectionReport } from './InspectionReport.js'
import { ErrorDisplay } from './ErrorDisplay.js'
import { InspectionProgress } from './InspectionProgress.js'
import { ConfigImport, type ImportedServer } from './ConfigImport.js'
import {
  EnvVarRows,
  entriesFromDetected,
  entriesFromEnvDict,
  entriesToEnv,
  validateEnvEntries,
  type EnvVarEntry,
} from './EnvVarRows.js'

interface StdioConnectionProps {
  serverName: string
  serverDescription: string
  onServerNameChange: (name: string) => void
  onServerDescriptionChange: (description: string) => void
}

export function StdioConnection({
  serverName,
  serverDescription,
  onServerNameChange,
  onServerDescriptionChange,
}: StdioConnectionProps) {
  const [repositoryUrl, setRepositoryUrl] = useState(() => {
    return sessionStorage.getItem('stdioRepositoryUrl') || ''
  })
  const [isGenerating, setIsGenerating] = useState(false)
  const [generatedConfig, setGeneratedConfig] = useState<GenerateStdioConfigResponse['config'] | null>(null)
  const [envVars, setEnvVars] = useState<string[]>([])
  const [envEntries, setEnvEntries] = useState<EnvVarEntry[]>([])
  const [connectionState, setConnectionState] = useState<'idle' | 'connecting' | 'connected' | 'failed'>('idle')
  const [connectionError, setConnectionError] = useState<string | null>(null)
  const [distributionType, setDistributionType] = useState(() => {
    return sessionStorage.getItem('stdioDistributionType') || ''
  })
  const [isRunningInspection, setIsRunningInspection] = useState(false)
  const [inspectionResult, setInspectionResult] = useState<InspectResponse | null>(null)
  const [inspectionError, setInspectionError] = useState<string | null>(null)
  const [inspectionErrorDetails, setInspectionErrorDetails] = useState<ApiErrorDetails | null>(null)
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null)
  const [isCancelling, setIsCancelling] = useState(false)
  const activeJobId = useRef<string | null>(null)

  const envValidation = validateEnvEntries(envEntries)

  // Handle generate config
  const handleGenerate = useCallback(async () => {
    if (!repositoryUrl.trim()) {
      setConnectionError('Repository URL is required')
      return
    }

    setIsGenerating(true)
    setConnectionError(null)
    setGeneratedConfig(null)
    setEnvVars([])
    setEnvEntries([])
    setConnectionState('idle')

    try {
      const result = await generateStdioConfig({
        repository_url: repositoryUrl.trim(),
        server_name: serverName.trim() || undefined,
      })

      if (result.success && result.config) {
        setGeneratedConfig(result.config)
        setEnvVars(result.env_vars || [])

        // Update server name if auto-generated
        if (result.config.name && !serverName.trim()) {
          onServerNameChange(result.config.name)
        }

        // Pre-fill one row per detected variable — keys locked, values empty
        // and marked required (no more 'your_value_here' placeholders).
        if (result.env_vars && result.env_vars.length > 0) {
          setEnvEntries(entriesFromDetected(result.env_vars))
        }
      } else {
        setConnectionError(result.error || 'Failed to generate configuration')
        setConnectionState('failed')
      }
    } catch (error) {
      setConnectionError(error instanceof Error ? error.message : 'Unknown error occurred')
      setConnectionState('failed')
    } finally {
      setIsGenerating(false)
    }
  }, [repositoryUrl, serverName, onServerNameChange])

  // Populate the form from an imported config file (single-server case)
  const handleImportedServer = useCallback((server: ImportedServer) => {
    onServerNameChange(server.name)
    onServerDescriptionChange(server.description || `MCP Server: ${server.name}`)
    if (server.repository) {
      setRepositoryUrl(server.repository)
      sessionStorage.setItem('stdioRepositoryUrl', server.repository)
    }
    if (server.distribution_type) {
      setDistributionType(server.distribution_type)
      sessionStorage.setItem('stdioDistributionType', server.distribution_type)
    }
    if (server.command) {
      setGeneratedConfig({
        name: server.name,
        description: server.description || `MCP Server: ${server.name}`,
        connection_type: 'stdio',
        repository: server.repository,
        command: server.command,
        args: server.args || [],
      })
    }
    if (server.env) {
      setEnvEntries(entriesFromEnvDict(server.env))
      setEnvVars(Object.keys(server.env))
    }
    setConnectionState('idle')
    setConnectionError(null)
    setInspectionResult(null)
    setInspectionError(null)
    setInspectionErrorDetails(null)
  }, [onServerNameChange, onServerDescriptionChange])

  // Handle connect (test stdio connection)
  const handleConnect = useCallback(async () => {
    if (!generatedConfig || !serverName.trim() || !serverDescription.trim()) {
      setConnectionError('Please generate config and fill in server name and description')
      return
    }

    setConnectionState('connecting')
    setConnectionError(null)

    try {
      const env = entriesToEnv(envEntries)
      const testResult = await testStdioConnection({
        name: serverName,
        command: generatedConfig.command,
        args: generatedConfig.args,
        env,
      })

      if (testResult.success && testResult.connected) {
        setConnectionState('connected')
      } else {
        setConnectionError(testResult.error || 'Connection test failed')
        setConnectionState('failed')
      }
    } catch (error) {
      setConnectionError(error instanceof Error ? error.message : 'Connection failed')
      setConnectionState('failed')
    }
  }, [generatedConfig, serverName, serverDescription, envEntries])

  // Handle disconnect — cancels the running inspection job (if any) and
  // returns the UI to idle
  const handleDisconnect = useCallback(async () => {
    if (activeJobId.current) {
      setIsCancelling(true)
      await cancelInspection(activeJobId.current)
      activeJobId.current = null
      setIsCancelling(false)
    }
    setConnectionState('idle')
    setConnectionError(null)
    setInspectionResult(null)
    setInspectionError(null)
    setInspectionErrorDetails(null)
    setJobStatus(null)
    setIsRunningInspection(false)
  }, [])

  // Handle run inspection (job-based: start → poll → result; cancellable)
  const handleRunInspection = useCallback(async () => {
    if (!generatedConfig || !serverName.trim() || !serverDescription.trim() || connectionState !== 'connected') {
      return
    }

    if (!generatedConfig.command || !generatedConfig.command.trim()) {
      setInspectionError('Command is missing from generated configuration. Please regenerate the config.')
      return
    }
    if (!generatedConfig.args || !Array.isArray(generatedConfig.args)) {
      setInspectionError('Args are missing or invalid in generated configuration. Please regenerate the config.')
      return
    }

    // Block on invalid env values before hitting the backend
    const validation = validateEnvEntries(envEntries)
    if (!validation.valid) {
      const parts: string[] = []
      if (validation.missing.length) parts.push(`missing values: ${validation.missing.join(', ')}`)
      if (validation.placeholders.length) parts.push(`placeholder values: ${validation.placeholders.join(', ')}`)
      setInspectionError(`Environment variables need attention — ${parts.join('; ')}`)
      return
    }

    setIsRunningInspection(true)
    setInspectionError(null)
    setInspectionErrorDetails(null)
    setInspectionResult(null)
    setJobStatus(null)

    try {
      const repoUrl = generatedConfig.repository || repositoryUrl.trim() || undefined
      const inspectionRequest = {
        name: serverName,
        description: serverDescription,
        connection_type: 'stdio',
        command: generatedConfig.command.trim(),
        args: generatedConfig.args,
        env: entriesToEnv(envEntries),
        required_env_vars: envVars.length > 0 ? envVars : undefined,
        repository: repoUrl,
        distribution_type: distributionType.trim() || undefined,
      }

      if (!repoUrl) {
        console.warn('⚠️  No repository URL provided - documentation analysis may be limited')
      }

      const result = await runInspection(inspectionRequest, (status) => {
        activeJobId.current = status.job_id
        setJobStatus(status)
      })

      setInspectionResult(result)
    } catch (error) {
      if (error instanceof InspectionError) {
        if (error.message !== 'Inspection cancelled') {
          setInspectionError(error.message)
          setInspectionErrorDetails(error.errorDetails ?? null)
        }
      } else {
        setInspectionError(error instanceof Error ? error.message : 'Unknown error occurred')
      }
    } finally {
      activeJobId.current = null
      setIsRunningInspection(false)
      setJobStatus(null)
    }
  }, [generatedConfig, serverName, serverDescription, envEntries, envVars, connectionState, repositoryUrl, distributionType])


  return (
    <div className="space-y-3">
      {/* Import from client config */}
      <ConfigImport onSingleServer={handleImportedServer} />

      {/* Server Name - Required */}
      <div>
        <label className="block text-xs font-medium text-gray-700 mb-1">
          Server Name <span className="text-red-500">*</span>
        </label>
        <input
          type="text"
          className="w-full p-2 border border-gray-200 rounded text-sm placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300"
          placeholder="Enter server name (e.g., GitHub MCP Server)"
          value={serverName}
          onChange={(e) => {
            const newValue = e.target.value
            onServerNameChange(newValue)
            sessionStorage.setItem('mcpServerName', newValue)
          }}
          disabled={connectionState === 'connected'}
          required
        />
      </div>

      {/* Server Description - Required */}
      <div>
        <label className="block text-xs font-medium text-gray-700 mb-1">
          Description <span className="text-red-500">*</span>
        </label>
        <textarea
          className="w-full p-2 border border-gray-200 rounded text-sm placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300 resize-none"
          placeholder="Enter server description"
          rows={2}
          value={serverDescription}
          onChange={(e) => {
            const newValue = e.target.value
            onServerDescriptionChange(newValue)
            sessionStorage.setItem('mcpServerDescription', newValue)
          }}
          disabled={connectionState === 'connected'}
          required
        />
      </div>

      {/* Distribution Type - Optional */}
      <div>
        <label className="block text-xs font-medium text-gray-700 mb-1">
          Distribution Type <span className="text-gray-400 font-normal">(optional)</span>
        </label>
        <select
          className="w-full p-2 border border-gray-200 rounded text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"
          value={distributionType}
          onChange={(e) => {
            const newValue = e.target.value
            setDistributionType(newValue)
            sessionStorage.setItem('stdioDistributionType', newValue)
          }}
          disabled={connectionState === 'connected'}
        >
          <option value="">Select distribution type</option>
          <option value="official">Official</option>
          <option value="community">Community</option>
        </select>
      </div>

      {/* Repository URL Input */}
      <div>
        <label className="block text-xs font-medium text-gray-700 mb-1">
          GitHub Repository URL <span className="text-red-500">*</span>
        </label>
        <div className="flex gap-2">
          <input
            type="text"
            className="flex-1 p-2 border border-gray-200 rounded text-sm placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300"
            placeholder="https://github.com/owner/repo"
            value={repositoryUrl}
            onChange={(e) => {
              const newValue = e.target.value
              setRepositoryUrl(newValue)
              sessionStorage.setItem('stdioRepositoryUrl', newValue)
            }}
            disabled={isGenerating || connectionState === 'connected'}
          />
          <button
            type="button"
            onClick={handleGenerate}
            disabled={isGenerating || !repositoryUrl.trim() || connectionState === 'connected'}
            className="px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
          >
            {isGenerating ? 'Generating...' : 'Generate'}
          </button>
        </div>
      </div>

      {/* Generated Config Display (Read-only) */}
      {generatedConfig && (
        <div>
          <label className="block text-xs font-medium text-gray-700 mb-1">
            Generated Configuration
          </label>
          {generatedConfig.repository && (
            <div className="mb-2 p-2 bg-blue-50 border border-blue-200 rounded text-xs">
              <span className="font-medium text-blue-900">Repository: </span>
              <a
                href={generatedConfig.repository}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-600 hover:underline break-all"
              >
                {generatedConfig.repository}
              </a>
            </div>
          )}
          <textarea
            readOnly
            value={JSON.stringify(generatedConfig, null, 2)}
            className="w-full h-48 p-3 border border-gray-200 rounded text-sm font-mono bg-gray-50 resize-none focus:outline-none"
          />
        </div>
      )}

      {/* Environment Variables (per-variable rows with validation) */}
      {(envEntries.length > 0 || generatedConfig) && (
        <div>
          <label className="block text-xs font-medium text-gray-700 mb-1">
            Environment Variables
            {envVars.length > 0 && (
              <span className="text-gray-400 font-normal"> — {envVars.length} detected from repository</span>
            )}
          </label>
          <EnvVarRows
            entries={envEntries}
            onChange={setEnvEntries}
            disabled={connectionState === 'connected'}
          />
        </div>
      )}

      {/* Connection Status */}
      {generatedConfig && (
        <div className="flex items-center justify-between">
          <StatusBadge
            state={connectionState === 'connected' ? 'ready' : connectionState === 'failed' ? 'failed' : 'disconnected'}
            isActive={connectionState === 'connected'}
          />
          {connectionState === 'connected' ? (
            !isRunningInspection && (
              <button
                type="button"
                onClick={handleDisconnect}
                className="px-4 py-2 bg-red-600 text-white text-sm font-medium rounded hover:bg-red-700"
              >
                Disconnect
              </button>
            )
          ) : (
            <button
              type="button"
              onClick={handleConnect}
              disabled={!generatedConfig || !serverName.trim() || !serverDescription.trim() || connectionState === 'connecting'}
              className="px-4 py-2 bg-green-600 text-white text-sm font-medium rounded hover:bg-green-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
            >
              {connectionState === 'connecting' ? 'Connecting...' : 'Connect'}
            </button>
          )}
        </div>
      )}

      {/* Connection Error */}
      {connectionError && (
        <div className="p-3 bg-red-50 border border-red-200 rounded">
          <div className="text-xs font-medium text-red-900">❌ Error</div>
          <div className="text-xs text-red-800 mt-1">{connectionError}</div>
        </div>
      )}

      {/* Live inspection progress with Disconnect (cancel) */}
      {isRunningInspection && (
        <InspectionProgress
          status={jobStatus}
          onDisconnect={handleDisconnect}
          isCancelling={isCancelling}
        />
      )}

      {/* Run Inspection Button */}
      {connectionState === 'connected' && !isRunningInspection && (
        <RunInspectionButton
          isRunning={isRunningInspection}
          apiAvailable={true}
          onRun={handleRunInspection}
          isDisabled={!serverName.trim() || !serverDescription.trim() || !envValidation.valid}
        />
      )}

      {/* Inline env validation hint */}
      {connectionState === 'connected' && !envValidation.valid && !isRunningInspection && (
        <p className="text-xs text-red-600">
          {envValidation.missing.length > 0 && `Enter values for: ${envValidation.missing.join(', ')}. `}
          {envValidation.placeholders.length > 0 && `Replace placeholder values for: ${envValidation.placeholders.join(', ')}.`}
        </p>
      )}

      {/* Export Button */}
      {inspectionResult?.success && inspectionResult.report_data && (
        <ExportButtons
          serverName={inspectionResult.report_data.server_name || serverName}
          reportPaths={inspectionResult.report_paths}
        />
      )}

      {/* Inspection Report */}
      {inspectionResult && (
        <div className="border-t border-gray-200 pt-4 mt-4">
          <h3 className="text-sm font-semibold mb-3">Inspection Report</h3>
          <InspectionReport inspectionResult={inspectionResult} />
        </div>
      )}

      {/* Inspection Error (typed, from backend classifier when available) */}
      {inspectionError && (
        <ErrorDisplay error={inspectionError} errorDetails={inspectionErrorDetails} />
      )}
    </div>
  )
}
