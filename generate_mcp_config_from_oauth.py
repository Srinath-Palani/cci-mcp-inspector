#!/usr/bin/env python3
"""
Generate MCP Server Configuration from OAuth Tokens

This script reads a JSON file containing OAuth tokens and generates
example_servers.json configuration file with proper npm package detection.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

try:
    import requests
    import base64
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    print("⚠️  Warning: 'requests' library not found. SSE endpoint testing will be skipped.")


def extract_github_repo_info(repo_url: str) -> Optional[Dict[str, str]]:
    """
    Extract GitHub owner and repo name from URL.
    
    Args:
        repo_url: Repository URL (e.g., "https://github.com/box-community/mcp-server-box")
        
    Returns:
        Dictionary with 'owner' and 'repo' keys, or None if not a GitHub URL
    """
    if not repo_url:
        return None
    
    # Handle different GitHub URL formats
        # Handle different GitHub URL formats, including subdirectories
        # Examples:
        #   https://github.com/owner/repo
        #   https://github.com/owner/repo/tree/branch/subdir
        #   https://github.com/owner/repo/blob/branch/subdir/README.md
    patterns = [
            r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?(?:/tree/([^/]+)(/.+))?/?$",
        r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, repo_url)
        if match:
            return {
                "owner": match.group(1),
                "repo": match.group(2).rstrip('/'),
                    "branch": match.group(3) if len(match.groups()) > 2 else None,
                    "subdir": match.group(4) if len(match.groups()) > 3 else None,
                "url": repo_url
            }
    
    return None


def fetch_github_file(owner: str, repo: str, file_path: str, branch: str = "main") -> Optional[str]:
    """
    Fetch a file from GitHub repository.
    
    Args:
        owner: GitHub repository owner
        repo: Repository name
        file_path: Path to file (e.g., "package.json", "README.md")
        branch: Branch name (default: "main")
        
    Returns:
        File content as string, or None if not found
    """
    if not HAS_REQUESTS:
        return None
    
    # Try raw.githubusercontent.com first (simpler, no API needed)
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.text
        elif response.status_code == 404:
            # Try other common branches
            for alt_branch in ["main", "master", "develop"]:
                if alt_branch == branch:
                    continue
                alt_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{alt_branch}/{file_path}"
                try:
                    alt_response = requests.get(alt_url, timeout=10)
                    if alt_response.status_code == 200:
                        return alt_response.text
                except Exception:
                    continue
    except Exception as e:
        # Silently continue to API fallback
        pass
    
    # Fallback: Try GitHub API (supports both main and master branches)
    for try_branch in [branch, "main", "master"]:
        try:
            api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{file_path}"
            headers = {"Accept": "application/vnd.github.v3.raw"}
            
            # Add GitHub token if available (for rate limits)
            github_token = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN")
            if github_token:
                headers["Authorization"] = f"token {github_token}"
            
            response = requests.get(api_url, headers=headers, params={"ref": try_branch}, timeout=10)
            if response.status_code == 200:
                # API returns JSON with base64 encoded content
                file_data = response.json()
                if "content" in file_data:
                    content = base64.b64decode(file_data["content"]).decode('utf-8')
                    return content
        except Exception:
            continue
    
    return None


def parse_package_json_from_github(repo_url: str) -> Optional[Dict[str, Any]]:
    """
    Fetch and parse package.json from GitHub repository to extract command, args, and env vars.
    
    Args:
        repo_url: GitHub repository URL
        
    Returns:
        Dictionary with:
        - command: Command to run (e.g., "node", "npx", "npm")
        - args: List of arguments
        - env_vars: List of detected environment variable names
        - package_name: npm package name if found
        - bin_entry: bin entry from package.json if found
    """
    if not HAS_REQUESTS:
        return None
    
    repo_info = extract_github_repo_info(repo_url)
    if not repo_info:
        return None
    
    owner = repo_info["owner"]
    repo = repo_info["repo"]
    
    # Fetch package.json
    package_json_content = fetch_github_file(owner, repo, "package.json")
    if not package_json_content:
        return None
    
    try:
        package_data = json.loads(package_json_content)
    except json.JSONDecodeError:
        return None
    
    result = {
        "command": None,
        "args": [],
        "env_vars": [],
        "package_name": package_data.get("name"),
        "bin_entry": None
    }
    
    # Extract bin entry (executable command)
    bin_data = package_data.get("bin", {})
    if isinstance(bin_data, dict) and bin_data:
        # Get the first bin entry
        bin_name = list(bin_data.keys())[0]
        bin_path = bin_data[bin_name]
        result["bin_entry"] = bin_name
        # If bin path is a JS file, use node to run it
        if bin_path.endswith(".js") or bin_path.endswith(".mjs"):
            result["command"] = "node"
            result["args"] = [bin_path]
        else:
            # Otherwise, assume it's executable directly
            result["command"] = bin_name
    elif isinstance(bin_data, str):
        # Single bin entry as string
        result["bin_entry"] = bin_data
        if bin_data.endswith(".js") or bin_data.endswith(".mjs"):
            result["command"] = "node"
            result["args"] = [bin_data]
        else:
            result["command"] = bin_data
    
    # Extract scripts that might indicate how to run the server
    scripts = package_data.get("scripts", {})
    for script_name, script_cmd in scripts.items():
        if "mcp" in script_name.lower() or "server" in script_name.lower() or "start" in script_name.lower():
            # Parse script command
            script_parts = script_cmd.split()
            if script_parts:
                if script_parts[0] == "node":
                    result["command"] = "node"
                    result["args"] = script_parts[1:] if len(script_parts) > 1 else []
                elif script_parts[0] == "tsx" or script_parts[0] == "ts-node":
                    result["command"] = "npx"
                    result["args"] = ["-y", script_parts[0]] + (script_parts[1:] if len(script_parts) > 1 else [])
                break
    
    # Extract environment variables from package.json
    # Check in various places where env vars might be documented
    package_str = json.dumps(package_data)
    # Look for common env var patterns
    env_patterns = [
        r'process\.env\.([A-Z_][A-Z0-9_]*)',
        r'\$\{([A-Z_][A-Z0-9_]*)\}',
        r'([A-Z_][A-Z0-9_]*)_(?:API_KEY|TOKEN|ACCESS_TOKEN|DEV_TOKEN|CLIENT_ID|CLIENT_SECRET)',
    ]
    for pattern in env_patterns:
        matches = re.findall(pattern, package_str)
        result["env_vars"].extend(matches)
    
    # Remove duplicates
    result["env_vars"] = list(set(result["env_vars"]))
    
    return result


def extract_env_vars_from_readme(readme_content: str, server_name: str) -> List[str]:
    """
    Extract environment variable names from README content.
    
    Args:
        readme_content: README file content
        server_name: Server name for pattern matching
        
    Returns:
        List of environment variable names found
    """
    if not readme_content:
        return []
    
    env_vars = []
    server_name_upper = server_name.upper().replace("-", "_").replace(" ", "_")
    
    # Pattern 1: process.env.VAR_NAME
    pattern1 = rf'process\.env\.({server_name_upper}_[A-Z0-9_]+)'
    matches = re.findall(pattern1, readme_content, re.IGNORECASE)
    env_vars.extend(matches)
    
    # Pattern 2: ${VAR_NAME} or $VAR_NAME
    pattern2 = rf'\$\{{?({server_name_upper}_[A-Z0-9_]+)\}}?'
    matches = re.findall(pattern2, readme_content, re.IGNORECASE)
    env_vars.extend(matches)
    
    # Pattern 3: VAR_NAME in code blocks or examples
    pattern3 = rf'\b({server_name_upper}_(?:API_KEY|TOKEN|ACCESS_TOKEN|DEV_TOKEN|CLIENT_ID|CLIENT_SECRET|SECRET))\b'
    matches = re.findall(pattern3, readme_content, re.IGNORECASE)
    env_vars.extend(matches)
    
    # Pattern 4: Generic env var patterns (VAR_NAME=value or export VAR_NAME)
    pattern4 = r'(?:export\s+)?([A-Z][A-Z0-9_]{2,})\s*='
    matches = re.findall(pattern4, readme_content)
    # Filter for relevant env vars (containing TOKEN, KEY, SECRET, API, etc.)
    relevant_vars = [m for m in matches if any(keyword in m.upper() for keyword in ['TOKEN', 'KEY', 'SECRET', 'API', 'AUTH', 'CLIENT', 'ID'])]
    env_vars.extend(relevant_vars)
    
    # Pattern 5: Extract env vars from JSON config blocks (e.g., MCP server configs)
    # Look for "env": { "VAR_NAME": "value" } patterns in code blocks
    json_block_pattern = r'```(?:json|typescript|ts|javascript|js)\s*\n([\s\S]*?)\n```'
    json_blocks = re.findall(json_block_pattern, readme_content, re.IGNORECASE)
    
    for block in json_blocks:
        block = block.strip()
        if not block:
            continue
        
        # Try to parse as JSON
        config_json = None
        try:
            config_json = json.loads(block)
        except json.JSONDecodeError:
            try:
                config_json = json.loads("{" + block + "}")
            except json.JSONDecodeError:
                m = re.search(r'\{.*\}', block, re.DOTALL)
                if m:
                    try:
                        config_json = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        continue
        
        if config_json:
            # Recursively find all "env" blocks and extract keys
            # Note: If a var is in an "env" block in JSON config, it's meant to be an env var
            # so we include ALL keys from env blocks (no keyword filtering needed)
            def extract_env_keys(obj):
                keys = []
                if isinstance(obj, dict):
                    if "env" in obj and isinstance(obj["env"], dict):
                        # Include all keys from env blocks - they're explicitly env vars
                        for key in obj["env"].keys():
                            keys.append(key.upper())
                    for value in obj.values():
                        keys.extend(extract_env_keys(value))
                elif isinstance(obj, list):
                    for item in obj:
                        keys.extend(extract_env_keys(item))
                return keys
            
            env_vars.extend(extract_env_keys(config_json))
    
    # Remove duplicates and return
    return list(set([var.upper() for var in env_vars]))


def parse_command_args_from_readme(readme_content: str, server_name: str) -> Optional[Dict[str, Any]]:
    result = {
        "command": None,
        "args": [],
        "package_name": None,
        "env_block": None
    }

    # Pattern 0: JSON config block
    # Capture the entire content of fenced code blocks that might contain MCP config.
    # Look for code fences with json/js/ts language tags
    json_block_pattern = r'```(?:json|typescript|ts|javascript|js)\s*\n([\s\S]*?)\n```'
    json_blocks = re.findall(json_block_pattern, readme_content, re.IGNORECASE)
    
    # Collect all valid configs - we'll prioritize ones with env blocks
    all_configs = []  # List of (has_env, config_dict) tuples
    
    for block in json_blocks:
        block = block.strip()
        if not block:
            continue
            
        config_json = None
        try:
            config_json = json.loads(block)
        except json.JSONDecodeError:
            try:
                # Try wrapping in braces if it's a snippet (like "mcpServers": { ... })
                config_json = json.loads("{" + block + "}")
            except json.JSONDecodeError:
                # Try extracting the first JSON object inside the block (ignore trailing text)
                m = re.search(r'\{.*\}', block, re.DOTALL)
                if m:
                    try:
                        config_json = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        continue
                else:
                    continue

        if not config_json:
            continue

        # Look for config either at root or nested under mcpServers
        configs_to_check = []
        if isinstance(config_json, dict):
            configs_to_check.append(config_json)
            if "mcpServers" in config_json and isinstance(config_json["mcpServers"], dict):
                configs_to_check.extend(config_json["mcpServers"].values())
        elif isinstance(config_json, list):
            configs_to_check.extend([item for item in config_json if isinstance(item, dict)])

        for cfg in configs_to_check:
            if isinstance(cfg, dict) and cfg.get("command") in ["uvx", "npx"]:
                has_env = cfg.get("env") is not None and isinstance(cfg.get("env"), dict) and len(cfg.get("env", {})) > 0
                all_configs.append((has_env, cfg))
    
    # Priority 1: Return first config with BOTH command AND env
    for has_env, cfg in all_configs:
        if has_env:
            result["command"] = cfg["command"]
            result["args"] = cfg.get("args", [])
            result["package_name"] = None
            result["env_block"] = cfg.get("env")
            return result
    
    # Priority 2 (fallback): Return first config with just command (no env)
    for has_env, cfg in all_configs:
        if not has_env:
            result["command"] = cfg["command"]
            result["args"] = cfg.get("args", [])
            result["package_name"] = None
            result["env_block"] = None
            return result
    """
    Parse README content to extract command and args for stdio connection.
    
    Looks for common patterns like:
    - npx @package/name
    - npx -y package-name
    - npm install -g package then package-name
    - Usage examples with command line arguments
    
    Args:
        readme_content: README file content
        server_name: Server name for pattern matching
        
    Returns:
        Dictionary with command, args, and package_name, or None if not found
    """
    if not readme_content:
        return None
    
    result = {
        "command": None,
        "args": [],
        "package_name": None,
        "env_block": None
    }
    
    # Pattern 1: npx usage examples in README (safe scanning; supports inline usage too)
    # NOTE: Avoid a single giant DOTALL regex over the entire README (can hang on large files).
    def _extract_npx_from_text(text: str) -> Optional[Dict[str, Any]]:
        """Extract an npx invocation from a given text chunk (code block or inline text)."""
        if not text:
            return None

        # Match: npx [-y] <package> [extra args ...]
        # - package supports scoped (@org/name) and common unscoped patterns
        # - extra args limited to same line (inline usage) or within the same fenced block chunk
        npx_cmd_regex = re.compile(
            r'\bnpx\b\s+(?P<rest>[^\n`]+)',
            re.IGNORECASE,
        )
        pkg_regex = re.compile(r'(@[\w-]+/[\w-]+|[\w-]+-mcp-server|[\w-]+-mcp|[\w-]+)')

        for m in npx_cmd_regex.finditer(text):
            rest = m.group('rest').strip()
            if not rest:
                continue

            # Normalize whitespace
            rest_flat = ' '.join(rest.split())

            # Find the package specifier by skipping leading flags (tokens that start
            # with '-').  pkg_regex uses [\w-]+ which matches '-y' as a valid token,
            # so searching rest_flat directly would pick up -y as the "package name"
            # and produce args like ["-y", "-y", "@pkg@latest"].
            tokens = rest_flat.split()
            package_specifier: Optional[str] = None
            extra_tokens: List[str] = []
            for i, token in enumerate(tokens):
                if not token.startswith('-'):
                    package_specifier = token
                    extra_tokens = tokens[i + 1:]
                    break

            if not package_specifier:
                continue

            # Validate it looks like an npm package name (not an unrelated word)
            if not pkg_regex.search(package_specifier):
                continue

            # Always emit exactly one -y; the full specifier (including @version tag
            # like @latest or @1.2.3) is preserved as a single token.
            args_list: List[str] = ['-y', package_specifier]
            if extra_tokens:
                args_list.extend(extra_tokens)

            # Env extraction: look for VAR=VALUE lines in the same chunk
            env_block_pattern = r'(SCENARIO_ID|API_KEY|[A-Z_]+)\s*=.*'
            env_lines = [line for line in text.splitlines() if re.match(env_block_pattern, line.strip())]
            env_dict = None
            if env_lines:
                env_dict = {}
                for line in env_lines:
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        env_dict[parts[0].strip()] = parts[1].strip()

            return {
                'command': 'npx',
                'args': args_list,
                'package_name': package_specifier,
                'env_block': env_dict,
            }

        return None

    # 1a) Prefer fenced code blocks first (most reliable)
    fenced_block_pattern = r'```[a-zA-Z]*[ \t]*\n(.*?)```'
    fenced_blocks = re.findall(fenced_block_pattern, readme_content, re.DOTALL)
    for block in fenced_blocks:
        found = _extract_npx_from_text(block)
        if found:
            result.update(found)
            return result

    # 1b) Then allow inline usage (e.g., README prose like klavis)
    found = _extract_npx_from_text(readme_content)
    if found:
        result.update(found)
        return result
    

    # Pattern 2: uvx command (prefer if npx not found)
    uvx_block_pattern = r'(```[a-zA-Z]*[ \t]*\n)?(.*?uvx\s+([\w./@-]+)(.*?))(```|$)'
    uvx_matches = re.findall(uvx_block_pattern, readme_content, re.DOTALL | re.IGNORECASE)
    if uvx_matches:
        block_start, block_content, uvx_cmd, uvx_args, block_end = uvx_matches[0]
        result["command"] = "uvx"
        
        # Remove comments (anything after #)
        if '#' in uvx_args:
            uvx_args = uvx_args.split('#', 1)[0]
            
        # Split args by spaces, handle quotes
        args = re.findall(r'(?:^|\s)(["\']?)([^\s"\']+)\1', uvx_args)
        result["args"] = [uvx_cmd] + ([arg[1] for arg in args] if args else uvx_args.split())
        result["package_name"] = None
        # Try to extract env block from the same code block
        env_block_pattern = r'(SCENARIO_ID|API_KEY|[A-Z_]+)\s*=.*'
        env_lines = [line for line in block_content.splitlines() if re.match(env_block_pattern, line.strip())]
        if env_lines:
            env_dict = {}
            for line in env_lines:
                parts = line.split('=', 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = parts[1].strip()
                    env_dict[key] = value
            result["env_block"] = env_dict
        return result

    # 2b. JSON config block: "command": "uvx", "args": [ ... ]
    # Find JSON blocks with "command": "uvx" and extract args
    json_uvx_pattern = r'"command"\s*:\s*"uvx"\s*,\s*"args"\s*:\s*\[(.*?)\]'
    json_uvx_matches = re.findall(json_uvx_pattern, readme_content, re.DOTALL | re.IGNORECASE)
    if json_uvx_matches:
        # Extract args as a list of strings
        args_str = json_uvx_matches[0]
        # Match quoted strings (handles both single and double quotes)
        arg_items = re.findall(r'"([^"]+)"|\'([^\']+)\'', args_str)
        args = [item[0] if item[0] else item[1] for item in arg_items]
        result["command"] = "uvx"
        result["args"] = args
        result["package_name"] = None
        return result

    # Do not fallback to package.json or other heuristics unless no npx usage is found in the README
    
    # Pattern 4: Direct command in code blocks (e.g., `node index.js` or `python main.py`)
    # Only use this if no package name was found (fallback for non-npm packages)
    # Look for code blocks with executable commands
    code_block_pattern = r'```(?:bash|sh|shell|console)?\s*\n\s*([\w-]+)\s+([^\n`]+)'
    code_matches = re.findall(code_block_pattern, readme_content, re.IGNORECASE | re.MULTILINE)
    for cmd, args_str in code_matches:
        # Check if it's a relevant command (not just generic commands)
        # Skip npx/npm here since we already checked for package names above
        if cmd in ['node', 'python', 'python3'] or (server_name.lower() in cmd.lower() and cmd not in ['npx', 'npm']):
            result["command"] = cmd
            # Parse args (split by spaces, handle quotes)
            args = re.findall(r'(?:^|\s)(["\']?)([^\s"\']+)\1', args_str)
            result["args"] = [arg[1] for arg in args] if args else args_str.split()
            if result["args"]:
                return result
    
    return None


def extract_env_vars_from_github(repo_url: str, server_name: str) -> List[str]:
    """
    Extract environment variable names from GitHub repository (README and package.json).
    Works for both SSE and stdio connections.
    
    Args:
        repo_url: GitHub repository URL
        server_name: Server name for pattern matching
        
    Returns:
        List of environment variable names found, or empty list if none found
    """
    if not HAS_REQUESTS:
        return []
    
    repo_info = extract_github_repo_info(repo_url)
    if not repo_info:
        return []
    
    owner = repo_info["owner"]
    repo = repo_info["repo"]
    
    env_vars = []
    
    # Step 1: Try to get env vars from package.json
    github_config = parse_package_json_from_github(repo_url)
    if github_config:
        env_vars.extend(github_config.get("env_vars", []))
    
    # Step 2: Fetch README to extract env vars
    readme_content = None
    for readme_file in ["README.md", "README", "readme.md", "readme"]:
        readme_content = fetch_github_file(owner, repo, readme_file)
        if readme_content:
            break
    
    if readme_content:
        readme_env_vars = extract_env_vars_from_readme(readme_content, server_name)
        env_vars.extend(readme_env_vars)
    
    # Remove duplicates and return
    return list(set([var.upper() for var in env_vars]))


def parse_github_repository_for_stdio(repo_url: str, server_name: str) -> Optional[Dict[str, Any]]:
    """
    Parse GitHub repository to extract stdio connection configuration.
    
    Args:
        repo_url: GitHub repository URL
        server_name: Server name for env var detection
        
    Returns:
        Dictionary with command, args, and env_vars, or None if not found
    """
    if not HAS_REQUESTS:
        return None
    
    repo_info = extract_github_repo_info(repo_url)
    if not repo_info:
        return None

    owner = repo_info["owner"]
    repo = repo_info["repo"]
    subdir = repo_info.get("subdir")

    print(f"   🔍 Checking GitHub repository: {owner}/{repo}{subdir or ''}")

    # Step 1: Try to get README from the subdirectory if present
    readme_content = None
    readme_files = ["README.md", "README", "readme.md", "readme"]
    if subdir:
        for readme_file in readme_files:
            subdir_readme_path = f"{subdir.lstrip('/')}/{readme_file}"
            readme_content = fetch_github_file(owner, repo, subdir_readme_path)
            if readme_content:
                break
    # Fallback: try root if subdir README not found
    if not readme_content:
        for readme_file in readme_files:
            readme_content = fetch_github_file(owner, repo, readme_file)
            if readme_content:
                break

    # Step 2: Only use npx usage examples from the subdirectory README (if found)
    readme_config = None
    if readme_content:
        readme_config = parse_command_args_from_readme(readme_content, server_name)
        readme_env_vars = extract_env_vars_from_readme(readme_content, server_name)
    else:
        readme_env_vars = []

    # Only use command/args from subdirectory README, do not fallback to package.json or repo name
    if readme_config and readme_config.get("command") == "npx":
        github_config = readme_config
        github_config["env_vars"] = readme_env_vars if readme_env_vars else []
        print(f"   ✅ Found npx configuration in subdirectory README: command={github_config.get('command')}, args={github_config.get('args')}")
        return github_config

    # If no npx usage found in subdirectory README, fallback to uvx if found
    if readme_config and readme_config.get("command") == "uvx":
        github_config = readme_config
        github_config["env_vars"] = readme_env_vars if readme_env_vars else []
        print(f"   ✅ Found uvx configuration in subdirectory README: command={github_config.get('command')}, args={github_config.get('args')}")
        return github_config

    # If neither npx nor uvx usage found in subdirectory README, do not fallback to npm/package.json heuristics
    print(f"   ⚠️  No npx or uvx usage found in subdirectory README for {repo}{subdir or ''}")
    # Fallback logic is intentionally disabled/commented out as per user request
    # If you want to enable fallback to npm/package.json heuristics, uncomment below:
    # ...existing fallback logic...
    return None


def verify_npm_package_executable(package_name: str) -> bool:
    """
    Verify that an npm package has an executable/bin entry that can be run.
    
    Only packages with a 'bin' entry can be executed via npx. Packages with only
    a 'main' entry are libraries, not executables.
    
    Args:
        package_name: Name of the npm package (e.g., "@canva/cli")
        
    Returns:
        True if package has an executable/bin entry, False otherwise
    """
    try:
        # Check if package has a bin entry (required for npx execution)
        result = subprocess.run(
            ["npm", "view", package_name, "bin", "--json"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            bin_output = result.stdout.strip()
            # If bin is null, empty, or "null", package has no executable
            if not bin_output or bin_output == "null" or bin_output == "{}":
                return False
            # Try to parse as JSON to see if it has entries
            try:
                bin_data = json.loads(bin_output)
                if isinstance(bin_data, dict) and bin_data:
                    # Has bin entries - can be executed
                    return True
                elif isinstance(bin_data, str) and bin_data:
                    # Single bin entry as string - can be executed
                    return True
            except json.JSONDecodeError:
                # If it's not valid JSON but not null/empty, check if it looks like a bin entry
                if bin_output and len(bin_output) > 2 and bin_output != "null":
                    return True
        # No bin entry found - cannot be executed via npx
        return False
    except (subprocess.TimeoutExpired, ValueError, subprocess.SubprocessError):
        # If we can't verify (network error, etc.), be conservative and return False
        # This will prefer SSE if available, which is safer
        return False


def detect_package_args(package_name: str) -> List[str]:
    """
    Detect if an npm package needs special arguments to run as MCP server.
    
    This function checks package metadata to determine if it's a CLI tool
    that needs a subcommand (like "mcp") or a dedicated MCP server.
    
    Args:
        package_name: Name of the npm package (e.g., "@canva/cli")
        
    Returns:
        List of additional arguments needed, or empty list if none
    """
    try:
        # Get package description
        result = subprocess.run(
            ["npm", "view", package_name, "description", "--json"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            description = result.stdout.strip().strip('"').lower()
            package_name_lower = package_name.lower()
            
            # Check if it's a CLI tool that might need a subcommand
            is_cli_tool = (
                "cli" in package_name_lower or
                "cli" in description or
                "command-line" in description or
                "command line" in description
            )
            
            # Check if it's explicitly an MCP server
            is_mcp_server = (
                "mcp server" in description or
                "mcp-server" in package_name_lower or
                "/mcp" in package_name_lower
            )
            
            # If it's a CLI tool but not explicitly an MCP server, it likely needs "mcp" subcommand
            if is_cli_tool and not is_mcp_server:
                return ["mcp"]
        
    except (subprocess.TimeoutExpired, ValueError, subprocess.SubprocessError):
        # If we can't determine, fall back to checking package name
        if "cli" in package_name.lower() and "mcp" not in package_name.lower():
            return ["mcp"]
    
    return []


def search_npm_package(server_name: str) -> Optional[Dict[str, Any]]:
    """
    Search npm for MCP server package using dynamic pattern matching.
    
    Args:
        server_name: Name of the server (e.g., "canva", "linear")
        
    Returns:
        Dictionary with package info (name, version, and optional args) or None if not found
    """
    # Dynamic patterns based on common npm package naming conventions
    # Ordered by likelihood (most common patterns first)
    all_patterns = [
        f"@{server_name}/mcp-server",
        f"@{server_name}/mcp",
        f"@{server_name}/cli",  # For CLI tools like @canva/cli
        # Model Context Protocol organization
        f"@modelcontextprotocol/server-{server_name}",
        # Third-party MCP packages
        f"@tacticlaunch/mcp-{server_name}",
        f"@notionhq/{server_name}-mcp-server",
        # Unscoped packages
        f"{server_name}-mcp-server",
        f"mcp-server-{server_name}",
        f"{server_name}-mcp",
    ]
    
    for pattern in all_patterns:
        try:
            # Try npm view first (faster and more reliable)
            result = subprocess.run(
                ["npm", "view", pattern, "name", "--json"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                package_name = result.stdout.strip().strip('"')
                if package_name and package_name != "null":
                    # Verify package has an executable before using it
                    has_executable = verify_npm_package_executable(package_name)
                    if has_executable:
                        # Detect if package needs special arguments
                        extra_args = detect_package_args(package_name)
                        return {
                            "package_name": package_name,
                            "version": "latest",
                            "extra_args": extra_args,
                            "has_executable": True
                        }
                    else:
                        print(f"   ⚠️  Package {package_name} found but has no executable/bin entry")
                        # Return None to indicate package exists but can't be used
                        return None
        except (subprocess.TimeoutExpired, ValueError, subprocess.SubprocessError):
            continue
    
    # Fallback: try npm search
    for pattern in all_patterns[:3]:  # Limit to first 3 to avoid timeout
        try:
            result = subprocess.run(
                ["npm", "search", pattern, "--json"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0:
                packages = json.loads(result.stdout)
                if packages:
                    # Return first matching package
                    package = packages[0] if isinstance(packages, list) else packages
                    if isinstance(package, dict) and "name" in package:
                        package_name = package["name"]
                        # Verify package has an executable before using it
                        has_executable = verify_npm_package_executable(package_name)
                        if has_executable:
                            # Detect if package needs special arguments
                            extra_args = detect_package_args(package_name)
                            return {
                                "package_name": package_name,
                                "version": package.get("version", "latest"),
                                "extra_args": extra_args,
                                "has_executable": True
                            }
                        else:
                            print(f"   ⚠️  Package {package_name} found but has no executable/bin entry")
                            # Continue searching for other packages
                            continue
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, subprocess.SubprocessError):
            continue
    
    return None


def test_sse_endpoint(mcp_server_url: str, access_token: str) -> bool:
    """
    Test if SSE endpoint is accessible with OAuth token.
    
    Uses SSE-specific headers to more accurately detect if the endpoint
    actually supports Server-Sent Events connections.
    
    Args:
        mcp_server_url: URL of the MCP server
        access_token: OAuth access token
        
    Returns:
        True if SSE endpoint is accessible and supports SSE, False otherwise
    """
    if not HAS_REQUESTS or not mcp_server_url or not access_token:
        return False
    
    try:
        # Use SSE-specific headers to test if endpoint actually supports SSE
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'text/event-stream',
            'Cache-Control': 'no-cache'
        }
        
        # Use stream=True to test SSE connection without reading the stream
        response = requests.get(mcp_server_url, headers=headers, timeout=5, stream=True)
        
        status_code = response.status_code
        
        # Check response headers for SSE indicators
        content_type = response.headers.get('Content-Type', '').lower()
        is_sse_content_type = 'text/event-stream' in content_type
        
        # 405 Method Not Allowed - definitely doesn't support SSE
        if status_code == 405:
            return False
        
        # 200 OK with SSE content type - definitely supports SSE
        if status_code == 200 and is_sse_content_type:
            return True
        
        # 200 OK but no SSE content type - might support SSE but need to check further
        # Try OPTIONS to see what methods are allowed
        if status_code == 200:
            try:
                options_headers = {
                    'Authorization': f'Bearer {access_token}',
                    'Accept': 'text/event-stream'
                }
                options_response = requests.options(mcp_server_url, headers=options_headers, timeout=5)
                # Check if GET method is explicitly allowed
                allowed_methods = options_response.headers.get('Allow', '').upper()
                if 'GET' not in allowed_methods and options_response.status_code != 405:
                    # GET not in allowed methods - SSE likely won't work
                    return False
                # If OPTIONS returns 405, the endpoint might not support SSE properly
                if options_response.status_code == 405:
                    return False
                # Try to read a small chunk from the stream to see if it's actually SSE
                # If we can't read or it's not SSE format, it might not be a real SSE endpoint
                try:
                    # Close the stream connection
                    response.close()
                except:
                    pass
                # If we got here, assume it might work but be conservative
                return True
            except Exception:
                # If OPTIONS fails, be conservative - return False to prefer stdio
                return False
        
        # 401/403 - Auth issue but endpoint might exist
        # However, if we get these with SSE headers, it's unclear
        # Be conservative and return False to prefer stdio
        if status_code in [401, 403]:
            return False
        
        # Other status codes - assume SSE won't work
        return False
            
    except requests.exceptions.RequestException as e:
        # Network error or timeout - assume SSE won't work
        return False
    except Exception:
        return False


def detect_expected_env_var_name(server_name: str, npm_package: Optional[Dict[str, Any]] = None, var_type: str = "token") -> str:
    """
    Detect the expected environment variable name for an MCP server.
    
    Tries multiple methods in order:
    1. Check npm package README/documentation for env var references
    2. Check npm package.json for env var hints
    3. Fall back to pattern-based generation
    
    Args:
        server_name: Name of the server (e.g., "box", "huggingface")
        npm_package: Optional npm package info dict
        var_type: Type of variable ("token", "client_id", "client_secret", "api_key", "dev_token")
        
    Returns:
        Environment variable name (e.g., "BOX_DEV_TOKEN", "HUGGINGFACE_API_KEY")
    """
    server_name_upper = server_name.upper().replace("-", "_").replace(" ", "_")
    server_name_lower = server_name.lower().replace("-", "_").replace(" ", "_")
    
    # Try to detect from npm package documentation
    if npm_package:
        package_name = npm_package.get("package_name", "")
        
        # Method 1: Check README for environment variable references
        detected_var = _detect_env_var_from_readme(package_name, server_name_upper, var_type)
        if detected_var:
            return detected_var
        
        # Method 2: Check package.json for env var hints in scripts or config
        detected_var = _detect_env_var_from_package_json(package_name, server_name_upper, var_type)
        if detected_var:
            return detected_var
    
    # Fall back to pattern-based generation
    return determine_env_var_name(server_name, var_type)


def _detect_env_var_from_readme(package_name: str, server_name_upper: str, var_type: str) -> Optional[str]:
    """
    Detect environment variable name from npm package README.
    
    Args:
        package_name: npm package name
        server_name_upper: Server name in uppercase (e.g., "HUGGINGFACE")
        var_type: Type of variable ("token", "client_id", etc.)
        
    Returns:
        Detected env var name or None
    """
    try:
        # Get README from npm
        result = subprocess.run(
            ["npm", "view", package_name, "readme", "--json"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            readme = result.stdout.strip().strip('"')
            if not readme:
                return None
            
            # Normalize readme (handle escaped strings)
            readme = readme.replace('\\n', '\n').replace('\\"', '"')
            
            # Build comprehensive search patterns
            # Pattern 1: Direct env var references like process.env.HUGGINGFACE_API_KEY
            process_env_pattern = rf"process\.env\.({server_name_upper}_[A-Z_]+)"
            matches = re.findall(process_env_pattern, readme, re.IGNORECASE)
            if matches:
                # Filter matches that are relevant to our var_type
                for match in matches:
                    match_upper = match.upper()
                    if _is_relevant_env_var(match_upper, var_type):
                        return match_upper
            
            # Pattern 2: Environment variable declarations/descriptions
            # Look for patterns like: HUGGINGFACE_API_KEY, ${HUGGINGFACE_API_KEY}, etc.
            env_var_patterns = [
                rf"\b({server_name_upper}_API_KEY)\b",
                rf"\b({server_name_upper}_ACCESS_TOKEN)\b",
                rf"\b({server_name_upper}_DEV_TOKEN)\b",
                rf"\b({server_name_upper}_TOKEN)\b",
                rf"\$\{{{server_name_upper}_[A-Z_]+\}}",
                rf"env\[['\"]?({server_name_upper}_[A-Z_]+)['\"]?\]",
            ]
            
            for pattern in env_var_patterns:
                matches = re.findall(pattern, readme, re.IGNORECASE)
                if matches:
                    for match in matches:
                        if isinstance(match, tuple):
                            match = match[0]
                        match_upper = match.upper().replace('${', '').replace('}', '')
                        if _is_relevant_env_var(match_upper, var_type):
                            return match_upper
            
            # Pattern 3: Look for configuration examples or code blocks
            # Search in markdown code blocks for env var usage
            code_block_pattern = rf"```(?:javascript|typescript|bash|sh|json|yaml|yml|env)?\n.*?({server_name_upper}_[A-Z_]+).*?\n```"
            matches = re.findall(code_block_pattern, readme, re.IGNORECASE | re.DOTALL)
            if matches:
                for match in matches:
                    match_upper = match.upper()
                    if _is_relevant_env_var(match_upper, var_type):
                        return match_upper
                        
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, Exception):
        pass
    
    return None


def _detect_env_var_from_package_json(package_name: str, server_name_upper: str, var_type: str) -> Optional[str]:
    """
    Detect environment variable name from npm package.json metadata.
    
    Args:
        package_name: npm package name
        server_name_upper: Server name in uppercase
        var_type: Type of variable
        
    Returns:
        Detected env var name or None
    """
    try:
        # Get package.json from npm
        result = subprocess.run(
            ["npm", "view", package_name, "--json"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            package_data = json.loads(result.stdout)
            
            # Check repository URL for hints
            repo = package_data.get("repository", {})
            if isinstance(repo, dict):
                repo_url = repo.get("url", "")
            else:
                repo_url = str(repo)
            
            # Check keywords for hints
            keywords = package_data.get("keywords", [])
            keywords_str = " ".join(keywords).upper()
            
            # Search in package data for env var patterns
            package_str = json.dumps(package_data).upper()
            env_pattern = rf"({server_name_upper}_[A-Z_]+)"
            matches = re.findall(env_pattern, package_str)
            if matches:
                for match in matches:
                    if _is_relevant_env_var(match, var_type):
                        return match
                        
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, json.JSONDecodeError, Exception):
        pass
    
    return None


def _is_relevant_env_var(env_var_name: str, var_type: str) -> bool:
    """
    Check if an environment variable name is relevant to the requested var_type.
    
    Args:
        env_var_name: Environment variable name (e.g., "HUGGINGFACE_API_KEY")
        var_type: Requested variable type ("token", "api_key", "client_id", etc.)
        
    Returns:
        True if the env var is relevant to the requested type
    """
    env_var_upper = env_var_name.upper()
    
    # Map var_type to possible suffixes
    type_mappings = {
        "token": ["TOKEN", "ACCESS_TOKEN", "API_KEY", "KEY", "AUTH_TOKEN"],
        "api_key": ["API_KEY", "KEY", "TOKEN", "ACCESS_TOKEN"],
        "dev_token": ["DEV_TOKEN", "TOKEN", "DEVELOPER_TOKEN"],
        "client_id": ["CLIENT_ID", "ID", "CLIENT"],
        "client_secret": ["CLIENT_SECRET", "SECRET"],
    }
    
    possible_suffixes = type_mappings.get(var_type, [var_type.upper()])
    
    # Check if env var ends with any of the possible suffixes
    for suffix in possible_suffixes:
        if env_var_upper.endswith(suffix):
            return True
    
    return False


def determine_env_var_name(server_name: str, var_type: str = "token") -> str:
    """
    Dynamically determine environment variable name based on server name.
    
    Uses dynamic pattern matching - no hardcoded server names.
    Pattern: {SERVER_NAME}_{VAR_TYPE}
    
    Args:
        server_name: Name of the server (e.g., "box", "linear")
        var_type: Type of variable ("token", "client_id", "client_secret", "api_key", "dev_token")
        
    Returns:
        Environment variable name (e.g., "BOX_DEV_TOKEN", "LINEAR_CLIENT_ID")
    """
    server_name_upper = server_name.upper().replace("-", "_").replace(" ", "_")
    
    # Dynamic var type mapping
    var_type_mapping = {
        "token": "ACCESS_TOKEN",
        "client_id": "CLIENT_ID",
        "client_secret": "CLIENT_SECRET",
        "api_key": "API_KEY",
        "dev_token": "DEV_TOKEN"
    }
    
    var_suffix = var_type_mapping.get(var_type, var_type.upper())
    return f"{server_name_upper}_{var_suffix}"


def determine_connection_type(mcp_server_url: str) -> str:
    """
    Determine connection type from MCP server URL.
    
    Args:
        mcp_server_url: URL of the MCP server
        
    Returns:
        "sse" if URL is HTTP/HTTPS, "stdio" otherwise
    """
    if not mcp_server_url:
        return "stdio"
    
    try:
        parsed = urlparse(mcp_server_url)
        if parsed.scheme in ["http", "https"]:
            return "sse"
    except Exception:
        pass
    
    return "stdio"


def generate_server_config(
    server_data: Dict[str, Any],
    npm_package: Optional[Dict[str, Any]] = None,
    test_sse: bool = True
) -> Dict[str, Any]:
    """
    Generate MCP server configuration from OAuth token data.
    
    Args:
        server_data: OAuth token data for the server
        npm_package: Optional npm package information
        test_sse: Whether to test SSE endpoint before deciding connection type
        
    Returns:
        Server configuration dictionary
    """
    server_name = server_data.get("serverName", "").lower()
    client_id = server_data.get("clientId", "")
    client_secret = server_data.get("clientSecret", "")
    token_url = server_data.get("tokenUrl", "")
    mcp_server_url = server_data.get("mcpServerUrl", "")
    
    # Handle accessToken - can be directly in server_data or nested in token object
    access_token = server_data.get("accessToken", "")
    token_data = server_data.get("token", {})
    if not access_token and isinstance(token_data, dict):
        access_token = token_data.get("accessToken", "")
    
    # Determine connection type
    connection_type = None
    
    # Step 1: Check if URL explicitly indicates SSE (e.g., contains "/sse")
    # If URL explicitly indicates SSE, prioritize SSE and skip npm package search
    url_indicates_sse = mcp_server_url and "/sse" in mcp_server_url.lower()
    url_is_http = mcp_server_url and mcp_server_url.startswith(("http://", "https://"))
    
    # Step 2: Only search for npm package if:
    #   - No SSE URL provided, OR
    #   - SSE URL provided but doesn't explicitly indicate SSE (might be a generic HTTP endpoint)
    # This avoids unnecessary npm searches when SSE is clearly the intended connection type
    should_search_npm = not url_indicates_sse
    
    if should_search_npm and not npm_package:
        print(f"   🔍 Searching npm for {server_name} MCP server package...")
        npm_package = search_npm_package(server_name)
        if npm_package:
            print(f"   ✅ Found npm package: {npm_package['package_name']}")
    elif url_indicates_sse:
        print(f"   ✅ URL explicitly indicates SSE - will test first, then search npm if SSE fails")
    
    # Step 3: Verify npm package has executable if we found one
    npm_package_usable = False
    if npm_package:
        package_name = npm_package.get("package_name", "")
        if package_name:
            print(f"   🔍 Verifying npm package has executable: {package_name}")
            npm_package_usable = verify_npm_package_executable(package_name)
            if npm_package_usable:
                print(f"   ✅ Package has executable - can use stdio")
            else:
                print(f"   ⚠️  Package has no executable/bin entry - cannot use stdio")
                print(f"   🔄 Will prefer SSE if endpoint available")
    
    # Step 4: If URL is HTTP/HTTPS, test SSE endpoint
    if mcp_server_url and mcp_server_url.startswith(("http://", "https://")):
        if test_sse and HAS_REQUESTS and access_token:
            print(f"   🧪 Testing SSE endpoint: {mcp_server_url}")
            sse_works = test_sse_endpoint(mcp_server_url, access_token)
            
            if sse_works:
                # SSE test passed
                if url_indicates_sse:
                    # URL explicitly indicates SSE - prefer SSE
                    print(f"   ✅ SSE endpoint is accessible (URL explicitly indicates SSE)")
                    if npm_package_usable:
                        print(f"   ⚠️  Both SSE and stdio available - using SSE (URL explicitly indicates SSE)")
                    connection_type = "sse"
                elif npm_package_usable:
                    # Have usable npm package - prefer stdio for reliability
                    print(f"   ⚠️  SSE test passed but usable npm package available - preferring stdio for reliability")
                    connection_type = "stdio"
                else:
                    # No usable npm package, use SSE
                    print(f"   ✅ SSE endpoint is accessible (no usable npm package)")
                    connection_type = "sse"
            else:
                # SSE test failed (405 or other error)
                print(f"   ⚠️  SSE endpoint test failed (likely 405 Method Not Allowed)")
                
                # If URL explicitly indicates SSE, prefer SSE even if test fails
                # (test might fail due to auth or other reasons, not because SSE isn't supported)
                if url_indicates_sse:
                    print(f"   ✅ URL explicitly indicates SSE - using SSE despite test failure")
                    print(f"   ℹ️  Test failure may be due to authentication or endpoint configuration")
                    connection_type = "sse"
                else:
                    # URL doesn't explicitly indicate SSE, try npm fallback
                    # If we skipped npm search because URL indicated SSE, search now as fallback
                    if not npm_package:
                        print(f"   🔍 SSE failed - searching npm for fallback stdio option...")
                        npm_package = search_npm_package(server_name)
                        if npm_package:
                            print(f"   ✅ Found npm package: {npm_package['package_name']}")
                            # Verify it has executable
                            package_name = npm_package.get("package_name", "")
                            if package_name:
                                npm_package_usable = verify_npm_package_executable(package_name)
                                if npm_package_usable:
                                    print(f"   ✅ Package has executable - will use stdio as fallback")
                                else:
                                    print(f"   ⚠️  Package has no executable - cannot use stdio fallback")
                    
                    if npm_package_usable:
                        print(f"   🔄 Falling back to stdio...")
                        connection_type = "stdio"
                    else:
                        # No usable npm package - use SSE anyway (better than failing stdio)
                        print(f"   ⚠️  SSE failed and no usable npm package - using SSE (stdio would fail)")
                        connection_type = "sse"
        elif url_indicates_sse:
            # URL explicitly indicates SSE, trust it
            print(f"   ✅ URL explicitly indicates SSE")
            if npm_package_usable:
                print(f"   ⚠️  Both SSE and stdio available - using SSE (URL explicitly indicates SSE)")
            connection_type = "sse"
        elif npm_package_usable:
            # Have usable npm package, prefer stdio
            connection_type = "stdio"
        else:
            # Default to SSE for HTTP/HTTPS URLs if no usable npm package
            if npm_package:
                print(f"   ⚠️  npm package found but has no executable - using SSE")
            connection_type = "sse"
    elif npm_package_usable:
        # Have usable npm package, use stdio
        connection_type = "stdio"
    else:
        # No usable npm package and no HTTP URL
        if npm_package:
            print(f"   ⚠️  npm package found but has no executable - defaulting to stdio (may fail)")
        connection_type = "stdio"
    
    # Build configuration
    config = {
        "name": server_name,
        "description": f"{server_name.capitalize()} MCP Server with OAuth",
        "connection_type": connection_type,
    }
    
    # Add repository if present in server_data
    repository = server_data.get("repository", "")
    has_repository = bool(repository)
    if repository:
        config["repository"] = repository
    
    # Extract ENV vars from GitHub for both SSE and stdio (if repository provided)
    github_env_vars = []
    if repository and HAS_REQUESTS:
        github_env_vars = extract_env_vars_from_github(repository, server_name)
        if github_env_vars:
            print(f"   🔑 Found {len(github_env_vars)} env var(s) from GitHub: {', '.join(github_env_vars)}")
    
    # Check GitHub repository for stdio configuration (if repository provided)
    github_config = None
    if connection_type == "stdio" and repository and HAS_REQUESTS:
        github_config = parse_github_repository_for_stdio(repository, server_name)
    
    # Add connection details based on type
    if connection_type == "stdio":
        # Step 1: Use GitHub configuration if available
        if github_config and (github_config.get("command") or github_config.get("package_name")):
            # Use GitHub configuration
            if github_config.get("command"):
                config["command"] = github_config["command"]
                config["args"] = github_config.get("args", [])
                print(f"   📦 Using GitHub config: command={config['command']}, args={config['args']}")
            elif github_config.get("package_name"):
                # Package name found in GitHub, use npx
                config["command"] = "npx"
                config["args"] = ["-y", github_config["package_name"]]
                print(f"   📦 Using GitHub config: package={github_config['package_name']}")
        elif npm_package:
            # Fallback to npm package
            config["command"] = "npx"
            # Build args list with package name and any extra arguments detected
            args = ["-y", npm_package["package_name"]]
            # Add extra arguments if detected (e.g., "mcp" subcommand for CLI tools)
            extra_args = npm_package.get("extra_args", [])
            if extra_args:
                args.extend(extra_args)
            config["args"] = args
            print(f"   📦 Using npm package (GitHub unavailable): {npm_package['package_name']}")
        else:
            # Final fallback: try common patterns
            config["command"] = "npx"
            config["args"] = ["-y", f"@{server_name}/mcp-server"]
            print(f"   📦 Using default pattern (no GitHub/npm found): @{server_name}/mcp-server")
    
    # Always preserve the original mcpServerUrl as endpoint_url for reference,
    # even when using stdio connection (useful for documentation/metadata)
    if mcp_server_url:
        config["endpoint_url"] = mcp_server_url
    
    # Add environment variables - dynamically determine based on server name
    env = {}
    
    # access_token, client_id, and client_secret are already extracted above
    
    # Step 1: Check if github_config has env_block (actual JSON env from README)
    # This preserves the exact env var names from the MCP config examples in README
    github_env_block = None
    if github_config and github_config.get("env_block"):
        github_env_block = github_config.get("env_block")
        # Extract env var names from the block
        for var_name in github_env_block.keys():
            if var_name.upper() not in [v.upper() for v in github_env_vars]:
                github_env_vars.append(var_name.upper())
    
    # Merge env vars from github_config (if stdio) with github_env_vars (already extracted above)
    if github_config:
        stdio_env_vars = github_config.get("env_vars", [])
        if stdio_env_vars:
            github_env_vars.extend(stdio_env_vars)
            github_env_vars = list(set([var.upper() for var in github_env_vars]))
    
    # Dynamically determine env var names based on server name (no hardcoding)
    # Priority: GitHub env vars -> detect_expected_env_var_name -> determine_env_var_name
    # Priority: client_secret -> access_token for token/env vars
    # If client_secret exists, use it as dev_token pattern (common for stdio servers)
    # Otherwise use access_token with token pattern
    
    # Determine which token to use for the main env var
    # Priority: client_secret -> access_token
    # Always prefer GitHub env vars if available (for both SSE and stdio)
    if client_secret:
        # Use client_secret as dev_token (common pattern for MCP servers)
        # Try to detect the actual expected env var name (prefer GitHub if available)
        if github_env_vars:
            # Use first matching env var from GitHub
            for var in github_env_vars:
                if "TOKEN" in var or "KEY" in var or "SECRET" in var:
                    env_var_name = var
                    break
            else:
                env_var_name = github_env_vars[0] if github_env_vars else detect_expected_env_var_name(server_name, npm_package, "dev_token")
        else:
            env_var_name = detect_expected_env_var_name(server_name, npm_package, "dev_token")
        env[env_var_name] = client_secret
    elif access_token:
        # Prefer GitHub env vars if available (for both SSE and stdio)
        if github_env_vars:
            # Use first matching env var from GitHub
            for var in github_env_vars:
                if "TOKEN" in var or "KEY" in var:
                    env_var_name = var
                    break
            else:
                env_var_name = github_env_vars[0] if github_env_vars else detect_expected_env_var_name(server_name, npm_package, "dev_token" if has_repository else "token")
        else:
            # Fallback: If repository is present, prefer dev_token pattern (stdio)
            # Otherwise use standard token pattern (SSE/remote)
            if has_repository:
                env_var_name = detect_expected_env_var_name(server_name, npm_package, "dev_token")
            else:
                env_var_name = detect_expected_env_var_name(server_name, npm_package, "token")
        env[env_var_name] = access_token
    elif github_env_vars:
        # No token provided, but we found env vars from GitHub
        # Add them with placeholder values so user knows what's needed
        for var in github_env_vars:
            env[var] = f"YOUR_{var}_HERE"
    
    # Add client credentials if provided
    if client_id:
        env_var_name = detect_expected_env_var_name(server_name, npm_package, "client_id")
        env[env_var_name] = client_id
    
    # Only add client_secret as separate env var if access_token exists and is non-empty
    # (meaning client_secret wasn't used as the main token)
    if client_secret and access_token and access_token.strip():
        # Both exist - add client_secret as separate env var (not used as dev_token)
        env_var_name = detect_expected_env_var_name(server_name, npm_package, "client_secret")
        env[env_var_name] = client_secret
    
    if env:
        config["env"] = env
    
    # Only add authentication configuration if repository is NOT present
    # If repository exists, skip authentication section (only ENV needed)
    if not has_repository:
        auth_config = {
            "type": "oauth2_1_authorization_code",
            "token_url": token_url,
            "client_id": client_id,
            "access_token": access_token,
        }
        
        if isinstance(token_data, dict):
            refresh_token = token_data.get("refreshToken")
            if refresh_token:
                auth_config["refresh_token"] = refresh_token
            
            expires_at = token_data.get("expiresAt")
            if expires_at:
                auth_config["expires_at"] = expires_at
        
        config["authentication"] = auth_config
    
    # Add skip key with default value of 0
    config["skip"] = 0
    
    return config


def process_oauth_tokens_file(input_file: Path, test_sse: bool = True) -> List[Dict[str, Any]]:
    """
    Process OAuth tokens file and generate server configurations.
    
    Args:
        input_file: Path to JSON file containing OAuth tokens
        test_sse: Whether to test SSE endpoints before deciding connection type
        
    Returns:
        List of server configurations
    """
    # Read input file
    try:
        with open(input_file, 'r') as f:
            oauth_data = json.load(f)
    except FileNotFoundError:
        print(f"❌ Error: File not found: {input_file}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ Error: Invalid JSON in {input_file}: {e}")
        sys.exit(1)
    
    if not isinstance(oauth_data, list):
        print(f"❌ Error: Expected a JSON array, got {type(oauth_data)}")
        sys.exit(1)
    
    server_configs = []
    
    print(f"📋 Processing {len(oauth_data)} server(s)...\n")
    
    for idx, server_data in enumerate(oauth_data, 1):
        server_name = server_data.get("serverName", "unknown")
        mcp_server_url = server_data.get("mcpServerUrl", "")
        
        print(f"[{idx}/{len(oauth_data)}] Processing: {server_name}")
        
        # Pre-search for npm package (will be used if SSE fails or if no URL)
        npm_package = None
        if not mcp_server_url or not mcp_server_url.startswith(("http://", "https://")):
            # No URL or not HTTP/HTTPS, search npm package first
            print(f"   🔍 Searching npm for {server_name} MCP server package...")
            npm_package = search_npm_package(server_name)
            
            if npm_package:
                print(f"   ✅ Found npm package: {npm_package['package_name']}")
            else:
                print(f"   ⚠️  No npm package found, will use default pattern if needed")
        
        # Generate configuration (will test SSE if URL is HTTP/HTTPS)
        config = generate_server_config(server_data, npm_package, test_sse=test_sse)
        server_configs.append(config)
        
        print(f"   ✅ Configuration generated (connection_type: {config.get('connection_type', 'unknown')})\n")
    
    return server_configs


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Generate MCP server configuration from OAuth tokens file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate config from oauth_tokens.json
  python generate_mcp_config_from_oauth.py oauth_tokens.json
  
  # Specify output directory
  python generate_mcp_config_from_oauth.py oauth_tokens.json --output-dir ./custom_config
        """
    )
    
    parser.add_argument(
        "input_file",
        type=str,
        help="Path to JSON file containing OAuth tokens"
    )
    
    parser.add_argument(
        "--output-dir",
        type=str,
        default="mcp_config",
        help="Output directory for generated configuration (default: mcp_config)"
    )
    
    parser.add_argument(
        "--output-file",
        type=str,
        default="example_servers.json",
        help="Output filename (default: example_servers.json)"
    )
    
    parser.add_argument(
        "--no-sse-test",
        action="store_true",
        help="Skip SSE endpoint testing (faster but less accurate)"
    )
    
    args = parser.parse_args()
    
    # Process input file
    input_path = Path(args.input_file)
    if not input_path.exists():
        print(f"❌ Error: Input file not found: {input_path}")
        sys.exit(1)
    
    # Generate configurations
    server_configs = process_oauth_tokens_file(input_path, test_sse=not args.no_sse_test)
    
    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Write output file
    output_file = output_dir / args.output_file
    output_data = {
        "servers": server_configs
    }
    
    try:
        with open(output_file, 'w') as f:
            json.dump(output_data, f, indent=2)
        
        print(f"✅ Configuration saved to: {output_file}")
        print(f"📊 Generated {len(server_configs)} server configuration(s)")
        
    except Exception as e:
        print(f"❌ Error writing output file: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
