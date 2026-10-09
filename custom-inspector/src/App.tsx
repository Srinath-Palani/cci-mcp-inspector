import { BrowserRouter as Router, Routes, Route } from 'react-router-dom'
import { McpServers } from './components/McpServers.js'
import { OAuthCallback } from './components/OAuthCallback.js'
import netskopeLogo from './assets/netskope-logo.svg'

function App() {
  return (
    <Router>
      <Routes>
        <Route path="/oauth/callback" element={<OAuthCallback />} />
        <Route
          path="/"
          element={
            <div className="min-h-screen bg-gray-50">
              <header className="bg-white border-b border-gray-200 px-6 py-3">
                <a
                  href="https://www.netskope.com/"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-block"
                >
                  <img src={netskopeLogo} alt="Netskope" className="h-6" />
                </a>
              </header>
              <div className="p-4">
              <div className="w-full max-w-7xl mx-auto">
                <div className="text-center mb-6">
                  <h1 className="text-3xl font-bold text-gray-900 mb-2">Custom MCP Inspector</h1>
                  <p className="text-gray-600">
                    Minimal demo showcasing extracted OAuth and MCP connection logic with{' '}
                    <a
                      href="https://github.com/modelcontextprotocol/use-mcp"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-blue-600 hover:text-blue-800 underline font-medium transition-colors"
                    >
                      use-mcp
                    </a>{' '}
                    React hook
                  </p>
                </div>
                <McpServers />
              </div>
              </div>
            </div>
          }
        />
      </Routes>
    </Router>
  )
}

export default App

