# Custom MCP Inspector

A React-based MCP inspector application demonstrating extracted OAuth and MCP connection logic.

This application showcases how to extract and reuse OAuth authentication and MCP server connection logic (with auto/http/sse transport support) into separate utility modules.

## Key Features

- **Extracted OAuth Logic**: OAuth callback handling is separated into `src/utils/oauthHandler.ts`
- **Extracted MCP Connection Logic**: MCP connection with transport type selection is separated into `src/utils/mcpConnection.ts`
- **Clean Separation**: Connection logic is reusable and can be easily integrated into other projects
- **Transport Type Support**: Supports auto, HTTP, and SSE transport types

## Project Structure

```
src/
  utils/
    mcpConnection.ts    # Extracted MCP connection hook with OAuth and transport logic
    oauthHandler.ts     # Extracted OAuth callback handler
  components/
    McpServers.tsx      # Main component using extracted connection logic
    OAuthCallback.tsx   # OAuth callback component using extracted handler
  App.tsx
  main.tsx
```

## Getting Started

### Prerequisites

- Node.js 18+ and pnpm installed

### Installation

```bash
pnpm install
```

This will automatically install `use-mcp` from npm (no need to build the parent library).

### Development

Start the development server:

```bash
pnpm dev
```

The app will be available at **http://localhost:5003**

### Testing the Application

1. **Open the app** in your browser at `http://localhost:5003`

2. **Test with Example MCP Servers** (optional):
   
   If you have the `use-mcp` repository cloned, you can test with the example servers:
   
   - **Hono MCP Server**: Start it from `examples/servers/hono-mcp`:
     ```bash
     cd ../../servers/hono-mcp
     pnpm dev
     # Server runs on http://localhost:5101
     ```
     Then connect to: `http://localhost:5101` in the custom-inspector
   
   - **CF Agents MCP Server**: Start it from `examples/servers/cf-agents`:
     ```bash
     cd ../../servers/cf-agents
     pnpm dev
     # Server runs on http://localhost:5102
     ```
     Then connect to: `http://localhost:5102` in the custom-inspector
   
   **Note**: You can also connect to any publicly available MCP server URL.

3. **Test Connection Flow**:
   - Enter an MCP server URL in the input field
   - Select a transport type (Auto, HTTP, or SSE)
   - Click "Connect"
   - Watch the connection state change (Discovering → Connecting → Ready)
   - If OAuth is required, the authentication flow will be handled automatically

4. **Test Features**:
   - **Tools**: Expand tools, fill in parameters, and execute them
   - **Resources**: Browse and read resource contents
   - **Prompts**: View and interact with server prompts
   - **Debug Log**: Monitor connection logs in real-time
   - **Transport Types**: Try switching between Auto, HTTP, and SSE

5. **Test OAuth Callback**:
   - Navigate to `http://localhost:5003/oauth/callback` to test the OAuth callback handler
   - This route uses the extracted `handleOAuthCallback` function

### Build

Build for production:

```sh
pnpm build
```

The built files will be in the `dist` directory. You can preview the production build locally with:

```sh
pnpm preview
```

## Development Commands

- **Dev server**: `pnpm dev` (runs on port 5003)
- **Build**: `pnpm build` (runs TypeScript compilation then Vite build)
- **Lint**: `pnpm lint` (ESLint)
- **Preview**: `pnpm preview` (preview production build locally)

## Standalone Usage

This project is completely standalone and doesn't require the parent `use-mcp` repository. Simply:

1. Clone or copy this `custom-inspector` directory anywhere
2. Run `pnpm install` to install dependencies (including `use-mcp` from npm)
3. Run `pnpm dev` to start the development server

The project uses the published `use-mcp` package from npm (version ^0.0.21), so no local build is needed.

## Extracted Logic

### `useMcpConnection` Hook

Located in `src/utils/mcpConnection.ts`, this hook encapsulates:
- MCP server connection management
- OAuth authentication flow
- Transport type selection (auto/http/sse)
- Connection state management
- Tool, resource, and prompt access

### `handleOAuthCallback` Function

Located in `src/utils/oauthHandler.ts`, this function handles:
- OAuth callback processing
- Token extraction and storage

## Usage Example

```tsx
import { useMcpConnection } from './utils/mcpConnection'

function MyComponent() {
  const {
    state,
    tools,
    connect,
    disconnect,
    callTool,
  } = useMcpConnection({
    url: 'https://your-mcp-server.com',
    transportType: 'auto', // or 'http' or 'sse'
    debug: true,
  })

  // Use the connection...
}
```

## Testing Checklist

- [ ] App loads at http://localhost:5003
- [ ] Can enter MCP server URL
- [ ] Can select transport type (Auto/HTTP/SSE)
- [ ] Connection state updates correctly
- [ ] Tools are displayed when connected
- [ ] Can execute tools with parameters
- [ ] Resources are displayed and readable
- [ ] Prompts work correctly
- [ ] Debug log shows connection events
- [ ] OAuth callback route works (`/oauth/callback`)
- [ ] Disconnect functionality works
- [ ] Clear storage functionality works
