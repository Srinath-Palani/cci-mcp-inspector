"""
MCP Analysis Agent

Analyzes discovered MCP server capabilities using LLM to categorize tools,
generate examples, identify relationships, and provide insights.
"""

import asyncio
import time
import json
from typing import Dict, Any, List

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from src.models.structured_output import (
    MCPTool,
    MCPToolParameter,
    MCPResource,
    MCPPrompt,
    MCPAnalysisResult,
    MCPAnalysisOutput,
    ToolCategoryAnalysis
)
from src.utility.utils import Utils

utils = Utils()


async def mcp_analysis_agent(discovery_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analysis agent that uses LLM to analyze discovered MCP server capabilities.
    
    Args:
        discovery_data: Discovery results from mcp_discovery_agent
    
    Returns:
        Dictionary containing analysis results with categorized tools and insights
    """
    server_name = discovery_data.get("server_name", "unknown")
    tools_raw = discovery_data.get("tools_discovered", [])
    resources_raw = discovery_data.get("resources_discovered", [])
    prompts_raw = discovery_data.get("prompts_discovered", [])
    
    print(f"\n{'='*60}")
    print(f"🧠 MCP ANALYSIS AGENT")
    print(f"{'='*60}")
    print(f"📡 Server: {server_name}")
    print(f"🔧 Tools to analyze: {len(tools_raw)}")
    print(f"📦 Resources to analyze: {len(resources_raw)}")
    print(f"📝 Prompts to analyze: {len(prompts_raw)}")
    print(f"{'='*60}\n")
    
    start_time = time.time()
    
    try:
        utils.load_env()
        
        # Load analysis instructions
        system_prompt = utils.load_prompt("analysis_agent_instruction.md")
        
        # Build user prompt with discovery data
        user_prompt = _build_analysis_prompt(server_name, tools_raw, resources_raw, prompts_raw)
        
        # Initialize LLM
        llm = utils.get_openai_llm(model="gpt-4o", temperature=0.3)
        
        print("🤖 Invoking LLM for analysis...\n")
        
        # Call LLM
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt)
        ]
        
        # Offloaded: llm.invoke is synchronous, and this agent is awaited on the
        # FastAPI event loop. Called directly it blocks every other job in a batch
        # (plus the status poll and health check) for the whole call — up to
        # LLM_REQUEST_TIMEOUT_SECONDS × attempts. That serialized the analysis phase
        # and made concurrency above ~2 buy almost nothing.
        response = await asyncio.to_thread(llm.invoke, messages)
        analysis_text = response.content
        
        print("✅ LLM analysis complete\n")
        print("🔄 Parsing analysis results...\n")
        
        # Parse LLM response into structured format
        analysis_result = _parse_analysis_response(
            analysis_text,
            tools_raw,
            resources_raw,
            prompts_raw
        )
        
        print("="*60)
        print("✅ ANALYSIS COMPLETED SUCCESSFULLY")
        print("="*60)
        # analysis_result is a dict from model_dump(), so use bracket notation
        analysis_data = analysis_result.get('analysis_result', {})
        tool_categories = analysis_data.get('tool_categories', [])
        complexity_score = analysis_data.get('complexity_score', 0)
        print(f"📊 Tool categories identified: {len(tool_categories)}")
        print(f"🎯 Complexity score: {complexity_score}/10")
        print("="*60 + "\n")
        
        return analysis_result
        
    except Exception as e:
        print(f"\n❌ Analysis failed: {str(e)}\n")
        
        # Return failure output with basic parsing
        fallback_result = _create_fallback_analysis(
            server_name,
            tools_raw,
            resources_raw,
            prompts_raw,
            error_message=str(e)
        )
        
        return fallback_result
        
    finally:
        end_time = time.time()
        elapsed_time = end_time - start_time
        print(f"⏱️  Total analysis time: {elapsed_time:.2f} seconds\n")


def _build_analysis_prompt(
    server_name: str,
    tools: List[Dict[str, Any]],
    resources: List[Dict[str, Any]],
    prompts: List[Dict[str, Any]]
) -> str:
    """Build the analysis prompt with discovery data."""
    
    prompt = f"""# MCP Server Analysis Request

## Server Information
- **Name**: {server_name}
- **Total Tools**: {len(tools)}
- **Total Resources**: {len(resources)}
- **Total Prompts**: {len(prompts)}

## Discovered Tools

"""
    
    for i, tool in enumerate(tools, 1):
        prompt += f"### Tool {i}: {tool.get('name', 'unnamed')}\n"
        prompt += f"**Description**: {tool.get('description', 'No description')}\n\n"
        
        # Include input schema if available
        if 'inputSchema' in tool:
            schema = tool['inputSchema']
            prompt += f"**Input Schema**:\n```json\n{json.dumps(schema, indent=2)}\n```\n\n"
    
    if resources:
        prompt += "\n## Discovered Resources\n\n"
        for i, resource in enumerate(resources, 1):
            prompt += f"{i}. **{resource.get('name', 'unnamed')}** - {resource.get('uri', 'no-uri')}\n"
            prompt += f"   Description: {resource.get('description', 'No description')}\n\n"
    
    if prompts:
        prompt += "\n## Discovered Prompts\n\n"
        for i, prompt_item in enumerate(prompts, 1):
            prompt += f"{i}. **{prompt_item.get('name', 'unnamed')}**\n"
            prompt += f"   Description: {prompt_item.get('description', 'No description')}\n\n"
    
    prompt += """
## Analysis Tasks

Please analyze this MCP server and provide:

1. **Tool Categorization**: Group tools into logical categories (navigation, data, interaction, files, etc.)
2. **Complexity Score**: Rate 0-10 based on number of tools, parameter complexity, and schema depth
3. **Primary Use Case**: What is this server mainly used for?
4. **Strengths**: What does this server do well?
5. **Limitations**: What's missing or could be improved?
6. **Recommendations**: How should developers use this server effectively?
7. **Tool Relationships**: Which tools work together? What are common workflows?
8. **Usage Examples**: For each tool, provide 2-3 practical usage examples

Please structure your response in the following JSON format:

```json
{
  "tool_categories": [
    {
      "category_name": "Navigation",
      "tool_count": 3,
      "tools": ["tool1", "tool2", "tool3"],
      "description": "Tools for navigating web pages"
    }
  ],
  "complexity_score": 7.5,
  "primary_use_case": "Web browser automation",
  "strengths": ["Comprehensive API", "Well documented"],
  "limitations": ["No mobile support"],
  "recommendations": ["Start with basic navigation", "Combine with data extraction tools"],
  "relationships": ["Use navigate → wait → click workflow"],
  "tool_examples": {
    "tool_name": [
      "Example 1: Navigate to URL",
      "Example 2: Navigate with options"
    ]
  }
}
```
"""
    
    return prompt


def _parse_analysis_response(
    analysis_text: str,
    tools_raw: List[Dict[str, Any]],
    resources_raw: List[Dict[str, Any]],
    prompts_raw: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Parse LLM response into structured analysis output."""
    
    # Try to extract JSON from the response
    try:
        # Look for JSON block in markdown
        if "```json" in analysis_text:
            json_start = analysis_text.find("```json") + 7
            json_end = analysis_text.find("```", json_start)
            json_text = analysis_text[json_start:json_end].strip()
        elif "{" in analysis_text:
            # Try to find JSON object
            json_start = analysis_text.find("{")
            json_end = analysis_text.rfind("}") + 1
            json_text = analysis_text[json_start:json_end]
        else:
            raise ValueError("No JSON found in response")
        
        analysis_data = json.loads(json_text)
        
    except Exception as e:
        print(f"⚠️  Could not parse JSON from LLM response: {e}")
        print("Using fallback parsing...")
        analysis_data = _extract_analysis_from_text(analysis_text)
    
    # Build tool categories
    tool_categories = []
    for cat in analysis_data.get("tool_categories", []):
        tool_categories.append(ToolCategoryAnalysis(
            category_name=cat.get("category_name", "Uncategorized"),
            tool_count=cat.get("tool_count", 0),
            tools=cat.get("tools", []),
            description=cat.get("description")
        ))
    
    # Build analysis result
    analysis_result = MCPAnalysisResult(
        tool_categories=tool_categories,
        complexity_score=float(analysis_data.get("complexity_score", 5.0)),
        primary_use_case=analysis_data.get("primary_use_case"),
        strengths=analysis_data.get("strengths", []),
        limitations=analysis_data.get("limitations", []),
        recommendations=analysis_data.get("recommendations", []),
        relationships=analysis_data.get("relationships", [])
    )
    
    # Build analyzed tools with categories and examples
    tool_examples = analysis_data.get("tool_examples", {})
    tools_analyzed = []
    
    for tool_raw in tools_raw:
        tool_name = tool_raw.get("name", "unnamed")
        
        # Find category for this tool
        tool_category = None
        for cat in tool_categories:
            if tool_name in cat.tools:
                tool_category = cat.category_name
                break
        
        # Parse parameters from input schema
        parameters = _parse_tool_parameters(tool_raw.get("inputSchema", {}))
        
        # Get examples
        examples = tool_examples.get(tool_name, [])
        
        tool = MCPTool(
            name=tool_name,
            description=tool_raw.get("description"),
            parameters=parameters,
            category=tool_category,
            examples=examples
        )
        tools_analyzed.append(tool)
    
    # Build analyzed resources
    resources_analyzed = [
        MCPResource(
            uri=r.get("uri", ""),
            name=r.get("name", ""),
            description=r.get("description"),
            mimeType=r.get("mimeType")
        )
        for r in resources_raw
    ]
    
    # Build analyzed prompts (arguments arrive as normalized dicts from discovery)
    prompts_analyzed = [
        MCPPrompt(
            name=p.get("name", ""),
            title=p.get("title"),
            description=p.get("description"),
            arguments=_parse_prompt_arguments(p.get("arguments"))
        )
        for p in prompts_raw
    ]
    
    # Create analysis output
    analysis_output = MCPAnalysisOutput(
        status="SUCCESS",
        tools_analyzed=tools_analyzed,
        resources_analyzed=resources_analyzed,
        prompts_analyzed=prompts_analyzed,
        analysis_result=analysis_result
    )
    
    return analysis_output.model_dump()


def _parse_prompt_arguments(arguments: Any) -> List[MCPToolParameter] | None:
    """Convert normalized prompt-argument dicts into MCPToolParameter objects."""
    if not arguments or not isinstance(arguments, list):
        return None
    parsed = []
    for a in arguments:
        if not isinstance(a, dict) or not a.get("name"):
            continue
        parsed.append(MCPToolParameter(
            name=a["name"],
            type="string",
            description=a.get("description"),
            required=bool(a.get("required", False)),
        ))
    return parsed or None


def _parse_tool_parameters(input_schema: Dict[str, Any]) -> List[MCPToolParameter]:
    """Parse tool parameters from JSON schema."""
    parameters = []
    
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])
    
    for param_name, param_schema in properties.items():
        raw_type = param_schema.get("type", "string")
        if isinstance(raw_type, (list, tuple)):
            raw_type = "|".join(str(t) for t in raw_type)
        param = MCPToolParameter(
            name=param_name,
            type=raw_type,
            description=param_schema.get("description"),
            required=param_name in required,
            default=param_schema.get("default"),
            enum=param_schema.get("enum"),
            properties=param_schema.get("properties")
        )
        parameters.append(param)
    
    return parameters


def _extract_analysis_from_text(text: str) -> Dict[str, Any]:
    """Fallback: Extract analysis data from unstructured text."""
    # Basic fallback structure
    return {
        "tool_categories": [],
        "complexity_score": 5.0,
        "primary_use_case": "Unknown",
        "strengths": [],
        "limitations": [],
        "recommendations": [],
        "relationships": [],
        "tool_examples": {}
    }


def _create_fallback_analysis(
    server_name: str,
    tools_raw: List[Dict[str, Any]],
    resources_raw: List[Dict[str, Any]],
    prompts_raw: List[Dict[str, Any]],
    error_message: str
) -> Dict[str, Any]:
    """Create fallback analysis when LLM analysis fails."""
    
    # Simple categorization
    tools_analyzed = []
    for tool_raw in tools_raw:
        parameters = _parse_tool_parameters(tool_raw.get("inputSchema", {}))
        tool = MCPTool(
            name=tool_raw.get("name", "unnamed"),
            description=tool_raw.get("description"),
            parameters=parameters,
            category="Uncategorized",
            examples=[]
        )
        tools_analyzed.append(tool)
    
    resources_analyzed = [
        MCPResource(
            uri=r.get("uri", ""),
            name=r.get("name", ""),
            description=r.get("description"),
            mimeType=r.get("mimeType")
        )
        for r in resources_raw
    ]
    
    prompts_analyzed = [
        MCPPrompt(
            name=p.get("name", ""),
            title=p.get("title"),
            description=p.get("description"),
            arguments=_parse_prompt_arguments(p.get("arguments"))
        )
        for p in prompts_raw
    ]
    
    analysis_result = MCPAnalysisResult(
        tool_categories=[
            ToolCategoryAnalysis(
                category_name="Uncategorized",
                tool_count=len(tools_raw),
                tools=[t.get("name", "") for t in tools_raw],
                description="Analysis failed, tools not categorized"
            )
        ],
        complexity_score=5.0,
        primary_use_case="Unknown (analysis failed)",
        strengths=[],
        limitations=[],
        recommendations=[],
        relationships=[]
    )
    
    analysis_output = MCPAnalysisOutput(
        status="FAILURE",
        tools_analyzed=tools_analyzed,
        resources_analyzed=resources_analyzed,
        prompts_analyzed=prompts_analyzed,
        analysis_result=analysis_result,
        error_message=error_message
    )
    
    return analysis_output.model_dump()


if __name__ == "__main__":
    """
    Test the analysis agent with sample discovery data.
    """
    import asyncio
    
    # Sample discovery data
    test_discovery_data = {
        "server_name": "test-server",
        "tools_discovered": [
            {
                "name": "navigate",
                "description": "Navigate to a URL",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Target URL"}
                    },
                    "required": ["url"]
                }
            }
        ],
        "resources_discovered": [],
        "prompts_discovered": []
    }
    
    # Run analysis
    result = asyncio.run(mcp_analysis_agent(test_discovery_data))
    
    # Print results
    print("\n" + "="*60)
    print("ANALYSIS RESULTS")
    print("="*60)
    print(json.dumps(result, indent=2))

