# Code Review — MCP Server Inspector

**Scope:** `api_bridge/`, `src/agents/`, `src/utility/`, `src/graph/`, `src/workflows/`, `src/models/`, `generate_mcp_config_from_oauth.py` (~14.6K lines of Python). Reviewed adversarially across 4 parallel passes; the most severe findings were re-verified directly against source.

**Summary:** ~95 findings. The service as configured is **unsafe to run on any network you don't fully trust** — it binds all interfaces with no authentication and will execute attacker-supplied commands. Separately, several correctness bugs mean **reports silently contain wrong data** (capabilities, auth attributes, testing counters). Both classes matter; the security ones are urgent.

---

## 🔴 CRITICAL — fix before next run on a shared network

### 1. Unauthenticated RCE via `/api/test-stdio-connection` and stdio inspect
**`api_bridge/main.py:372-427`, `565-580`, `916`** — Verified.
`TestStdioConnectionRequest` takes arbitrary `command` / `args` / `env` and executes them via `MCPConnectionManager.connect()`. No auth, no allowlist, no sandbox. Anyone who can reach port 8000 runs commands as you, with your environment (`OPENAI_API_KEY`, `GITHUB_PERSONAL_ACCESS_TOKEN`, …).
**Fix:** bind localhost + add auth (see #3); restrict `command` to an allowlist (`npx`, `uvx`, `node`) with validated args; gate stdio behind an explicit opt-in flag.

### 2. Supply-chain RCE: README-derived command execution
**`generate_mcp_config_from_oauth.py:315-563`** via **`main.py:1141-1161`** (`build_stdio_config_from_repo`) — Verified.
Batch targets of kind `github` fetch the repo README, regex-extract an `npx`/`uvx` invocation, and execute it with attacker-controlled args/env (`args_list.extend(extra_tokens)`). Any public repo — or a README pointing at a typosquatted npm package — controls a command line on your host. `npx -y` auto-installs.
**Fix:** never execute commands parsed from README prose. Only accept a `package_name` verified against the repo's own published package; validate args against `^[\w@./=-]+$`; require explicit per-run confirmation.

### 3. Server binds `0.0.0.0` with zero authentication
**`api_bridge/main.py:2137-2139`** — Verified (`uvicorn.run(app, host="0.0.0.0", port=8000)`).
Every endpoint — stdio exec, report reads, OAuth status, `/mcp-proxy` SSRF — is open to the LAN (or internet, if exposed). Compounds #1 and #2 into one-request host compromise.
**Fix:** default bind to `127.0.0.1`; add a per-start bearer token enforced by middleware on all routes.

### 4. OAuth Authorization-Code flow: no `state`, no URL-encoding
**`src/utility/oauth_manager.py:210`** — Verified (`'&'.join(f'{k}={v}' ...)`, no `state=` anywhere in the file).
Classic OAuth CSRF (callback can't be bound to the initiating session), plus a `redirect_uri` or scope containing `&`/`#` injects extra query params into the authorize request.
**Fix:** add `state = secrets.token_urlsafe(32)` and verify on redirect; build the URL with `urllib.parse.urlencode(auth_params)`.

### 5. OAuth token cache path traversal
**`src/utility/oauth_manager.py:338, 364, 386`** — Verified (`cache_file = self.cache_dir / f"{self.client_id}_token.json"`).
`client_id` comes from remote-supplied auth metadata. A `client_id` of `../../../../home/user/.ssh/authorized_keys` makes the OAuth flow write attacker-controlled JSON (mode 0600) to that path.
**Fix:** hash the client_id for the filename (`sha256`) and assert `cache_file.resolve().is_relative_to(cache_dir.resolve())`.

### 6. `detect_capabilities` reports every capability as supported
**`src/utility/mcp_connection_manager.py:478-517`** — Verified.
`list_tools`/`list_resources`/`list_prompts` catch all exceptions and return `[]`, so the `try/except` around them in `detect_capabilities` never fires — every flag is set `True` even when the server returned method-not-found. **All downstream capability reports are wrong.**
**Fix:** base flags on the handshake capabilities (as `mcp_capability_fetcher` already does correctly), not on the call merely returning.

### 7. `AuthenticationDiscovery.discovered_config` AttributeError on the common path
**`src/utility/auth_discovery.py:696` + `107`** — Verified (no `__init__`; attribute assigned only on the metadata-endpoint path, not the GitHub-README path that returns first).
Calling `validate_config_against_discovered` after a README-based discovery crashes with `AttributeError`.
**Fix:** add `__init__` setting `self.discovered_config = None`; also set it on the GitHub path (~line 47).

### 8. Shared mutable `Utils()` across parallel graph workers
**`src/graph/nodes.py:33`** — module-level `utils = Utils()` shared across all graph invocations including parallel `Send` workers in multi-server runs. If `Utils` holds per-run state (env caches, file handles), parallel servers race.
**Fix:** instantiate per node, or make it stateless/thread-safe.

### 9. Multi-server fan-out uses wrong LangGraph syntax
**`src/graph/inspection_graph.py:333`** — `add_conditional_edges(START, _fan_out, ["inspect_one_server"])` where `_fan_out` returns a list of `Send` objects. LangGraph expects the routing function to return node names, not `Send`s — this raises `ValueError` at runtime on the multi-server path.
**Fix:** return node-name strings from the router and pass payloads separately, or use the documented `Send` pattern.

### 10. Skipped servers silently vanish from results
**`src/graph/inspection_graph.py:115-144`** — `_inspect_one_server` catches all exceptions and returns `{}`, so LangGraph merges nothing: skipped servers appear in neither `completed_reports` nor `failed_servers`. Callers can't tell them apart from success.
**Fix:** return a distinct marker (e.g. `{"skipped_servers": [name]}`).

---

## 🟠 HIGH — security holes and silent data corruption

### API bridge
11. **Path traversal via `server_name` / `report_id` / `output_group`** — `main.py:535-543, 689-690, 1424-1425, 2018-2054`. User-controlled names interpolated into `Path(tempfile.gettempdir())/...` with no validation; `../../` escapes the temp tree for both reads and writes. Fix: validate `^[A-Za-z0-9_-]{1,64}$`, `resolve()` + containment check.
12. **Full tracebacks returned to clients** — `main.py:354-369, 420-427, 771-797`. `_classify_exception` appends `traceback.format_exc()` to `job.error`, served verbatim by the status endpoint. Leaks paths, versions, env-derived values. Fix: generic message + error id; log server-side.
13. **OAuth tokens fetchable by session_id alone, multi-use** — `main.py:2005-2015`. Tokens returned unlimited times within a 10-min TTL, no binding to initiator. Fix: single-use (delete on first retrieval), bind to a caller secret.
14. **SSRF in OAuth discovery** — `oauth_session_manager.py:74-153`. Fetches user-controlled `endpoint_url` with `follow_redirects=True`, then POSTs registration/auth-code payloads to endpoints named by that (attacker-controlled) metadata — including internal/cloud-metadata hosts. Fix: apply the proxy's `validate_target` guard to every fetch in the OAuth flow; pin origins.
15. **DNS-rebinding TOCTOU in proxy SSRF guard** — `mcp_proxy.py:194-254`. `validate_target` resolves, httpx re-resolves on connect. Attacker DNS answers public-IP-then-loopback bypasses the guard. Fix: pin the validated IP via a custom transport.
16. **Unbounded request body buffering in proxy** — `mcp_proxy.py:418`. `await request.body()` with no size cap → memory DoS. Fix: cap at a few MB or stream.
17. **No idle/total deadline on proxied streams** — `mcp_proxy.py:65` (`read=None`). Slow-loris upstreams exhaust the connection pool. Fix: idle watchdog + per-IP stream cap.
18. **Unbounded job creation, no rate limiting on `/api/inspect`** — `inspection_job_manager.py:104-249`. Stuck `running` jobs live forever; a client can spawn unlimited LangGraph runs billed to your OpenAI key. Fix: global in-flight cap + rate limit.
19. **Race in batch retry** — `main.py:1569-1590`. State check and `reset_for_retry` aren't atomic; two concurrent retries can open two MCP sessions to one server. Fix: transition state synchronously in the handler.
20. **Fire-and-forget `create_task` in single-inspect path** — `main.py:916-917, 1215-1221`. Cancel between `create_job` and `mark_running` → job stuck in `pending`, inspection runs anyway. Fix: check `cancel_requested` at top of the runner; add a done-callback.
21. **`${VAR}` resolved from server env for client-supplied names** — `main.py:205-265` + `utils.py:28-36` (`override=True`). A remote caller can exfiltrate arbitrary backend env vars by naming them in `env`. Fix: never resolve placeholders from `os.environ` for request-supplied names; treat as hard errors.
22. **Unredacted `server_config` printed per inspection** — `main.py:1857` — Verified. Prints full tokens (`access_token`, `refresh_token`, `client_secret`, `env`) to stdout/logs. `_redact_secrets` exists but is bypassed here. Fix: use it (or delete the print).

### Agents
23. **Prompt injection: untrusted server content interpolated verbatim into LLM prompts** — `mcp_analysis_agent.py:148-167`. Tool/resource/prompt descriptions and full input schemas from a hostile MCP server go straight into the user prompt with no delimiters or instruction framing. A malicious server can steer the analysis output (e.g. mark itself "malware-safe") or attempt key exfiltration via output fields. Fix: wrap in `<untrusted-data>` tags, instruct the model to treat contents as data only, validate output against discovered tool names.
24. **Trust attributes derived from LLM free text** — `mcp_attribute_extraction_agent.py:246-253`. A README containing "official MCP server" flips `DistributionType.official=True` via string-matching the LLM echo. Fix: never derive trust from LLM text; require verifiable signals (domain/owner match).
25. **Unsound auth inference** — `mcp_attribute_extraction_agent.py:771-788, 882-887`. Any "jwt" substring → claims OAuth Client Credentials; any "oauth" mention → claims *both* OAuth 2.1 flows. False security attributes reported as detected fact. Fix: report only what was actually detected; mark the rest "unknown".
26. **Analysis LLM failures collapse into success-shaped fallbacks** — `mcp_analysis_agent.py:108-120`. A 401 from OpenAI produces "Unknown (analysis failed)" output with success semantics — no signal that the LLM phase is broken fleet-wide. Fix: re-raise `CancelledError`; distinguish transient LLM failure (retry/propagate) from parse failure (fallback); mark degraded output `PARTIAL`.
27. **Naive JSON extraction from LLM responses** — `mcp_analysis_agent.py:226-233, 237, 257`. `find("{")`/`rfind("}")` breaks on prose braces; uncoerced `float(complexity_score)` discards a 95%-good parse on one bad field. Fix: `json.JSONDecoder().raw_decode` scanning; per-field defensive coercion; retry once on parse failure.
28. **No prompt size cap** — `mcp_analysis_agent.py:136-211`. Every tool's full `inputSchema` embedded with `indent=2`, no truncation. A hostile/buggy server with huge schemas blows the context window and burns cost. Fix: cap total prompt size, strip/summarize schemas, batch large tool sets.
29. **Guessed `https://api.{name}.{tld}` endpoints probed with unverified TLS** — `mcp_attribute_extraction_agent.py:960-987, 1130-1180`. Fabricates endpoints from a user-supplied name and opens TLS to 6 TLDs (SSRF-adjacent scanning), with `verify_mode=CERT_NONE` — expired/self-signed certs report "TLS 1.3 OK". Fix: remove the guessing heuristic; validate certs in a separate verified handshake and report `certificate_valid` separately.

### Utility
30. **Credential leak / SSRF via redirect-following with caller headers** — `protocol_version_prober.py:227, 536`, `documentation_analyzer.py:184-186, 245, 606`. `httpx.AsyncClient(follow_redirects=True)` forwards `Authorization: Bearer` cross-origin; redirects to internal hosts followed. Fix: `follow_redirects=False`, validate 3xx targets manually, strip auth on cross-origin.
31. **Path traversal in report/output filenames** — `utils.py:104-114`, `client_config_generator.py:317-318`. Raw `server_name` interpolated into paths; `sanitize_server_name` exists but isn't called and doesn't strip `..`. Fix: strict allowlist regex + containment assert.
32. **Stored XSS in HTML reports** — `attribute_report_generator.py:654-671, 1285-1289`. Server-controlled names/descriptions interpolated without `html.escape`. Fix: escape everything interpolated into HTML.
33. **CSV formula injection** — `capability_csv_exporter.py:135-138`, `attribute_report_generator.py:771-780`. Quoting doesn't stop Excel interpreting `=`, `+`, `-`, `@` prefixes. Fix: prefix dangerous leading chars with `'`.
34. **Blocking OAuth HTTP + `input()` on the event loop** — `mcp_connection_manager.py:176-180` + `oauth_manager.py:219`. Authorization-code flow blocks the whole FastAPI event loop at a terminal prompt. Fix: resolve tokens before async context; route the flow through the callback endpoint.
35. **Unbounded redirect-following fetches (SSRF + memory DoS)** — `auth_discovery.py:232`, `documentation_analyzer.py:245-248, 606-608`. No scheme/host validation, no size cap, redirects followed. Fix: https-only, block private IPs post-DNS, stream with byte limit.

---

## 🟡 MEDIUM — robustness and correctness (grouped)

- **Expired-token fallback** (`oauth_manager.py:90-97`) — returns known-expired token on refresh failure, masking the real error. Clear cache and re-run the flow.
- **Expiry-less config token trusted forever** (`oauth_manager.py:82-83`).
- **Unbounded SSE accumulation** (`protocol_version_prober.py:339-355`) — time-bounded but not byte-bounded; hostile server floods `data:` lines. Cap at ~1 MiB.
- **Bare `except:` swallowing `CancelledError`** — 12 sites (worst: `mcp_connection_manager.py:499-512`, `mcp_attribute_extraction_agent.py` ×5). Cancellation during capability detection is ignored.
- **`_strip_server_prefix` mangles names** (`capability_csv_exporter.py:62-81`) — slices original by normalized-prefix length.
- **HTML report hardcodes protocol versions** (`attribute_report_generator.py:1025-1028`) — omits newer revisions the CSV includes; HTML misreports.
- **Generated configs embed resolved secrets, world-readable** (`client_config_generator.py:188-222`) — keep `${VAR}` verbatim or chmod 600.
- **`pagination_incomplete` flag never set** (`mcp_capability_fetcher.py:240-241`) — truncated tool lists presented as complete.
- **`escape_csv_field` misses `\r`** (`attribute_report_generator.py:778`) — row corruption.
- **`sys.exit()` inside workflow** (`mcp_inspector_workflow.py:139, 150`) — kills the FastAPI process when called via the API bridge. Raise instead.
- **Unguarded `state["..."]` KeyError risks** throughout `src/graph/nodes.py` (lines 108, 186, 238, 297-301, 340, 345) on `TypedDict(total=False)` fields — use `.get()` with defaults.
- **`MCPCapabilities` rejects extra keys** (`structured_output.py:37` + `report_builder.py:37`) — `ValidationError` on unexpected handshake fields. Add `model_config = ConfigDict(extra="ignore")`.
- **Hardcoded per-version booleans** (`structured_output.py:150-205`) — brittle; use a dict keyed off `SUPPORTED_PROTOCOL_VERSIONS`.
- **Aggregation does sync file I/O on the event loop** (`main.py:1253-1353`) — multi-second stalls on 100-report batches; failures swallowed with a bare `print`. Use `asyncio.to_thread`; record `aggregate_error`.
- **Token cache file TOCTOU** (`oauth_manager.py:351-353`) — created world-readable then chmod'd. Use `os.open(..., 0o600)`.
- **Caller-controlled `output_group` as dict key + path** (`main.py:1424-1425`) — collision hijack + path injection.
- **`test_sse_endpoint` sends Bearer token to attacker-influenced URL** (`generate_mcp_config_from_oauth.py:876-961`) and leaks streamed connections on early returns.

---

## 🔵 LOW — hygiene

- Testing agent is a **placeholder** that reports `total_tools_tested=len(tools)` while marking every tool SKIPPED (`mcp_testing_agent.py:14-76`) — misleading counters. Report 0 until implemented.
- Dead code: `readme_lower` unused (`mcp_attribute_extraction_agent.py:1655`); the npx/uvx extraction block below line 388 in `generate_mcp_config_from_oauth.py` is shadowed whenever any JSON config block matched (#19 in bridge review).
- Variable shadowing `auth_type` dict→str (`mcp_attribute_extraction_agent.py:639-642`).
- `error_message` includes full tracebacks returned to API callers (`mcp_discovery_agent.py:507-540`).
- `initial_inspection_state` treats `""` as a valid output dir via truthiness (`inspection_graph.py:71-92`).
- 403 from the proxy leaks resolved internal IPs (`mcp_proxy.py:250-253`).
- `/api/discover-auth` is an unauthenticated SSRF oracle via error-message differences (`main.py:430-468`).

---

## What I checked that's clean

- `api_bridge/error_classifier.py` — pure pattern matching, no I/O or state. No findings.
- No committed `.env` (git-untracked, `.gitignore` covers it), no hardcoded secrets in source, no `eval`/`exec`/`shell=True`/`pickle.loads`.
- The proxy's `validate_target` SSRF guard exists and covers loopback/private/link-local — its remaining hole is the DNS-rebinding TOCTOU (#15), not the check itself.
- Discovery agent's per-attempt timeouts and 90s walk budget are sound; `Utils.get_openai_llm` sets `timeout=60s, max_retries=2` (no infinite retry); `DocumentationAnalyzer` truncates LLM input at 8000 chars.
- All main entry points parse cleanly (`ast.parse` on `main.py`, `mcp_proxy.py`, `mcp_attribute_extraction_agent.py`).

## Test-suite note

`pytest` is not installed in the project venv (`No module named pytest`), so the `test_*.py` suite (12 files) can't be run as-is. `reports/` contains 49 generated report dirs (8.1 MB) checked into the working tree — consider gitignoring generated artifacts.

---

## Recommended order of work

1. **Contain the blast radius (one sitting):** bind `127.0.0.1` + add auth middleware (#3), gate/remove stdio execution (#1, #2), fix the unredacted config print (#22). These four changes neutralize the "one request owns the host" cluster.
2. **Fix report integrity (the product's actual job):** `detect_capabilities` always-True (#6), `discovered_config` AttributeError (#7), auth-inference soundness (#25), trust-from-LLM-text (#24), pagination flag, HTML protocol list.
3. **OAuth correctness:** `state` + URL-encoding (#4), cache path traversal (#5), blocking `input()` (#34).
4. **Prompt-injection hardening (#23)** and LLM-output robustness (#26, #27, #28).
5. Everything else by severity.
