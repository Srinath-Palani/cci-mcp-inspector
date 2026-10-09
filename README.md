# MCP Server Inspector

A standalone tool that connects to MCP (Model Context Protocol) servers and produces
a complete, evidence-backed profile of each one: protocol version negotiation,
authentication requirements, capabilities (tools / resources / prompts), transport
status, and a structured attribute report — via a CLI **and** a web UI.

Every field in a report traces to a live server response or declared config.
Nothing is inferred from the server name or guessed.

## Features

- **Live protocol handshake** — negotiate-latest-first across MCP spec versions
  (`2024-11-05` … `2026-07-28`), with the negotiation walk recorded.
- **Auth discovery** — detects OAuth 2.1 (authorization-code & client-credentials),
  Bearer token, Personal Access Token, and API key; full browser OAuth flow supported.
- **Capability discovery** — paginated `tools/list`, `resources/list`, `prompts/list`,
  with Read/Write/Delete classification from declared annotations only
  (blank-when-unconfirmed, never a name-keyword guess).
- **Transport status** — flags servers still on the deprecated HTTP/SSE (`/sse`)
  transport vs current Streamable HTTP (`/mcp`), and detects `410 Gone` retired endpoints.
- **Attribute report** — Distribution Type, Protocol Version, Pricing, Hosting,
  Authentication, TLS, Transport, Tools Operations, Deployment, plus a
  **Server Info** section (name, endpoint URL, GitHub repo, and a real resolved
  traffic name — proxy → `server.json` → MCP registry → live handshake).
- **Batch inspection** — inspect many servers at bounded concurrency with a merged
  protocol/capabilities CSV.
- **Reports in every format** — JSON, CSV, Markdown, HTML, console.

## Layout

```
MCP_Server_Inspector/
├── src/                     # inspection engine (CLI + shared)
│   ├── agents/              # discovery, analysis, attribute extraction
│   ├── graph/               # LangGraph pipeline + report builder
│   ├── models/              # pydantic structured-output models
│   ├── utility/             # connection, auth, traffic-name, report generators
│   └── workflows/           # CLI entry point
├── api_bridge/              # FastAPI backend the web UI talks to
├── custom-inspector/        # web UI (React + Vite)
├── examples/                # example server configs
├── tests/                   # test suite
├── requirements.txt
└── mcp.json
```

## Quick start

### 1. Install

```bash
cd MCP_Server_Inspector
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` for the LLM analysis step (see `.env.template`):

```bash
OPENAI_API_KEY=sk-proj-<your-key>
```

### 2. Configure a server

Edit `examples/example_servers.json` (or pass your own `--config`):

```json
{
  "name": "Notion",
  "connection_type": "http",
  "endpoint_url": "https://mcp.notion.com/mcp",
  "skip": 0
}
```

A GitHub-hosted stdio server uses `command`/`args` and an optional `repository`
(recorded in the report's Server Info section).

### 3a. CLI

```bash
# one server
python -m src.workflows.mcp_inspector_workflow --server notion

# all configured servers
python -m src.workflows.mcp_inspector_workflow --all

# custom config + output dir
python -m src.workflows.mcp_inspector_workflow --config mcp_config/mcp_client.json --all --output-dir ./batch_results
```

### 3b. Web UI

```bash
# backend (FastAPI, binds 127.0.0.1:8000) — run from the project root
python api_bridge/main.py

# frontend (React dev server)
cd custom-inspector
pnpm install
pnpm dev
```

The UI lets you add endpoints, auto-detect auth (Connect), run OAuth in a popup,
inspect one or many servers, and download each report format.

## Output files

Each inspection writes to `reports/<server>_<timestamp>/`:

| File | Contents |
|---|---|
| `inspection_report.json` | full merged report (capabilities, tools, attributes, metadata) |
| `discovery_raw.json` | raw discovery result, incl. `endpoint_url` / `repository` / `transport_info` |
| `analysis_raw.json` | raw LLM analysis result |
| `attribute_checklist.md` / `.csv` | attribute checklist (Server Info, Distribution Type, …) |
| `attributes.csv` | full attribute rows |
| `attribute_report.html` | rendered HTML report |
| `mcp_capabilities.csv` | per-tool capability rows |

Multi-server runs also write `all_servers_protocol.csv` (protocol + transport per
server) and `all_servers_capabilities.csv`.

## Tests

Run from the project root:

```bash
python tests/test_installation.py     # smoke-test the install
python tests/test_spec_alignment.py   # report-format / classification checks
```

(The suite includes API regression, transport-fallback, batch-concurrency, and
end-to-end tests; several hit live endpoints.)

## Notes

- The web UI requires the `api_bridge` backend — it holds the secrets, spawns stdio
  servers, and bypasses browser CORS, none of which a browser can do alone.
- Real credentials live only in `.env` (gitignored). `examples/` and
  `.env.template` contain placeholders only.
