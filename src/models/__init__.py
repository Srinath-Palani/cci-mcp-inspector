"""Pydantic models for structured outputs"""

from src.models.structured_output import (
    MCPToolParameter,
    MCPTool,
    MCPResource,
    MCPPrompt,
    MCPCapabilities,
    MCPAnalysisResult,
    MCPServerInspectionReport,
    MCPDiscoveryOutput,
    MCPAnalysisOutput,
    ToolCategoryAnalysis,
)

__all__ = [
    "MCPToolParameter",
    "MCPTool",
    "MCPResource",
    "MCPPrompt",
    "MCPCapabilities",
    "MCPAnalysisResult",
    "MCPServerInspectionReport",
    "MCPDiscoveryOutput",
    "MCPAnalysisOutput",
    "ToolCategoryAnalysis",
]

