### Quick Reference

```bash

git clone https://github.com/netSkope/app-info.git

cd app-info/internal_tools/MCP_Server_Inspector

python3 -m venv .venv

# Activate virtual environment
source .venv/bin/activate  # macOS/Linux
.venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Usage of `generate_mcp_config_from_oauth.py` to convert OAuth tokens to configs:
python generate_mcp_config_from_oauth.py <absolute_path of the mcp-oauth-tokens.json>      --output-dir mcp_config     --output-file mcp_client.json

# Use custom config file and inspect all servers:
python -m  src.workflows.mcp_inspector_workflow --config mcp_config/mcp_client.json   --all

# Use custom output directory:
python -m src.workflows.mcp_inspector_workflow --config mcp_config/mcp_client.json --server notion --output-dir ./my_reports

# Inspect all servers with custom output directory:
python -m src.workflows.mcp_inspector_workflow --config mcp_config/mcp_client.json --all --output-dir ./batch_results

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
	python -m src.workflows.mcp_inspector_workflow --config mcp_config/mcp_client.json --server notion --output-dir ./my_reports

# Use custom output directory for all servers:
	python -m src.workflows.mcp_inspector_workflow --config mcp_config/mcp_client.json --all --output-dir ./batch_results
```
