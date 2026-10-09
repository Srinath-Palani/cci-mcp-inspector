/**
 * Per-variable environment editor.
 *
 * Replaces the raw JSON textarea: each variable is a key/value row. Detected
 * (required) variables are pre-filled as keys with empty values; empty or
 * placeholder values are flagged inline and block Run Inspection.
 */

export interface EnvVarEntry {
  key: string
  value: string
  required: boolean
}

const PLACEHOLDER_VALUES = new Set([
  'your_value_here', 'your-value-here', 'changeme', 'change_me', 'xxx', 'todo', '****', '**',
])

export function isPlaceholderValue(value: string): boolean {
  const v = value.trim()
  if (!v) return false // empty is "missing", not "placeholder"
  if (PLACEHOLDER_VALUES.has(v.toLowerCase())) return true
  if (v.startsWith('<') && v.endsWith('>')) return true
  return false
}

/** Validation summary used to gate the Run Inspection button. */
export function validateEnvEntries(entries: EnvVarEntry[]): {
  valid: boolean
  missing: string[]
  placeholders: string[]
} {
  const missing = entries.filter((e) => e.required && !e.value.trim() && !e.value.startsWith('${')).map((e) => e.key)
  const placeholders = entries.filter((e) => isPlaceholderValue(e.value)).map((e) => e.key)
  return { valid: missing.length === 0 && placeholders.length === 0, missing, placeholders }
}

/** Convert entries to the env dict sent to the backend (skips empty optional rows). */
export function entriesToEnv(entries: EnvVarEntry[]): Record<string, string> | undefined {
  const env: Record<string, string> = {}
  for (const e of entries) {
    if (e.key.trim() && e.value.trim()) {
      env[e.key.trim()] = e.value
    }
  }
  return Object.keys(env).length > 0 ? env : undefined
}

/** Build initial entries from detected variable names (values empty, required). */
export function entriesFromDetected(varNames: string[]): EnvVarEntry[] {
  return varNames.map((name) => ({ key: name, value: '', required: true }))
}

/** Build entries from an imported env dict (e.g. from a config file). */
export function entriesFromEnvDict(env: Record<string, string>, requiredNames: string[] = []): EnvVarEntry[] {
  const required = new Set(requiredNames)
  return Object.entries(env).map(([key, value]) => ({
    key,
    value,
    required: required.has(key),
  }))
}

interface EnvVarRowsProps {
  entries: EnvVarEntry[]
  onChange: (entries: EnvVarEntry[]) => void
  disabled?: boolean
}

export function EnvVarRows({ entries, onChange, disabled }: EnvVarRowsProps) {
  const update = (index: number, patch: Partial<EnvVarEntry>) => {
    const next = entries.slice()
    next[index] = { ...next[index], ...patch }
    onChange(next)
  }

  const remove = (index: number) => {
    onChange(entries.filter((_, i) => i !== index))
  }

  const add = () => {
    onChange([...entries, { key: '', value: '', required: false }])
  }

  return (
    <div className="space-y-1.5">
      {entries.map((entry, i) => {
        const empty = entry.required && !entry.value.trim()
        const placeholder = isPlaceholderValue(entry.value)
        const invalid = empty || placeholder
        return (
          <div key={i}>
            <div className="flex gap-2 items-center">
              <input
                type="text"
                className="w-2/5 p-2 border border-gray-200 rounded text-sm font-mono placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-300"
                placeholder="VAR_NAME"
                value={entry.key}
                onChange={(e) => update(i, { key: e.target.value })}
                disabled={disabled || entry.required}
              />
              <input
                type="password"
                className={`flex-1 p-2 border rounded text-sm font-mono placeholder-gray-400 focus:outline-none focus:ring-1 ${
                  invalid ? 'border-red-300 focus:ring-red-300 bg-red-50' : 'border-gray-200 focus:ring-blue-300'
                }`}
                placeholder={entry.required ? 'Required — enter value' : 'Value'}
                value={entry.value}
                onChange={(e) => update(i, { value: e.target.value })}
                disabled={disabled}
              />
              {entry.required ? (
                <span className="text-xs text-red-500 w-14 text-center flex-shrink-0">required</span>
              ) : (
                <button
                  type="button"
                  onClick={() => remove(i)}
                  disabled={disabled}
                  className="text-xs text-gray-400 hover:text-red-500 w-14 text-center flex-shrink-0"
                  title="Remove variable"
                >
                  remove
                </button>
              )}
            </div>
            {placeholder && (
              <p className="text-xs text-red-600 mt-0.5">
                This looks like a placeholder — enter the real value.
              </p>
            )}
          </div>
        )
      })}
      <button
        type="button"
        onClick={add}
        disabled={disabled}
        className="text-xs text-blue-600 hover:underline disabled:text-gray-300"
      >
        + Add variable
      </button>
      <p className="text-xs text-gray-400">
        Values are sent only to your local Inspector backend. Leave a value empty to
        use the variable from the backend&apos;s .env file, or use <code>{'${VAR}'}</code> to
        reference a backend environment variable.
      </p>
    </div>
  )
}
