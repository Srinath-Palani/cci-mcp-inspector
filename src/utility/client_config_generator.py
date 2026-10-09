"""
MCP Client Configuration Generator

Generates MCP client configuration files from inspection results.
Supports generating configurations for Cursor, Claude Desktop, and other MCP clients.
"""

from typing import Dict, Any, Optional
from pathlib import Path
import json
import os


class MCPClientConfigGenerator:
    """Generate MCP client configuration from inspection results."""
    
    def __init__(self, inspection_report: Dict[str, Any], original_config: Optional[Dict[str, Any]] = None):
        """
        Initialize with inspection report.
        
        Args:
            inspection_report: Complete inspection report dictionary
            original_config: Original server configuration (if available)
        """
        self.report = inspection_report
        self.original_config = original_config or {}
        self.server_name = inspection_report.get('server_name', 'unknown')
        self.connection_type = inspection_report.get('connection_type', 'stdio')
        self.metadata = inspection_report.get('metadata', {})
        self.discovery_data = self.metadata.get('discovery_data', {})
    
    def generate_cursor_config(self) -> Dict[str, Any]:
        """
        Generate Cursor-compatible MCP client configuration.
        
        Returns:
            Dictionary with Cursor MCP server configuration
        """
        config = {}
        
        if self.connection_type == "stdio":
            config = self._generate_stdio_config()
        elif self.connection_type == "sse":
            config = self._generate_sse_config()
        else:
            # Fallback to stdio if unknown
            config = self._generate_stdio_config()
        
        return config
    
    def _generate_stdio_config(self) -> Dict[str, Any]:
        """Generate stdio-based configuration."""
        config = {
            "command": self.original_config.get("command", "npx"),
            "args": self.original_config.get("args", [])
        }
        
        # Add environment variables
        env = self.original_config.get("env", {})
        if env:
            config["env"] = {}
            for key, value in env.items():
                # Use environment variable reference if it's a placeholder
                if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                    config["env"][key] = os.getenv(key.replace("${", "").replace("}", ""), value)
                else:
                    config["env"][key] = value
        
        return config
    
    def _generate_sse_config(self) -> Dict[str, Any]:
        """Generate SSE-based configuration."""
        endpoint_url = self.original_config.get("endpoint_url", "")
        if not endpoint_url:
            # Try to extract from discovery data
            endpoint_url = self.original_config.get("endpoint_url", "")
        
        config = {
            "url": endpoint_url
        }
        
        # Add authentication headers
        auth_config = self.original_config.get("authentication", {})
        if auth_config:
            headers = self._build_auth_headers(auth_config)
            if headers:
                config["headers"] = headers
        
        return config
    
    def _build_auth_headers(self, auth_config: Dict[str, Any]) -> Dict[str, str]:
        """Build authentication headers from auth configuration."""
        headers = {}
        auth_type = auth_config.get("type", "")
        
        if auth_type == "bearer_token":
            token = auth_config.get("token", "")
            if token:
                # Handle environment variable references
                if isinstance(token, str) and token.startswith("${") and token.endswith("}"):
                    env_var = token.replace("${", "").replace("}", "")
                    token = os.getenv(env_var, token)
                headers["Authorization"] = f"Bearer {token}"
        
        elif auth_type == "personal_access_token":
            token = auth_config.get("token", "")
            if token:
                if isinstance(token, str) and token.startswith("${") and token.endswith("}"):
                    env_var = token.replace("${", "").replace("}", "")
                    token = os.getenv(env_var, token)
                headers["Authorization"] = f"token {token}"
        
        elif auth_type == "api_token":
            token = auth_config.get("token", "")
            header_name = auth_config.get("header", "X-API-Key")
            if token:
                if isinstance(token, str) and token.startswith("${") and token.endswith("}"):
                    env_var = token.replace("${", "").replace("}", "")
                    token = os.getenv(env_var, token)
                headers[header_name] = token
        
        elif auth_type in ["oauth2_1_authorization_code", "oauth2_1_client_credentials"]:
            # For OAuth, we need to generate token first
            # For now, include OAuth config in a comment or separate section
            token = auth_config.get("access_token", "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            # Note: OAuth requires dynamic token generation
        
        return headers
    
    def generate_claude_desktop_config(self) -> Dict[str, Any]:
        """
        Generate Claude Desktop-compatible MCP client configuration.
        
        Returns:
            Dictionary with Claude Desktop MCP server configuration
        """
        # Claude Desktop uses similar format to Cursor
        return self.generate_cursor_config()
    
    def generate_full_client_config(self, format: str = "cursor") -> Dict[str, Any]:
        """
        Generate complete MCP client configuration file.
        
        Args:
            format: Configuration format ("cursor", "claude", "generic")
        
        Returns:
            Complete client configuration dictionary
        """
        if format == "cursor":
            server_config = self.generate_cursor_config()
        elif format == "claude":
            server_config = self.generate_claude_desktop_config()
        else:
            server_config = self.generate_cursor_config()
        
        # Build full configuration
        full_config = {
            "mcpServers": {
                self.server_name: server_config
            }
        }
        
        return full_config
    
    def save_client_config(
        self,
        output_path: Path,
        format: str = "cursor",
        include_comments: bool = True
    ) -> Path:
        """
        Save client configuration to file.
        
        Args:
            output_path: Path to save configuration file
            format: Configuration format ("cursor", "claude", "generic")
            include_comments: Whether to include helpful comments
        
        Returns:
            Path to saved configuration file
        """
        config = self.generate_full_client_config(format)
        
        # Convert to JSON with formatting
        if include_comments:
            # Create a JSONC-like format with comments
            json_str = json.dumps(config, indent=2)
            
            # Add comments
            comments = [
                f"// MCP Client Configuration for {self.server_name}",
                f"// Generated from inspection report",
                f"// Connection Type: {self.connection_type}",
                f"// Protocol Version: {self._get_protocol_version()}",
                ""
            ]
            
            # Add authentication notes if needed
            auth_config = self.original_config.get("authentication", {})
            if auth_config:
                auth_type = auth_config.get("type", "")
                if "oauth" in auth_type.lower():
                    comments.append("// NOTE: OAuth requires dynamic token generation")
                    comments.append("// You may need to configure OAuth flow separately")
                    comments.append("")
            
            json_str = "\n".join(comments) + json_str
            
            # Save as .jsonc if comments are included
            if output_path.suffix == ".json":
                output_path = output_path.with_suffix(".jsonc")
        else:
            json_str = json.dumps(config, indent=2)
        
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w') as f:
            f.write(json_str)
        
        return output_path
    
    def _get_protocol_version(self) -> str:
        """Extract the newest supported MCP protocol version from the report."""
        from src.models.structured_output import SUPPORTED_PROTOCOL_VERSIONS, protocol_version_field
        attributes = self.report.get('server_attributes', {})
        if attributes:
            protocol = attributes.get('protocol_version', {})
            # SUPPORTED_PROTOCOL_VERSIONS is ordered newest-first
            for version in SUPPORTED_PROTOCOL_VERSIONS:
                if protocol.get(protocol_version_field(version)):
                    return version
        return "unknown"
    
    def generate_config_summary(self) -> str:
        """Generate a human-readable summary of the configuration."""
        summary = f"\n{'='*80}\n"
        summary += f"  MCP CLIENT CONFIGURATION - {self.server_name}\n"
        summary += f"{'='*80}\n\n"
        
        summary += f"Server Name: {self.server_name}\n"
        summary += f"Connection Type: {self.connection_type}\n"
        summary += f"Protocol Version: {self._get_protocol_version()}\n\n"
        
        if self.connection_type == "stdio":
            command = self.original_config.get("command", "N/A")
            args = self.original_config.get("args", [])
            summary += f"Command: {command}\n"
            summary += f"Arguments: {' '.join(args)}\n"
            
            env = self.original_config.get("env", {})
            if env:
                summary += f"\nEnvironment Variables:\n"
                for key, value in env.items():
                    summary += f"  {key}: {value}\n"
        
        elif self.connection_type == "sse":
            endpoint = self.original_config.get("endpoint_url", "N/A")
            summary += f"Endpoint URL: {endpoint}\n"
            
            auth_config = self.original_config.get("authentication", {})
            if auth_config:
                auth_type = auth_config.get("type", "N/A")
                summary += f"Authentication: {auth_type}\n"
        
        summary += f"\n{'='*80}\n"
        
        return summary


def generate_client_config_from_inspection(
    inspection_report_path: str,
    original_config_path: Optional[str] = None,
    output_path: Optional[str] = None,
    format: str = "cursor"
) -> Path:
    """
    Generate MCP client configuration from inspection report.
    
    Args:
        inspection_report_path: Path to inspection_report.json
        original_config_path: Path to original server config (optional)
        output_path: Path to save client config (defaults to json_config folder)
        format: Configuration format ("cursor", "claude", "generic")
    
    Returns:
        Path to generated configuration file
    """
    # Load inspection report
    with open(inspection_report_path, 'r') as f:
        inspection_report = json.load(f)
    
    # Load original config if provided
    original_config = None
    if original_config_path:
        with open(original_config_path, 'r') as f:
            config_data = json.load(f)
            # Find the server config
            servers = config_data.get("servers", [])
            server_name = inspection_report.get("server_name", "")
            for server in servers:
                if server.get("name") == server_name:
                    original_config = server
                    break
    
    # Generate configuration
    generator = MCPClientConfigGenerator(inspection_report, original_config)
    
    # Determine output path
    if output_path is None:
        # Default to json_config folder
        json_config_dir = Path("json_config")
        json_config_dir.mkdir(exist_ok=True)
        server_name = inspection_report.get("server_name", "server")
        output_path = json_config_dir / f"{server_name}_client_config.json"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Save configuration
    config_path = generator.save_client_config(output_path, format)
    
    # Print summary
    print(generator.generate_config_summary())
    print(f"✅ Client configuration saved to: {config_path}")
    
    return config_path


if __name__ == "__main__":
    """Command-line usage."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python client_config_generator.py <inspection_report.json> [original_config.json] [output_path]")
        sys.exit(1)
    
    inspection_path = sys.argv[1]
    original_path = sys.argv[2] if len(sys.argv) > 2 else None
    output_path = sys.argv[3] if len(sys.argv) > 3 else None
    
    generate_client_config_from_inspection(inspection_path, original_path, output_path)

