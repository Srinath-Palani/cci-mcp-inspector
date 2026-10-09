import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// StrictMode intentionally removed: it double-invokes effects in dev which
// causes use-mcp to open two OAuth popup tabs instead of one.
createRoot(document.getElementById('root')!).render(<App />)

