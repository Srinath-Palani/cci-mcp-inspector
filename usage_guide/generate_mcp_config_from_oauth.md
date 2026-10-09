## 📖 Usage Guide

### Command-Line Arguments

```bash
python generate_mcp_config_from_oauth.py <input_file> [OPTIONS]
```

**Required Arguments**:
- `input_file`: Path to JSON file containing OAuth tokens

**Optional Arguments**:
- `--output-dir <directory>`: Output directory (default: `mcp_config`)
- `--output-file <filename>`: Output filename (default: `example_servers.json`)
- `--no-sse-test`: Skip SSE endpoint testing (faster but less accurate)

### Basic Usage

```bash
# Process OAuth tokens and generate server configurations
python generate_mcp_config_from_oauth.py sample_oauth_tokens.json
```

**Output**: `mcp_config/example_servers.json`

### Advanced Usage Examples

#### Example 1: Custom Output Location
```bash
python generate_mcp_config_from_oauth.py oauth_tokens.json \
  --output-dir ./configs \
  --output-file my_mcp_servers.json
```

**Result**: Creates `configs/my_mcp_servers.json`

### Summary
        Created a standalone script generate_mcp_config_from_oauth.py that:

### Features
        Reads OAuth tokens from a JSON file in the specified format
Tests SSE endpoints: sends a GET request with the OAuth token to check if SSE works
        If it returns 405 (Method Not Allowed), falls back to stdio
        If it returns 200, uses SSE
        If it returns 401/403, assumes SSE might work (auth issue)
Searches npm packages: automatically searches for MCP server npm packages
        Uses server-specific mappings (e.g., @canva/cli, @tacticlaunch/mcp-linear)
        Falls back to common patterns if not found
Generates configurations: creates example_servers.json in the mcp_config folder with:
        Connection type (stdio or sse)
        Command and args (if stdio)
        Endpoint URL (if sse)
        Environment variables

