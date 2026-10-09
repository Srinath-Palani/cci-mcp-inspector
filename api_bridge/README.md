# MCP Inspector API Bridge

FastAPI server that exposes Server Inspector functionality as REST API.

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

**Note:** The API bridge requirements include all Server Inspector dependencies, so you don't need to install them separately.

2. Set up environment variables (create `.env` file in `MCP_Server_Inspector/` directory):
```bash
cd ../MCP_Server_Inspector
echo "OPENAI_API_KEY=your-openai-key-here" > .env
cd ../api_bridge
```

Or set the environment variable directly:
```bash
export OPENAI_API_KEY=your-openai-key-here
```

## Running

```bash
python main.py
```

Or with uvicorn directly:
```bash
uvicorn main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`

## API Endpoints

### `POST /api/discover-auth`
Discover OAuth requirements for an MCP server.

**Request:**
```json
{
  "endpoint_url": "https://mcp.example.com/mcp"
}
```

**Response:**
```json
{
  "success": true,
  "auth_required": true,
  "auth_config": {
    "type": "oauth2_1_authorization_code",
    "token_url": "https://mcp.example.com/token",
    "authorization_url": "https://mcp.example.com/authorize"
  },
  "metadata_available": true,
  "recommendations": []
}
```

### `POST /api/inspect`
Run full MCP server inspection.

**Request:**
```json
{
  "server_name": "My Server",
  "endpoint_url": "https://mcp.example.com/mcp",
  "connection_type": "sse",
  "oauth_tokens": {
    "token_url": "https://mcp.example.com/token",
    "client_id": "client-id",
    "access_token": "access-token",
    "refresh_token": "refresh-token",
    "expires_at": 1234567890
  }
}
```

**Response:**
```json
{
  "success": true,
  "report_path": "/path/to/report.json",
  "report_data": { ... }
}
```

