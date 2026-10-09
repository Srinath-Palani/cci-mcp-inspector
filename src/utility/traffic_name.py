"""
Traffic-name resolution for MCP servers.

The "traffic name" is a real, externally-sourced identifier — never derived
from the URL or guessed. Resolution order (first real hit wins, with the
source recorded so the report shows provenance):

  1. proxy  — your proxy's route name, when a proxy source is configured:
        a. TRAFFIC_NAME_MAP_FILE (JSON/YAML path) or TRAFFIC_NAME_MAP_URL
           (GET returning JSON) mapping endpoint-origin / full URL / repo → name
        b. TRAFFIC_NAME_HEADER (e.g. X-Traffic-Name) read from a response that
           went through your proxy (HTTPS_PROXY / per-server proxy_url)
  2. vendor — the vendor's canonical name:
        a. server.json in the repo root (official MCP registry schema)
        b. the official MCP registry API, matched by endpoint URL or repo
        c. the live handshake's serverInfo.name (always available on success)

Returns (name, source) or (None, None) when nothing real was found — the
caller renders blank/NA rather than a fabricated slug.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests

# Module-level cache for the proxy mapping so a batch of servers pays one fetch.
_MAP_CACHE: Optional[Dict[str, str]] = None
_MAP_LOADED = False


def _origin(url: str) -> str:
    """'https://mcp.atlassian.com/mcp' -> 'mcp.atlassian.com'"""
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _repo_slug(repository: str) -> str:
    """'https://github.com/owner/repo' -> 'owner/repo'"""
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)", (repository or "").strip())
    if not m:
        return ""
    return f"{m.group(1)}/{m.group(2).removesuffix('.git')}".lower()


# ── Method 1a: proxy mapping (file or URL) ───────────────────────────────────

def _load_proxy_map() -> Dict[str, str]:
    """Load the configured proxy mapping once; {} when unconfigured/unreadable."""
    global _MAP_CACHE, _MAP_LOADED
    if _MAP_LOADED:
        return _MAP_CACHE or {}
    _MAP_LOADED = True
    _MAP_CACHE = {}

    map_file = os.getenv("TRAFFIC_NAME_MAP_FILE", "").strip()
    map_url = os.getenv("TRAFFIC_NAME_MAP_URL", "").strip()

    raw: Optional[Dict[str, Any]] = None
    if map_file:
        try:
            text = open(map_file, "r", encoding="utf-8").read()
            raw = json.loads(text)
        except Exception as exc:
            print(f"⚠️  Traffic-name map file unreadable ({map_file}): {exc}")
    elif map_url:
        try:
            resp = requests.get(map_url, timeout=8)
            if resp.status_code == 200:
                raw = resp.json()
        except Exception as exc:
            print(f"⚠️  Traffic-name map URL fetch failed ({map_url}): {exc}")

    if isinstance(raw, dict):
        # Accept {key: name} or {"servers": {key: name}} shapes; keys are
        # matched case-insensitively against endpoint URL / origin / repo slug.
        entries = raw.get("servers") if isinstance(raw.get("servers"), dict) else raw
        _MAP_CACHE = {str(k).strip().lower(): str(v).strip() for k, v in entries.items() if v}
    return _MAP_CACHE


def _from_proxy_map(endpoint_url: Optional[str], repository: Optional[str]) -> Optional[str]:
    mapping = _load_proxy_map()
    if not mapping:
        return None
    candidates = []
    if endpoint_url:
        url = endpoint_url.strip().rstrip("/").lower()
        candidates += [url, _origin(url)]
    if repository:
        repo = repository.strip().rstrip("/").lower()
        candidates += [repo, _repo_slug(repo)]
    for key in candidates:
        if key and key in mapping:
            return mapping[key]
    return None


# ── Method 1b: header from a proxied response ────────────────────────────────

def _from_proxy_header(endpoint_url: Optional[str], proxy_url: Optional[str]) -> Optional[str]:
    """
    Read the traffic name from a response header after routing through the proxy.
    Inert unless TRAFFIC_NAME_HEADER is set AND a proxy is in play (per-server
    proxy_url or HTTPS_PROXY/HTTP_PROXY env).
    """
    header_name = os.getenv("TRAFFIC_NAME_HEADER", "").strip()
    if not header_name or not endpoint_url:
        return None
    effective_proxy = proxy_url or os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if not effective_proxy:
        return None
    try:
        resp = requests.get(
            endpoint_url,
            timeout=8,
            allow_redirects=False,
            proxies={"http": effective_proxy, "https": effective_proxy},
        )
        value = resp.headers.get(header_name)
        if value and value.strip():
            return value.strip()
    except Exception as exc:
        print(f"⚠️  Traffic-name header probe via proxy failed: {exc}")
    return None


# ── Method 2a: server.json in the repo root ──────────────────────────────────

def _from_server_json(repository: Optional[str]) -> Optional[str]:
    slug = _repo_slug(repository or "")
    if not slug:
        return None
    for branch in ("main", "master"):
        url = f"https://raw.githubusercontent.com/{slug}/{branch}/server.json"
        try:
            resp = requests.get(url, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                name = data.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        except Exception:
            continue
    return None


# ── Method 2b: official MCP registry API ─────────────────────────────────────

_REGISTRY = "https://registry.modelcontextprotocol.io/v0/servers"


def _from_registry(endpoint_url: Optional[str], repository: Optional[str]) -> Optional[str]:
    target_urls = set()
    if endpoint_url:
        target_urls.add(endpoint_url.strip().rstrip("/").lower() + "/")
        target_urls.add(endpoint_url.strip().rstrip("/").lower())
    repo = _repo_slug(repository or "")

    # The default listing is capped and won't hold most servers — search by the
    # endpoint's hostname (and repo name) so the registry actually returns the
    # relevant entries. Match on declared remote URL first, then repository.
    queries = []
    if endpoint_url:
        host = _origin(endpoint_url)
        # 'mcp.notion.com' -> 'notion'
        base = host.split(".")[1] if host.startswith("mcp.") and len(host.split(".")) > 2 else host.split(".")[0]
        if base:
            queries.append(base)
    if repo:
        queries.append(repo.split("/")[-1].replace("mcp-server-", "").replace("mcp-", ""))

    for q in dict.fromkeys(queries):  # dedupe, preserve order
        try:
            resp = requests.get(_REGISTRY, params={"search": q, "limit": 50}, timeout=25)
            if resp.status_code != 200:
                continue
            for entry in resp.json().get("servers", []):
                srv = entry.get("server", {})
                for remote in srv.get("remotes", []) or []:
                    rurl = (remote.get("url") or "").strip().lower()
                    if rurl and (rurl in target_urls or rurl.rstrip("/") in target_urls):
                        if srv.get("name"):
                            return srv["name"]
                if repo:
                    r = ((srv.get("repository") or {}).get("url") or "").lower()
                    if repo and repo in r and srv.get("name"):
                        return srv["name"]
        except Exception as exc:
            print(f"⚠️  MCP registry lookup failed: {exc}")
    return None


# ── Entry point ──────────────────────────────────────────────────────────────

async def resolve_traffic_name(
    endpoint_url: Optional[str] = None,
    repository: Optional[str] = None,
    handshake_server_name: Optional[str] = None,
    proxy_url: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve the traffic name. Returns (name, source); both None when nothing
    real was found. Source is one of: proxy_map | proxy_header | server_json |
    registry | handshake.
    """
    import asyncio

    name = await asyncio.to_thread(_from_proxy_map, endpoint_url, repository)
    if name:
        return name, "proxy_map"

    name = await asyncio.to_thread(_from_proxy_header, endpoint_url, proxy_url)
    if name:
        return name, "proxy_header"

    name = await asyncio.to_thread(_from_server_json, repository)
    if name:
        return name, "server_json"

    name = await asyncio.to_thread(_from_registry, endpoint_url, repository)
    if name:
        return name, "registry"

    if handshake_server_name and handshake_server_name.strip():
        return handshake_server_name.strip(), "handshake"

    return None, None
