# 📖 MCP Server Inspector - Usage Guide

Complete guide for installing dependencies and running the MCP Server Inspector workflow.

---

## 📋 Table of Contents

1. [Prerequisites](#prerequisites)
2. [Installation](#installation)
3. [Environment Setup](#environment-setup)
4. [Running the Workflow](#running-the-workflow)
5. [Usage Examples](#usage-examples)
6. [Understanding Output](#understanding-output)
7. [Troubleshooting](#troubleshooting)

---

## 🔧 Prerequisites

Before you begin, ensure you have:

- ✅ **Python 3.11 or higher** (check with `python --version`)
- ✅ **Node.js 18 or higher** (for MCP servers that use npm packages)
- ✅ **OpenAI API key** (for AI-powered analysis)
- ✅ **Git** (for cloning the repository)

---

## 📦 Installation

### Step 1: Clone the Repository

```bash
git clone <repository-url>
cd MCP_Server_Inspector
```

### Step 2: Create Virtual Environment

**On macOS/Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**On Windows:**
```bash
python -m venv .venv
.venv\Scripts\activate
```

### Step 3: Install Dependencies

```bash
# Install all required packages from requirements.txt
pip install -r requirements.txt
```

**What gets installed:**
- `langchain==0.3.27` - LangChain framework
- `langgraph==0.6.11` - LangGraph for workflow orchestration
- `langchain-mcp-adapters==0.1.10` - MCP protocol adapters
- `langchain-openai==0.3.35` - OpenAI integration
- `langchain-community==0.3.31` - Community integrations
- `python-dotenv==1.1.1` - Environment variable management
- `pydantic==2.10.6` - Data validation
- `asyncio==3.4.3` - Async support
- `IPython==9.6.0` - Interactive Python shell

### Step 4: Verify Installation

```bash
# Test that all packages are installed correctly
python test_installation.py
```

You should see:
```
✅ All required packages are installed!
```

---

## 🔐 Environment Setup

### Step 1: Create `.env` File

```bash
# Copy the template
cp env_template.txt .env

# Or create manually
touch .env  # macOS/Linux
# or
type nul > .env  # Windows
```

### Step 2: Configure Environment Variables

Edit the `.env` file and add your credentials:

```bash
# Required: OpenAI API Key (for AI analysis)
OPENAI_API_KEY=sk-proj-your-openai-key-here

# Optional: Server-specific tokens (if inspecting those servers)
GITHUB_PERSONAL_ACCESS_TOKEN=github_pat_your-token-here
NOTION_TOKEN=secret_your-notion-token-here
LINEAR_API_KEY=lin_api_your-linear-key-here

# Optional: OAuth credentials (if using OAuth servers)
AUTH0_CLIENT_ID=your-client-id
AUTH0_CLIENT_SECRET=your-client-secret
AUTH0_TOKEN_URL=https://your-tenant.auth0.com/oauth/token
```

**Important Notes:**
- The `.env` file is in `.gitignore` and will NOT be committed to git
- Only `OPENAI_API_KEY` is required for basic usage
- Add other tokens only if you plan to inspect those specific servers

### Step 3: Verify Environment

```bash
# Check that .env file is loaded
python -c "from dotenv import load_dotenv; load_dotenv(); import os; print('OPENAI_API_KEY:', 'SET' if os.getenv('OPENAI_API_KEY') else 'NOT SET')"
```

---

## 🚀 Running the Workflow

### Basic Command Structure

```bash
python -m src.workflows.mcp_inspector_workflow [OPTIONS]
```

### Command-Line Options

| Option | Description | Example |
|-------|-------------|---------|
| `--config <path>` | Path to server configuration JSON file | `--config ./custom_config.json` |
| `--server <name>` | Name of server(s) to inspect (comma-separated) | `--server Notion-mcp` |
| `--all` | Inspect all servers from config file | `--all` |
| `--enable-testing` | Enable tool testing (experimental) | `--enable-testing` |
| `--output-dir <path>` | Custom output directory for all reports | `--output-dir ./my_reports` |

---

## 💡 Usage Examples

### Example 1: Inspect First Server (Default)

```bash
python -m src.workflows.mcp_inspector_workflow
```

**What happens:**
- Uses default config: `examples/example_servers.json`
- Inspects the first server in the configuration
- Generates reports in `reports/<server_name>_<timestamp>/`

**Note:** Use `--output-dir` to specify a custom directory instead of the default timestamped one.

### Example 2: Inspect Specific Server

```bash
# Inspect a single server by name
python -m src.workflows.mcp_inspector_workflow --server Notion-mcp
```

**Output:**
```
================================================================================
                    🔍 MCP SERVER INSPECTOR 🔍
================================================================================

📋 Loaded configuration for: Notion-mcp
📁 Output directory: reports/Notion-mcp_20251111_143022

================================================================================
PHASE 0: AUTHENTICATION DISCOVERY
================================================================================
...
```

### Example 3: Inspect Multiple Servers

```bash
# Inspect multiple servers (comma-separated)
python -m src.workflows.mcp_inspector_workflow --server Notion-mcp,Canva-gemini-oauth
```

**What happens:**
- Inspects both servers sequentially
- Aggregates results into a single CSV file
- Creates `reports/multi_server_inspection_<timestamp>/aggregated_attribute_checklist.csv`

### Example 4: Inspect All Servers

```bash
# Inspect all servers from config file
python -m src.workflows.mcp_inspector_workflow --all
```

**What happens:**
- Processes all servers in `examples/example_servers.json`
- Respects the `skip` flag (servers with `skip: 1` are skipped)
- Generates aggregated CSV with all results

### Example 5: Use Custom Config File

```bash
# Use a different configuration file
python -m src.workflows.mcp_inspector_workflow \
  --config ./mcp_config/example_servers.json \
  --server Canva-gemini-oauth
```

**Use case:** When you have multiple config files for different environments or server sets.

### Example 6: Inspect All Servers from Custom Config

```bash
# Inspect all servers from custom config
python -m src.workflows.mcp_inspector_workflow \
  --config ./mcp_config/example_servers.json \
  --all
```

### Example 7: Enable Testing (Experimental)

```bash
# Run inspection with tool testing enabled
python -m src.workflows.mcp_inspector_workflow \
  --server Notion-mcp \
  --enable-testing
```

**Note:** Testing agent is experimental and may not be fully implemented.

### Example 8: Use Custom Output Directory

```bash
# Single server with custom output directory
python -m src.workflows.mcp_inspector_workflow \
  --config ./mcp_config/example_servers.json \
  --server Notion-mcp \
  --output-dir ./my_reports
```

**What happens:**
- All reports are saved directly to `./my_reports/`
- No timestamped subdirectory is created
- Useful for organizing reports in a specific location

### Example 9: Multi-Server Inspection with Custom Output Directory

```bash
# Inspect all servers and save to custom directory
python -m src.workflows.mcp_inspector_workflow \
  --config ./mcp_config/example_servers.json \
  --all \
  --output-dir ./batch_inspection_results
```

**What happens:**
- Each server gets its own subdirectory: `./batch_inspection_results/server_name/`
- Aggregated CSV and HTML are saved in: `./batch_inspection_results/aggregated_attribute_checklist.csv`
- All results are organized under one directory

---

## 📊 Understanding Output

### Console Output

The workflow prints progress and results to the console:

```
================================================================================
                    🔍 MCP SERVER INSPECTOR 🔍
================================================================================

📋 Loaded configuration for: Notion-mcp
📁 Output directory: reports/Notion-mcp_20251111_143022

================================================================================
PHASE 0: AUTHENTICATION DISCOVERY
================================================================================
🔍 Testing endpoint: https://mcp.notion.com/mcp
✅ Authentication discovery completed

================================================================================
PHASE 1: DISCOVERY
================================================================================
🔍 Discovering server capabilities...
✅ Discovery completed: 15 tools, 0 resources, 0 prompts

================================================================================
PHASE 2: ANALYSIS
================================================================================
🧠 Analyzing server capabilities...
✅ Analysis completed

================================================================================
PHASE 3: ATTRIBUTE EXTRACTION
================================================================================
📋 Extracting server attributes...
✅ Attributes extracted

================================================================================
PHASE 4: REPORT GENERATION
================================================================================

🎨 Generating attribute reports...

================================================================================
✅ INSPECTION COMPLETED SUCCESSFULLY
================================================================================

📄 Full report: reports/Notion-mcp_20251111_143022/inspection_report.json
📋 Attribute checklist: reports/Notion-mcp_20251111_143022/attribute_checklist.md
📊 HTML report: reports/Notion-mcp_20251111_143022/attribute_report.html
📈 CSV export: reports/Notion-mcp_20251111_143022/attribute_checklist.csv
```

### Generated Files

After inspection, the following files are created in the output directory:

1. **`inspection_report.json`** - Complete inspection report (JSON)
   - Server metadata
   - Discovered tools, resources, prompts
   - AI analysis results
   - Extracted attributes

2. **`attribute_checklist.md`** - Human-readable attribute checklist (Markdown)
   - Yes/No checklist for all attributes
   - Capabilities details
   - Non-read-only tools

3. **`attribute_report.html`** - Interactive HTML report
   - Formatted for easy viewing
   - Includes all details from Markdown report

4. **`attribute_checklist.csv`** - CSV export for analysis
   - Spreadsheet-friendly format
   - Can be opened in Excel, Google Sheets, etc.

5. **`discovery_raw.json`** - Raw discovery results (intermediate)
6. **`analysis_raw.json`** - Raw analysis results (intermediate)

### Report Structure

**Inspection Report (`inspection_report.json`):**
```json
{
  "server_name": "Notion-mcp",
  "connection_type": "stdio",
  "discovery_timestamp": "2025-11-11T14:30:22",
  "server_attributes": {
    "distribution_type": {"official": true, "community": false},
    "pricing": {"free": true, "paid": false},
    "authentication": {
      "oauth2_1_authorization_code": true,
      "bearer_token": true
    },
    "protocol_version": {"detected_version": "2024-11-05"},
    "transport_protocol": {"stdio": true, "http_sse": false},
    "tools_operation_type": {"read_update_delete": true},
    "deployment_approach": {"remote": true}
  },
  "capabilities": {
    "tools": true,
    "resources": false,
    "prompts": false
  },
  "tools": [...],
  "resources": [...],
  "prompts": [...],
  "analysis": {
    "complexity_score": 7.5,
    "primary_use_case": "Notion workspace management",
    "tool_categories": [...],
    "strengths": [...]
  },
  "statistics": {
    "total_tools": 15,
    "total_resources": 0,
    "total_prompts": 0
  }
}
```

---

## 🔍 Multi-Server Inspection

### Aggregated CSV Output

When inspecting multiple servers, an aggregated CSV is generated:

```bash
python -m src.workflows.mcp_inspector_workflow --server Server1,Server2,Server3
```

**Output:** `reports/multi_server_inspection_<timestamp>/aggregated_attribute_checklist.csv`

**With custom output directory:**
```bash
python -m src.workflows.mcp_inspector_workflow \
  --server Server1,Server2,Server3 \
  --output-dir ./my_results
```

**Output:** `./my_results/aggregated_attribute_checklist.csv` (and `./my_results/server_name/` for each server)

**CSV Structure:**
```csv
Server Name,Attribute,Type,Status
Server1,Distribution Type,official,Yes
Server1,Distribution Type,community,No
Server1,Authentication,oauth2_1_authorization_code,Yes
Server2,Distribution Type,official,No
Server2,Distribution Type,community,Yes
...
```

**Benefits:**
- Compare attributes across multiple servers
- Easy to filter and analyze in spreadsheet tools
- All data in one place

---

## ⚙️ Configuration File Format

The workflow uses `examples/example_servers.json` by default. Each server entry:

```json
{
  "servers": [
    {
      "name": "Notion-mcp",
      "description": "Notion MCP Server",
      "connection_type": "stdio",
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "endpoint_url": "https://mcp.notion.com/mcp",
      "env": {
        "NOTION_TOKEN": "your-token-here"
      },
      "authentication": {
        "type": "oauth2_1_authorization_code",
        "token_url": "https://mcp.notion.com/token",
        "client_id": "your-client-id",
        "access_token": "your-access-token",
        "refresh_token": "your-refresh-token"
      },
      "skip": 0
    }
  ]
}
```

**Fields:**
- `name`: Unique server identifier
- `connection_type`: `"stdio"` or `"sse"`
- `command`: Command to run (e.g., `"npx"`, `"node"`)
- `args`: Command arguments
- `endpoint_url`: Remote endpoint URL (for SSE connections)
- `env`: Environment variables (tokens, API keys)
- `authentication`: OAuth configuration (optional)
- `skip`: Set to `1` to skip this server when using `--all`

---

## 🐛 Troubleshooting

### Issue: "Module not found" or "No module named 'langchain'"

**Solution:**
```bash
# Ensure virtual environment is activated
source .venv/bin/activate  # macOS/Linux
# or
.venv\Scripts\activate  # Windows

# Reinstall dependencies
pip install -r requirements.txt
```

### Issue: "OPENAI_API_KEY not found"

**Solution:**
```bash
# Check .env file exists
ls -la .env  # macOS/Linux
dir .env  # Windows

# Verify .env file has correct format
cat .env  # macOS/Linux
type .env  # Windows

# Should contain:
# OPENAI_API_KEY=sk-proj-your-key-here
```

### Issue: "No servers configured" or "Failed to load server configuration"

**Solution:**
```bash
# Check config file exists
ls examples/example_servers.json

# Validate JSON syntax
python -m json.tool examples/example_servers.json

# Check server name matches
python -c "import json; data=json.load(open('examples/example_servers.json')); print([s['name'] for s in data['servers']])"
```

### Issue: "Connection timeout" or "Failed to connect to server"

**Solution:**
- Verify Node.js is installed: `node --version`
- Check server command is correct in config
- Ensure npm package exists: `npm view @notionhq/notion-mcp-server`
- For remote servers, verify endpoint URL is accessible

### Issue: "Authentication failed" or "Invalid token"

**Solution:**
- Check environment variables in `.env` file
- Verify token is valid and not expired
- For OAuth, ensure `access_token` and `refresh_token` are correct
- Check token has required scopes/permissions

### Issue: "Server skipped" (when using --all)

**Solution:**
- Check `skip` field in server config
- Set `"skip": 0` to enable inspection
- Or use `--server <name>` to inspect specific server

### Issue: "Permission denied" or "Command not found"

**Solution:**
```bash
# Ensure Python is in PATH
which python  # macOS/Linux
where python  # Windows

# Use python3 if python doesn't work
python3 -m src.workflows.mcp_inspector_workflow --server Notion-mcp
```

### Issue: "CSV parsing error" (multi-server inspection)

**Solution:**
- This is usually a formatting issue with multi-line fields
- The workflow uses Python's `csv` module to handle this
- If issues persist, check individual server reports first

---

### Quick Reference

```bash
# Activate virtual environment
source .venv/bin/activate  # macOS/Linux
.venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Run inspection
python -m src.workflows.mcp_inspector_workflow --server <name>

# Inspect all servers
python -m src.workflows.mcp_inspector_workflow --all

# Use custom config
python -m src.workflows.mcp_inspector_workflow --config <path> --server <name>

# Use custom output directory
python -m src.workflows.mcp_inspector_workflow --server <name> --output-dir ./my_reports

# Inspect all servers with custom output directory
python -m src.workflows.mcp_inspector_workflow --all --output-dir ./batch_results

# Usage of `generate_mcp_config_from_oauth.py` to convert OAuth tokens to configs
python generate_mcp_config_from_oauth.py <absolute_path of the mcp-oauth-tokens.json>  --output-dir mcp_config --output-file mcp_client.json
```

## Few More Examples

```bash
# Use default config file and inspect all servers:
	python -m src.workflows.mcp_inspector_workflow --all

# Use custom config file and inspect all servers:
	python -m src.workflows.mcp_inspector_workflow --config /path/to/custom_servers.json --all

# Use custom config file and inspect specific servers:
	python -m src.workflows.mcp_inspector_workflow --config /path/to/custom_servers.json --server Server1,Server2

# Use default config file and inspect specific servers:
	python -m src.workflows.mcp_inspector_workflow --server Canva-gemini-oauth,Notion-gemini-oauth

# Use custom output directory for single server:
	python -m src.workflows.mcp_inspector_workflow --server Notion-mcp --output-dir ./my_reports

# Use custom output directory for all servers:
	python -m src.workflows.mcp_inspector_workflow --all --output-dir ./batch_results
```
---

## ✅ Quick Start Checklist

- [ ] Python 3.11+ installed
- [ ] Node.js 18+ installed
- [ ] Repository cloned
- [ ] Virtual environment created and activated
- [ ] Dependencies installed (`pip install -r requirements.txt`)
- [ ] `.env` file created with `OPENAI_API_KEY`
- [ ] Server configuration file ready (`examples/example_servers.json`)
- [ ] First inspection run successfully

---

## 🎯 Next Steps

After your first successful inspection:

1. **Explore Reports**: Open the generated HTML report to see detailed results
2. **Compare Servers**: Run inspections on multiple servers and compare attributes
3. **Customize Analysis**: Edit analysis prompts to focus on specific aspects
4. **Process OAuth Tokens**: Use `generate_mcp_config_from_oauth.py` to convert OAuth tokens to configs

---

**Happy Inspecting! 🔍**

