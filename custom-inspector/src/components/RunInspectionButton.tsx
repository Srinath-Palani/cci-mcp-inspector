interface RunInspectionButtonProps {
  isRunning: boolean
  apiAvailable: boolean | null
  onRun: () => void
  isDisabled?: boolean
}

export function RunInspectionButton({ isRunning, apiAvailable, onRun, isDisabled = false }: RunInspectionButtonProps) {
  return (
    <div className="flex gap-2 items-center">
      <button
        className="bg-green-600 hover:bg-green-700 text-white rounded py-2 px-4 text-sm font-medium disabled:opacity-50 whitespace-nowrap"
        onClick={onRun}
        disabled={isRunning || !apiAvailable || isDisabled}
      >
        {isRunning ? 'Running Inspection...' : 'Run Inspection'}
      </button>
      {apiAvailable === false && (
        <span className="text-xs text-red-600">
          API unavailable - make sure backend is running on localhost:8000
        </span>
      )}
    </div>
  )
}

