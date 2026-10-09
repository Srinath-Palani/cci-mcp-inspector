"""
Utility functions for MCP Server Inspector.

Provides common utilities for configuration, file operations,
LLM access, and directory management.
"""

import datetime
import os
import json
from pathlib import Path
from typing import Tuple, Optional, Dict, Any, List

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI


class Utils:
    """Common utility functions for MCP Inspector."""

    def __init__(self, config_file: Optional[Path] = None):
        self.project_root = self.get_project_root()
        if config_file:
            self.examples_path = Path(config_file).resolve()
        else:
            self.examples_path = self.project_root / "examples" / "example_servers.json"

    def load_env(self) -> None:
        """Load environment variables from .env file."""
        # Try to load from project root first, then current directory
        project_root = self.get_project_root()
        env_file = project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=True)
        else:
            load_dotenv(override=True)  # Fallback to default behavior

    def get_project_root(self) -> Path:
        """Get the project root directory."""
        return Path(__file__).resolve().parent.parent.parent

    def get_openai_api_key(self) -> str:
        """Get OpenAI API key from environment."""
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not found in environment variables")
        return api_key

    def get_llm(self, provider: str = "openai", model: str = "gpt-4o", temperature: float = 0) -> str:
        """
        Get LLM model identifier.
        
        Args:
            provider: LLM provider (openai, anthropic, etc.)
            model: Model name
            temperature: Temperature setting
            
        Returns:
            Model identifier string
        """
        llm_model = f"{provider}:{model}"
        print(f"🤖 LLM Model: {llm_model}")
        return llm_model

    # Per-request timeout and retry cap for LLM calls. The OpenAI client defaults
    # to a 600 s request timeout with retries on top — one stalled analysis call
    # would therefore outlive the entire 300 s inspection budget and the job would
    # die blaming the MCP handshake instead of the LLM phase.
    LLM_REQUEST_TIMEOUT_SECONDS = 60.0
    LLM_MAX_RETRIES = 2

    def get_openai_llm(self, model: str = "gpt-4o", temperature: float = 0) -> ChatOpenAI:
        """
        Get ChatOpenAI instance.

        Args:
            model: Model name
            temperature: Temperature setting

        Returns:
            ChatOpenAI instance
        """
        self.load_env()
        llm = ChatOpenAI(
            model=model,
            temperature=temperature,
            timeout=self.LLM_REQUEST_TIMEOUT_SECONDS,
            max_retries=self.LLM_MAX_RETRIES,
        )
        print(f"🤖 LLM Model: {model}")
        return llm

    def setup_output_dir(self, server_name: str, base_dir: Optional[Path] = None) -> Path:
        """
        Create and return output directory for inspection reports.
        
        Args:
            server_name: Name of the MCP server being inspected
            base_dir: Optional base directory (if not provided, uses env var or creates new)
            
        Returns:
            Path to the output directory
        """
        if base_dir is None:
            env_dir = os.getenv("MCP_INSPECTOR_OUTPUT_DIR")
            if env_dir:
                base_dir = Path(env_dir)
            else:
                now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                base_dir = self.project_root / "reports" / f"{server_name}_{now}"

        base_dir.mkdir(parents=True, exist_ok=True)
        print(f"📁 Output directory: {base_dir}")
        return base_dir

    def load_server_config(self, server_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Load server configuration from example_servers.json.
        
        Args:
            server_name: Name of the server to load (if None, returns first server)
            
        Returns:
            Server configuration dictionary
        """
        if not self.examples_path.exists():
            raise FileNotFoundError(f"Server config not found: {self.examples_path}")

        with open(self.examples_path, "r") as f:
            config = json.load(f)

        servers = config.get("servers", [])
        if not servers:
            raise ValueError("No servers configured in example_servers.json")

        if server_name:
            for server in servers:
                if server.get("name") == server_name:
                    return server
            raise ValueError(f"Server '{server_name}' not found in configuration")
        
        # Return first server if no name specified
        return servers[0]
    
    def get_all_server_names(self) -> List[str]:
        """
        Get all server names from example_servers.json.
        
        Returns:
            List of server names
        """
        if not self.examples_path.exists():
            raise FileNotFoundError(f"Server config not found: {self.examples_path}")

        with open(self.examples_path, "r") as f:
            config = json.load(f)

        servers = config.get("servers", [])
        if not servers:
            raise ValueError("No servers configured in example_servers.json")
        
        return [server.get("name") for server in servers if server.get("name")]

    def save_report(self, report: Dict[str, Any], output_dir: Path, filename: str = "inspection_report.json") -> Path:
        """
        Save inspection report to JSON file.
        
        Args:
            report: Report dictionary to save
            output_dir: Directory to save report in
            filename: Filename for the report
            
        Returns:
            Path to saved report
        """
        report_path = output_dir / filename
        
        # Custom JSON encoder to handle Pydantic AnyUrl and other non-serializable types
        class JSONEncoder(json.JSONEncoder):
            def default(self, obj):
                # Handle Pydantic AnyUrl objects and other non-serializable types
                try:
                    # Try to convert to string if it has string representation
                    if hasattr(obj, '__str__'):
                        return str(obj)
                except:
                    pass
                # Fall back to default JSON encoder
                return super().default(obj)
        
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, cls=JSONEncoder)
        
        print(f"✅ Report saved to: {report_path}")
        return report_path

    def load_prompt(self, prompt_filename: str) -> str:
        """
        Load prompt template from prompts directory.
        
        Args:
            prompt_filename: Name of the prompt file
            
        Returns:
            Prompt content as string
        """
        prompt_path = self.project_root / "src" / "prompts" / prompt_filename
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
        
        with open(prompt_path, "r") as f:
            return f.read()

    def format_prompt(self, template: str, **kwargs) -> str:
        """
        Format prompt template with provided variables.
        
        Args:
            template: Prompt template string
            **kwargs: Variables to substitute in template
            
        Returns:
            Formatted prompt string
        """
        return template.format(**kwargs)

    def sanitize_server_name(self, name: str) -> str:
        """
        Sanitize server name for use in filenames and paths.
        
        Args:
            name: Server name
            
        Returns:
            Sanitized name
        """
        return name.replace(" ", "_").replace("/", "_").replace("\\", "_").lower()

    def get_timestamp(self) -> str:
        """
        Get current timestamp in ISO 8601 format.
        
        Returns:
            Timestamp string
        """
        return datetime.datetime.now().isoformat()

    def calculate_statistics(self, tools: list, resources: list, prompts: list) -> Dict[str, int]:
        """
        Calculate statistics for discovered entities.
        
        Args:
            tools: List of tools
            resources: List of resources
            prompts: List of prompts
            
        Returns:
            Statistics dictionary
        """
        return {
            "total_tools": len(tools),
            "total_resources": len(resources),
            "total_prompts": len(prompts),
            "total_entities": len(tools) + len(resources) + len(prompts)
        }

    def merge_metadata(self, *metadata_dicts: Dict[str, Any]) -> Dict[str, Any]:
        """
        Merge multiple metadata dictionaries.
        
        Args:
            *metadata_dicts: Variable number of dictionaries to merge
            
        Returns:
            Merged dictionary
        """
        merged = {}
        for metadata in metadata_dicts:
            if metadata:
                merged.update(metadata)
        return merged

