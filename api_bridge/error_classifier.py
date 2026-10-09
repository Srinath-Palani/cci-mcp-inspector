"""
Deterministic error classifier for the MCP Inspector API bridge.

Maps exceptions and failure results from any inspection stage to a structured
ErrorDetails payload so the frontend can render a typed badge, a human-readable
description, and actionable suggestions instead of a raw exception string.

No LLM is involved — classification is pure pattern matching on exception
types, HTTP status codes, and well-known error strings.
"""

from __future__ import annotations

import asyncio
import re
import socket
import ssl
from typing import Any, Dict, List, Optional

from src.models.structured_output import ErrorDetails, ErrorSuggestion


# Ordered list of (error_type, matcher) — first match wins.
# Matchers receive (message_lower, exc, status_code) and return bool.

def _status(status_code: Optional[int], *codes: int) -> bool:
    return status_code in codes


# HTTP statuses that mean "this endpoint does not speak this MCP transport"
# rather than "this server is broken". Kept in sync with
# src/agents/mcp_discovery_agent.TRANSPORT_MISMATCH_STATUSES.
TRANSPORT_MISMATCH_STATUSES = (400, 405, 406)


def classify_error(
    exc: Optional[BaseException] = None,
    message: str = "",
    stage: str = "unknown",
    status_code: Optional[int] = None,
    missing_env_vars: Optional[List[str]] = None,
    placeholder_env_vars: Optional[List[str]] = None,
    failure_kind: Optional[str] = None,
) -> ErrorDetails:
    """
    Classify a failure into a structured ErrorDetails.

    Args:
        exc: The exception, if one was raised.
        message: Error message (used when no exception, e.g. FAILURE result dicts).
        stage: Pipeline stage where the failure occurred
               (oauth | auth_discovery | mcp_discovery | llm_analysis |
                attribute_extraction | generate_report | config | unknown).
        status_code: HTTP status code when known.
        missing_env_vars: For ENV_VARS_MISSING — names of unresolved variables.
        placeholder_env_vars: For ENV_VARS_PLACEHOLDER — names still holding placeholders.
        failure_kind: Coarse cause from mcp_capability_fetcher.classify_failure
                      ("http_status" | "tls" | "dns" | "connect" | "stream_closed" |
                      "read_timeout" | "unknown"). Preferred over string matching
                      when available, because it is derived from exception types.
    """
    msg = (message or (str(exc) if exc else "")).strip()
    msg_lower = msg.lower()

    # Extract a status code from the message if not supplied.
    # 400/405/406 are included because they are the transport-mismatch statuses:
    # without them, a server that rejects the wrong transport fell through to
    # UNKNOWN with a raw exception string and no suggestion.
    if status_code is None:
        m = re.search(r"\b(400|401|403|404|405|406|408|429|500|502|503|504)\b", msg)
        if m:
            status_code = int(m.group(1))

    # ── Env var errors (explicit, from backend validation) ──────────────────
    if placeholder_env_vars:
        names = ", ".join(placeholder_env_vars)
        return ErrorDetails(
            error_type="ENV_VARS_PLACEHOLDER",
            title="Environment variables contain placeholder values",
            description=(
                f"The following variables still hold placeholder values and were not "
                f"replaced with real credentials: {names}."
            ),
            suggestions=[
                ErrorSuggestion(text=f"Enter a real value for: {names}.", action="fix_env"),
                ErrorSuggestion(text="Placeholder values like 'your_value_here' are ignored by the server and cause authentication failures."),
            ],
            technical_detail=msg or None,
            stage=stage,
            status_code=None,
        )

    if missing_env_vars:
        names = ", ".join(missing_env_vars)
        return ErrorDetails(
            error_type="ENV_VARS_MISSING",
            title="Required environment variables are missing",
            description=(
                f"The server declares these required variables but no value was provided "
                f"in the UI and none was found in the Inspector's environment: {names}."
            ),
            suggestions=[
                ErrorSuggestion(text=f"Fill in a value for: {names}.", action="fix_env"),
                ErrorSuggestion(text="Alternatively, add the variable to the Inspector's .env file and re-run."),
            ],
            technical_detail=msg or None,
            stage=stage,
            status_code=None,
        )

    # ── Exception-type based classification (most reliable) ─────────────────
    if exc is not None:
        if isinstance(exc, asyncio.TimeoutError) or isinstance(exc, TimeoutError):
            return _timeout(msg, stage)
        if isinstance(exc, socket.gaierror):
            return _dns(msg, stage)
        if isinstance(exc, ssl.SSLError):
            return _tls(msg, stage)

    # ── failure_kind based classification ───────────────────────────────────
    # Derived from exception types upstream, so it beats string matching. An
    # explicit HTTP status still wins, since it is more specific than the kind.
    if failure_kind and status_code is None:
        if failure_kind == "stream_closed":
            return _stream_closed(msg, stage)
        if failure_kind == "tls":
            return _tls(msg, stage)
        if failure_kind == "dns":
            return _dns(msg, stage)
        if failure_kind == "read_timeout":
            return _timeout(msg, stage)

    # ── Message/status based classification ─────────────────────────────────
    if _status(status_code, 401) or "unauthorized" in msg_lower:
        # Distinguish "OAuth required" (WWW-Authenticate advertises OAuth) from bad token
        if "oauth" in msg_lower or "authorization_uri" in msg_lower or "no token" in msg_lower:
            return ErrorDetails(
                error_type="OAUTH_REQUIRED",
                title="OAuth authentication required",
                description=(
                    "The MCP server requires OAuth authentication. No valid OAuth token "
                    "was provided with this inspection request."
                ),
                suggestions=[
                    ErrorSuggestion(text="Click 'Authenticate' to complete the OAuth flow through the Inspector backend (works even when the browser is blocked by CORS).", action="oauth_start"),
                    ErrorSuggestion(text="If you already have an access token, paste it into the Bearer Token field instead."),
                ],
                technical_detail=msg or None,
                stage=stage,
                status_code=401,
            )
        return ErrorDetails(
            error_type="AUTH_INVALID_TOKEN",
            title="Authentication token rejected (401)",
            description="The server rejected the provided token. It may be expired, revoked, or malformed.",
            suggestions=[
                ErrorSuggestion(text="Re-authenticate to obtain a fresh token.", action="oauth_start"),
                ErrorSuggestion(text="If using a static API key or bearer token, verify it was copied completely and has not expired."),
            ],
            technical_detail=msg or None,
            stage=stage,
            status_code=401,
        )

    if _status(status_code, 403) or "forbidden" in msg_lower:
        return ErrorDetails(
            error_type="AUTH_FORBIDDEN",
            title="Access forbidden (403)",
            description="The token was accepted but does not grant access to this resource. This is usually a missing scope or workspace permission.",
            suggestions=[
                ErrorSuggestion(text="Check that the token/OAuth grant includes the scopes this server requires."),
                ErrorSuggestion(text="Verify your account has access to the workspace/resources the server exposes."),
            ],
            technical_detail=msg or None,
            stage=stage,
            status_code=403,
        )

    if _status(status_code, 410) or ("gone" in msg_lower and "410" in msg_lower):
        return ErrorDetails(
            error_type="ENDPOINT_DEPRECATED",
            title="Endpoint permanently removed (410 Gone) — deprecated URL",
            description=(
                "The server reports this endpoint is gone for good. The URL is deprecated "
                "and no longer valid — many MCP servers retired their /sse endpoint when "
                "Streamable HTTP (/mcp) replaced HTTP/SSE."
            ),
            suggestions=[
                ErrorSuggestion(text="Check the server's documentation or GitHub repository for the current endpoint URL."),
                ErrorSuggestion(text="If the given URL ends in /sse, try the same host with /mcp — Streamable HTTP replaced the deprecated SSE transport."),
            ],
            technical_detail=msg or None,
            stage=stage,
            status_code=410,
        )

    if _status(status_code, 404) or ("not found" in msg_lower and "404" in msg_lower):
        return ErrorDetails(
            error_type="TRANSPORT_MISMATCH",
            title="Endpoint not found (404) — possible transport mismatch or deprecated URL",
            description=(
                "No MCP endpoint responded at this URL for any attempted transport "
                "(Streamable HTTP and SSE were both tried). The path may be wrong, the "
                "server may use a different endpoint (e.g. /mcp vs /sse), or the endpoint "
                "may be deprecated and no longer valid."
            ),
            suggestions=[
                ErrorSuggestion(text="Verify the endpoint URL — many servers use https://host/mcp (Streamable HTTP) or https://host/sse (SSE)."),
                ErrorSuggestion(text="Check the server's documentation/README for the exact remote endpoint path."),
                ErrorSuggestion(text="If the URL came from an older listing, check the server's GitHub repository — the endpoint may have moved or been retired."),
            ],
            technical_detail=msg or None,
            stage=stage,
            status_code=404,
        )

    if _status(status_code, *TRANSPORT_MISMATCH_STATUSES):
        return _transport_mismatch(msg, stage, status_code)

    if "timed out" in msg_lower or "timeout" in msg_lower:
        return _timeout(msg, stage)

    if "getaddrinfo" in msg_lower or "name or service not known" in msg_lower or "nodename nor servname" in msg_lower:
        return _dns(msg, stage)

    # Deliberately narrow. The previous test matched any message merely
    # containing "ssl" or "tls" — including MCP servers whose own error text
    # mentions TLS — and mislabelled unrelated failures as certificate problems.
    # A genuine TLS failure always carries one of these specific tokens.
    if (
        "certificate verify failed" in msg_lower
        or "certificate_verify_failed" in msg_lower
        or "sslcertverificationerror" in msg_lower
        or "ssl: " in msg_lower
        or "sslerror" in msg_lower
        or "ssleoferror" in msg_lower
        or "handshake failure" in msg_lower
        or "wrong version number" in msg_lower
        or "self signed certificate" in msg_lower
        or "self-signed certificate" in msg_lower
        or "hostname mismatch" in msg_lower
        or "certificate has expired" in msg_lower
    ):
        return _tls(msg, stage)

    # Only when no HTTP status was involved: a 500 whose body happens to mention
    # a closed connection is a server error, not a transport mismatch.
    if status_code is None:
        if "closed" in msg_lower and ("connection" in msg_lower or "stream" in msg_lower or "resource" in msg_lower):
            return _stream_closed(msg, stage)
        if "incomplete" in msg_lower and "read" in msg_lower:
            return _stream_closed(msg, stage)

    if status_code is not None and status_code >= 500:
        return ErrorDetails(
            error_type="SERVER_ERROR",
            title=f"MCP server error ({status_code})",
            description="The MCP server encountered an internal error while handling the request. This is a server-side problem, not a configuration issue.",
            suggestions=[
                ErrorSuggestion(text="Retry the inspection — transient 5xx errors are common.", action="retry"),
                ErrorSuggestion(text="If the error persists, check the server's status page or contact its maintainer."),
            ],
            technical_detail=msg or None,
            stage=stage,
            status_code=status_code,
        )

    if "command required" in msg_lower or "endpoint url required" in msg_lower or "not found in configuration" in msg_lower or "invalid json" in msg_lower:
        return ErrorDetails(
            error_type="CONFIG_INVALID",
            title="Invalid server configuration",
            description=msg or "The server configuration is missing required fields or is malformed.",
            suggestions=[
                ErrorSuggestion(text="For stdio servers: command and args are required."),
                ErrorSuggestion(text="For remote servers: a valid endpoint URL is required."),
            ],
            technical_detail=msg or None,
            stage=stage,
            status_code=None,
        )

    if "connection refused" in msg_lower or "connect call failed" in msg_lower or "cannot connect" in msg_lower:
        return ErrorDetails(
            error_type="CONNECTION_TIMEOUT",
            title="Connection refused",
            description=(
                "Nothing is listening at the target address, or a firewall rejected the "
                "connection. If this URL worked before, the server may have been retired "
                "or the endpoint deprecated."
            ),
            suggestions=[
                ErrorSuggestion(text="Verify the server is running and the URL/port is correct."),
                ErrorSuggestion(text="For stdio servers, check the command starts successfully in a terminal."),
                ErrorSuggestion(text="Check the server's status page or GitHub repository — the endpoint may have been moved or shut down."),
            ],
            technical_detail=msg or None,
            stage=stage,
            status_code=None,
        )

    return ErrorDetails(
        error_type="UNKNOWN",
        title="Inspection failed",
        description=msg or "An unexpected error occurred during inspection.",
        suggestions=[
            ErrorSuggestion(text="Check the technical details below for the underlying cause."),
            ErrorSuggestion(text="Retry the inspection; if it persists, review the server configuration.", action="retry"),
        ],
        technical_detail=msg or None,
        stage=stage,
        status_code=status_code,
    )


def _timeout(msg: str, stage: str) -> ErrorDetails:
    return ErrorDetails(
        error_type="CONNECTION_TIMEOUT",
        title="Connection timed out",
        description="The MCP server did not respond within the time limit. The server may be down, slow, or the URL/transport may be wrong.",
        suggestions=[
            ErrorSuggestion(text="Verify the endpoint URL and that the server is reachable."),
            ErrorSuggestion(text="Try switching transport (Streamable HTTP ↔ SSE)."),
            ErrorSuggestion(text="Retry — remote servers occasionally stall on cold starts.", action="retry"),
        ],
        technical_detail=msg or None,
        stage=stage,
        status_code=None,
    )


def _dns(msg: str, stage: str) -> ErrorDetails:
    return ErrorDetails(
        error_type="DNS_FAILURE",
        title="Hostname could not be resolved",
        description=(
            "DNS lookup failed for the server hostname. The URL is likely misspelled, "
            "the host does not exist, or the endpoint is deprecated and no longer valid."
        ),
        suggestions=[
            ErrorSuggestion(text="Check the endpoint URL for typos in the hostname."),
            ErrorSuggestion(text="Confirm the host resolves: run 'nslookup <hostname>' in a terminal."),
            ErrorSuggestion(text="Check the server's documentation or GitHub repository — the remote endpoint may have moved or been deprecated."),
        ],
        technical_detail=msg or None,
        stage=stage,
        status_code=None,
    )


def _transport_mismatch(msg: str, stage: str, status_code: Optional[int]) -> ErrorDetails:
    """
    400/405/406 from an MCP endpoint almost always mean "this endpoint does not
    speak the transport you used" rather than "your request was malformed":
    a Streamable HTTP POST to a legacy SSE endpoint gets 400/405, and an SSE GET
    to a Streamable-HTTP-only endpoint gets 405/406.
    """
    return ErrorDetails(
        error_type="TRANSPORT_MISMATCH",
        title=f"Server rejected the request ({status_code}) — transport mismatch",
        description=(
            f"The endpoint answered {status_code} to the MCP handshake. For MCP servers this "
            "normally means the endpoint does not speak the transport that was used — for "
            "example a legacy SSE endpoint receiving a Streamable HTTP POST, or a "
            "Streamable-HTTP-only endpoint receiving an SSE GET."
        ),
        suggestions=[
            ErrorSuggestion(text="Switch transport (Streamable HTTP ↔ SSE).", action="switch_transport"),
            ErrorSuggestion(text="Check the endpoint path — SSE servers usually expose /sse, Streamable HTTP servers /mcp."),
            ErrorSuggestion(text="Consult the server's documentation for which transport and path it supports."),
        ],
        technical_detail=msg or None,
        stage=stage,
        status_code=status_code,
    )


def _stream_closed(msg: str, stage: str) -> ErrorDetails:
    return ErrorDetails(
        error_type="TRANSPORT_MISMATCH",
        title="Connection closed before the handshake completed",
        description=(
            "The server closed the connection before completing the MCP handshake. It never "
            "returned an HTTP status, so this is usually a transport mismatch — the endpoint "
            "accepted the TCP/TLS connection but does not speak the MCP transport that was "
            "used. A server-side crash or an upstream proxy dropping the stream can also "
            "produce this."
        ),
        suggestions=[
            ErrorSuggestion(text="Switch transport (Streamable HTTP ↔ SSE).", action="switch_transport"),
            ErrorSuggestion(text="Verify the endpoint path is the MCP endpoint and not the server's landing page."),
            ErrorSuggestion(text="Retry — a dropped stream is sometimes transient.", action="retry"),
        ],
        technical_detail=msg or None,
        stage=stage,
        status_code=None,
    )


def _tls(msg: str, stage: str) -> ErrorDetails:
    return ErrorDetails(
        error_type="TLS_ERROR",
        title="TLS/SSL error",
        description="The secure connection could not be established. The server's certificate may be invalid, expired, or the TLS configuration is incompatible.",
        suggestions=[
            ErrorSuggestion(text="Verify the URL uses the correct scheme (https:// for remote MCP servers)."),
            ErrorSuggestion(text="Check the server certificate: run 'openssl s_client -connect <host>:443' in a terminal."),
        ],
        technical_detail=msg or None,
        stage=stage,
        status_code=None,
    )


def classify_failure_result(result: Dict[str, Any], stage: str) -> ErrorDetails:
    """
    Classify a FAILURE result dict from a discovery/analysis agent
    (the {status: FAILURE, error_message: ...} shape).

    The discovery agent also propagates `status_code` and `failure_kind` (from
    mcp_capability_fetcher.classify_failure). Passing them through means the
    classification comes from the actual exception type rather than from string
    matching on a message that has been reworded several times on its way up.
    """
    message = result.get("error_message") or result.get("error") or "Unknown error"

    status_code = result.get("status_code")
    if not isinstance(status_code, int):
        status_code = None

    failure_kind = result.get("failure_kind")
    if not isinstance(failure_kind, str):
        failure_kind = None

    return classify_error(
        message=message,
        stage=stage,
        status_code=status_code,
        failure_kind=failure_kind,
    )
