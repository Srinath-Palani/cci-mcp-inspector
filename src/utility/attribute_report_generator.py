"""
Attribute Report Generator

Generates human-readable checklist and table reports from MCP server attributes.
Supports multiple output formats: Markdown, HTML, CSV, and Console.
"""

from typing import Dict, Any, List
from pathlib import Path
import json
import re


class AttributeReportGenerator:
    """Generate various report formats from MCP server attributes."""
    
    def __init__(self, inspection_report: Dict[str, Any]):
        """
        Initialize with an inspection report.
        
        Args:
            inspection_report: Complete inspection report dictionary
        """
        self.report = inspection_report
        server_attributes = inspection_report.get('server_attributes')
        self.attributes = server_attributes if server_attributes is not None else {}
        self.server_name = inspection_report.get('server_name', 'Unknown')
        
        # Get tools from report (prefer analyzed, fallback to discovered)
        self.tools = inspection_report.get('tools', [])
        if not self.tools:
            # Try to get from discovery metadata if available
            discovery_data = inspection_report.get('metadata', {}).get('discovery_data', {})
            if discovery_data:
                tools_discovered = discovery_data.get('tools_discovered', [])
                if tools_discovered:
                    # Convert discovered tools to tool format
                    self.tools = [
                        {
                            'name': tool.get('name', 'Unknown'),
                            'description': tool.get('description', ''),
                            'inputSchema': tool.get('inputSchema', {})
                        }
                        for tool in tools_discovered
                    ]
        
        self.resources = inspection_report.get('resources', [])
        if not self.resources:
            discovery_data = inspection_report.get('metadata', {}).get('discovery_data', {})
            if discovery_data:
                resources_discovered = discovery_data.get('resources_discovered', [])
                if resources_discovered:
                    self.resources = [
                        {
                            'name': res.get('name', 'Unknown'),
                            'description': res.get('description', ''),
                            'uri': res.get('uri', '')
                        }
                        for res in resources_discovered
                    ]
        
        self.prompts = inspection_report.get('prompts', [])
        if not self.prompts:
            discovery_data = inspection_report.get('metadata', {}).get('discovery_data', {})
            if discovery_data:
                prompts_discovered = discovery_data.get('prompts_discovered', [])
                if prompts_discovered:
                    self.prompts = [
                        {
                            'name': prompt.get('name', 'Unknown'),
                            'description': prompt.get('description', '')
                        }
                        for prompt in prompts_discovered
                    ]
    
    def _safe_get(self, value, default=None):
        """Safely get a value, returning default if value is None."""
        return value if value is not None else default
    
    def _categorize_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Categorize tools by functionality.
        
        Returns:
            Dictionary mapping category names to lists of tools
        """
        categories = {
            "Issue Management": [],
            "Comment Handling": [],
            "Project Management": [],
            "Team & Workspace Metadata": [],
            "Search & Query Utilities": [],
            "Admin & Miscellaneous": [],
            "Other": []
        }
        
        # Keywords for each category
        issue_keywords = ["issue", "bug", "ticket", "task", "item"]
        comment_keywords = ["comment", "note", "reply", "discussion"]
        project_keywords = ["project", "milestone", "sprint", "cycle", "roadmap"]
        team_keywords = ["team", "user", "member", "workspace", "workflow", "state", "label", "assignee"]
        search_keywords = ["search", "find", "query", "list", "get", "retrieve", "fetch"]
        admin_keywords = ["viewer", "info", "settings", "config", "admin", "workspace"]
        
        for tool in self.tools:
            tool_name = (tool.get('name') or '').lower()
            tool_desc = (tool.get('description') or '').lower()
            tool_text = f"{tool_name} {tool_desc}"
            
            categorized = False
            
            # Issue Management
            if any(kw in tool_text for kw in issue_keywords):
                if any(kw in tool_text for kw in ["create", "add", "new", "post"]):
                    categories["Issue Management"].append({**tool, "action": "Create Issue"})
                elif any(kw in tool_text for kw in ["update", "modify", "edit", "change", "set"]):
                    categories["Issue Management"].append({**tool, "action": "Update Issue"})
                elif any(kw in tool_text for kw in ["delete", "remove", "archive", "close"]):
                    categories["Issue Management"].append({**tool, "action": "Delete/Archive Issue"})
                elif any(kw in tool_text for kw in ["assign", "assignee"]):
                    categories["Issue Management"].append({**tool, "action": "Assign Issue"})
                elif any(kw in tool_text for kw in search_keywords):
                    categories["Issue Management"].append({**tool, "action": "Search Issues"})
                else:
                    categories["Issue Management"].append({**tool, "action": tool.get('name', 'Unknown')})
                categorized = True
            
            # Comment Handling
            if not categorized and any(kw in tool_text for kw in comment_keywords):
                if any(kw in tool_text for kw in ["create", "add", "post", "write"]):
                    categories["Comment Handling"].append({**tool, "action": "Add Comment"})
                elif any(kw in tool_text for kw in search_keywords):
                    categories["Comment Handling"].append({**tool, "action": "Search Comments"})
                else:
                    categories["Comment Handling"].append({**tool, "action": tool.get('name', 'Unknown')})
                categorized = True
            
            # Project Management
            if not categorized and any(kw in tool_text for kw in project_keywords):
                if any(kw in tool_text for kw in ["create", "add", "new", "start"]):
                    categories["Project Management"].append({**tool, "action": "Create Project"})
                elif any(kw in tool_text for kw in ["update", "modify", "edit", "change"]):
                    categories["Project Management"].append({**tool, "action": "Update Project"})
                elif any(kw in tool_text for kw in ["update", "change", "history"]):
                    categories["Project Management"].append({**tool, "action": "Search Project Updates"})
                elif any(kw in tool_text for kw in search_keywords):
                    categories["Project Management"].append({**tool, "action": "Search Projects"})
                else:
                    categories["Project Management"].append({**tool, "action": tool.get('name', 'Unknown')})
                categorized = True
            
            # Team & Workspace Metadata
            if not categorized and any(kw in tool_text for kw in team_keywords):
                if "team" in tool_text:
                    categories["Team & Workspace Metadata"].append({**tool, "action": "Get Teams"})
                elif "workflow" in tool_text or "state" in tool_text:
                    categories["Team & Workspace Metadata"].append({**tool, "action": "Get Workflow States"})
                elif "label" in tool_text:
                    categories["Team & Workspace Metadata"].append({**tool, "action": "Get Labels"})
                elif "user" in tool_text:
                    categories["Team & Workspace Metadata"].append({**tool, "action": "Get Users"})
                else:
                    categories["Team & Workspace Metadata"].append({**tool, "action": tool.get('name', 'Unknown')})
                categorized = True
            
            # Search & Query Utilities
            if not categorized and any(kw in tool_text for kw in search_keywords):
                if "roadmap" in tool_text:
                    categories["Search & Query Utilities"].append({**tool, "action": "Search Roadmaps"})
                elif "cycle" in tool_text or "sprint" in tool_text:
                    categories["Search & Query Utilities"].append({**tool, "action": "Search Cycles"})
                elif "document" in tool_text or "doc" in tool_text:
                    categories["Search & Query Utilities"].append({**tool, "action": "Search Documents"})
                elif "notification" in tool_text:
                    categories["Search & Query Utilities"].append({**tool, "action": "Search Notifications"})
                else:
                    categories["Search & Query Utilities"].append({**tool, "action": tool.get('name', 'Unknown')})
                categorized = True
            
            # Admin & Miscellaneous
            if not categorized and any(kw in tool_text for kw in admin_keywords):
                if "viewer" in tool_text:
                    categories["Admin & Miscellaneous"].append({**tool, "action": "Get Viewer"})
                elif "workspace" in tool_text and "info" in tool_text:
                    categories["Admin & Miscellaneous"].append({**tool, "action": "Get Workspace Info"})
                else:
                    categories["Admin & Miscellaneous"].append({**tool, "action": tool.get('name', 'Unknown')})
                categorized = True
            
            # Other
            if not categorized:
                categories["Other"].append({**tool, "action": tool.get('name', 'Unknown')})
        
        # Remove empty categories
        return {k: v for k, v in categories.items() if v}
    
    def _categorize_non_readonly_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Identify and categorize non-read-only tools (write/update/delete operations).
        
        Returns:
            Dictionary mapping operation categories to lists of tools
        """
        categories = {
            "Design Operations": [],
            "Import/Export Tools": [],
            "Organization Tools": [],
            "Collaboration Tools": [],
            "Content Management": [],
            "Configuration Tools": [],
            "Other Write Operations": []
        }
        
        # Keywords that indicate write operations
        write_keywords = [
            "create", "add", "new", "post", "write", "generate", "make",
            "update", "modify", "edit", "change", "set", "adjust", "resize",
            "delete", "remove", "archive", "close", "destroy",
            "import", "export", "upload", "download",
            "move", "copy", "organize", "reorganize",
            "comment", "reply", "respond", "mention",
            "assign", "unassign", "transfer",
            "publish", "unpublish", "share", "unshare",
            "configure", "setup", "install", "uninstall"
        ]
        
        # Keywords for each category
        design_keywords = ["design", "template", "layout", "visual", "image", "graphic", "canvas", "artwork"]
        import_export_keywords = ["import", "export", "upload", "download", "transfer", "sync"]
        organization_keywords = ["folder", "organize", "move", "copy", "reorganize", "structure", "categorize", "tag"]
        collaboration_keywords = ["comment", "reply", "mention", "share", "collaborate", "team", "discuss", "feedback"]
        content_keywords = ["content", "document", "file", "asset", "resource", "media"]
        config_keywords = ["configure", "setup", "settings", "config", "preference", "option"]
        
        for tool in self.tools:
            tool_name = (tool.get('name') or '').lower()
            tool_desc = (tool.get('description') or '').lower()
            tool_text = f"{tool_name} {tool_desc}"
            
            # Check if this is a write operation
            # First, check the tool name - if it starts with read-only keywords, it's likely read-only
            read_only_name_prefixes = ["read", "get", "list", "search", "find", "retrieve", "fetch", "view", "show", "display"]
            tool_name_starts_with_readonly = any(tool_name.startswith(prefix) for prefix in read_only_name_prefixes)
            
            # Check if tool name or description contains write keywords
            has_write_keywords = any(kw in tool_text for kw in write_keywords)
            
            # Check if tool name or description contains read-only keywords
            read_only_keywords = ["get", "list", "search", "find", "retrieve", "fetch", "read", "view", "show", "display"]
            has_read_only_keywords = any(kw in tool_text for kw in read_only_keywords)
            
            # Tool is read-only if:
            # 1. Tool name starts with read-only prefix (e.g., "read-", "get-", "list-")
            # 2. OR it has read-only keywords but NO write keywords
            is_read_only = tool_name_starts_with_readonly or (has_read_only_keywords and not has_write_keywords)
            
            # Tool is a write operation if it has write keywords and is not read-only
            is_write_operation = has_write_keywords and not is_read_only
            
            # If it's a write operation and not read-only, categorize it
            if is_write_operation and not is_read_only:
                categorized = False
                
                # Design Operations
                if any(kw in tool_text for kw in design_keywords):
                    if any(kw in tool_text for kw in ["generate", "create", "new", "make"]):
                        categories["Design Operations"].append({**tool, "operation": "Create/Generate Design"})
                    elif any(kw in tool_text for kw in ["resize", "adjust", "modify", "update"]):
                        categories["Design Operations"].append({**tool, "operation": "Modify Design"})
                    elif any(kw in tool_text for kw in ["import", "upload"]):
                        categories["Design Operations"].append({**tool, "operation": "Import Design"})
                    elif any(kw in tool_text for kw in ["move", "organize"]):
                        categories["Design Operations"].append({**tool, "operation": "Organize Design"})
                    else:
                        categories["Design Operations"].append({**tool, "operation": tool.get('name', 'Unknown')})
                    categorized = True
                
                # Import/Export Tools
                if not categorized and any(kw in tool_text for kw in import_export_keywords):
                    if any(kw in tool_text for kw in ["import", "upload"]):
                        categories["Import/Export Tools"].append({**tool, "operation": "Import Content"})
                    elif any(kw in tool_text for kw in ["export", "download"]):
                        categories["Import/Export Tools"].append({**tool, "operation": "Export Content"})
                    else:
                        categories["Import/Export Tools"].append({**tool, "operation": tool.get('name', 'Unknown')})
                    categorized = True
                
                # Organization Tools
                if not categorized and any(kw in tool_text for kw in organization_keywords):
                    if any(kw in tool_text for kw in ["create", "new", "make"]):
                        categories["Organization Tools"].append({**tool, "operation": "Create Folder/Structure"})
                    elif any(kw in tool_text for kw in ["move", "copy", "reorganize"]):
                        categories["Organization Tools"].append({**tool, "operation": "Move/Reorganize Items"})
                    else:
                        categories["Organization Tools"].append({**tool, "operation": tool.get('name', 'Unknown')})
                    categorized = True
                
                # Collaboration Tools
                if not categorized and any(kw in tool_text for kw in collaboration_keywords):
                    if any(kw in tool_text for kw in ["comment", "add comment"]):
                        categories["Collaboration Tools"].append({**tool, "operation": "Add Comment"})
                    elif any(kw in tool_text for kw in ["reply", "respond"]):
                        categories["Collaboration Tools"].append({**tool, "operation": "Reply to Comment"})
                    elif any(kw in tool_text for kw in ["share", "collaborate"]):
                        categories["Collaboration Tools"].append({**tool, "operation": "Share/Collaborate"})
                    else:
                        categories["Collaboration Tools"].append({**tool, "operation": tool.get('name', 'Unknown')})
                    categorized = True
                
                # Content Management
                if not categorized and any(kw in tool_text for kw in content_keywords):
                    if any(kw in tool_text for kw in ["create", "new", "add"]):
                        categories["Content Management"].append({**tool, "operation": "Create Content"})
                    elif any(kw in tool_text for kw in ["update", "modify", "edit"]):
                        categories["Content Management"].append({**tool, "operation": "Update Content"})
                    elif any(kw in tool_text for kw in ["delete", "remove"]):
                        categories["Content Management"].append({**tool, "operation": "Delete Content"})
                    else:
                        categories["Content Management"].append({**tool, "operation": tool.get('name', 'Unknown')})
                    categorized = True
                
                # Configuration Tools
                if not categorized and any(kw in tool_text for kw in config_keywords):
                    categories["Configuration Tools"].append({**tool, "operation": tool.get('name', 'Unknown')})
                    categorized = True
                
                # Other Write Operations
                if not categorized:
                    categories["Other Write Operations"].append({**tool, "operation": tool.get('name', 'Unknown')})
        
        # Remove empty categories
        return {k: v for k, v in categories.items() if v}
    
    def _summarize_description(self, description: str, max_length: int = 120) -> str:
        """
        Summarize a long description to a concise sentence.
        
        Args:
            description: Full description text
            max_length: Maximum length for the summary
            
        Returns:
            Summarized description as a short sentence
        """
        if not description:
            return ""
        
        description = str(description)  # Ensure it's a string
        
        # Remove extra whitespace and newlines
        description = " ".join(description.split())
        
        # If already short enough, return as is
        if len(description) <= max_length:
            return description
        
        # Strategy 1: Extract the first sentence if it's meaningful
        first_sentence = description.split('.')[0].strip()
        if first_sentence and len(first_sentence) <= max_length and len(first_sentence) > 20:
            # Check if first sentence is meaningful (not just a fragment)
            if len(first_sentence.split()) >= 5:
                # Add period to make it a complete sentence
                if not first_sentence.endswith('.'):
                    first_sentence += '.'
                return first_sentence
        
        # Strategy 2: Look for key summary phrases
        summary_patterns = [
            r"^(This (tool|resource|prompt|command|function|utility|feature|instruction|guide|documentation|migration).*?\.)",
            r"^(Use this (when|to|for|if).*?\.)",
            r"^(Read (the|a|specific|this).*?\.)",
            r"^(Create (a|new|an).*?\.)",
            r"^(Get (the|a|all|available).*?\.)",
            r"^(Search (for|the|a).*?\.)",
            r"^(Instructions (for|on|to).*?\.)",
            r"^(Detailed (instructions|guide|information|documentation).*?\.)",
        ]
        
        for pattern in summary_patterns:
            match = re.match(pattern, description, re.IGNORECASE)
            if match:
                summary = match.group(1).strip()
                if len(summary) <= max_length:
                    return summary
        
        # Strategy 3: Extract first meaningful clause
        for delimiter in [', ', '; ', '.\n', '.\t']:
            if delimiter in description:
                first_part = description.split(delimiter)[0].strip()
                if first_part and len(first_part) <= max_length and len(first_part) > 15:
                    if not first_part.endswith('.'):
                        first_part += '.'
                    return first_part
        
        # Strategy 4: Extract first sentence, make it concise
        sentences = description.split('.')
        if sentences and sentences[0]:
            first = sentences[0].strip() or ''
            if len(first) > max_length:
                action_words = ['read', 'create', 'get', 'search', 'find', 'update', 'delete', 
                              'import', 'export', 'move', 'copy', 'generate', 'resize', 'modify']
                words = first.lower().split()
                for i, word in enumerate(words):
                    if word in action_words and i < len(words) - 1:
                        start_idx = first.lower().find(word)
                        remaining = first[start_idx:]
                        for delim in ['.', ',', ';']:
                            if delim in remaining:
                                summary = remaining.split(delim, 1)[0].strip()
                                if len(summary) <= max_length and len(summary) > 10:
                                    if not summary.endswith('.'):
                                        summary += '.'
                                    return summary
                        if len(remaining) <= max_length:
                            return remaining + '.'
                        else:
                            summary = remaining[:max_length-3].rsplit(' ', 1)[0] + '.'
                            return summary
            
            if len(first) <= max_length:
                if not first.endswith('.'):
                    first += '.'
                return first
        
        # Strategy 5: Fallback - key words
        words = description.split()
        key_words = []
        for word in words[:15]:
            if len(word) > 3 and word.lower() not in ['the', 'this', 'that', 'with', 'from', 'when', 'where']:
                key_words.append(word)
                if len(' '.join(key_words)) > max_length - 10:
                    break
        
        summary = ' '.join(key_words[:8])
        if len(summary) > max_length:
            summary = summary[:max_length-3].rsplit(' ', 1)[0]
        
        if summary and not summary.endswith('.'):
            summary += '.'
        
        return summary if summary else description[:max_length]
    
    def _generate_capabilities_details(self) -> str:
        """Generate detailed capabilities section."""
        details = "\n## Capabilities Details\n\n"
        
        # Tools
        if self.tools:
            details += "### Tools\n\n"
            categorized = self._categorize_tools()
            
            for category, tools in categorized.items():
                if tools:
                    details += f"**{category}**\n"
                    for tool in tools:
                        action = tool.get('action', tool.get('name', 'Unknown'))
                        tool_name = tool.get('name', 'Unknown')
                        tool_desc = tool.get('description', '')
                        details += f"  • **{action}** – {tool_desc if tool_desc else tool_name}\n"
                    details += "\n"
        else:
            details += "### Tools\n\nNo tools available.\n\n"
        
        # Non-Read-Only Tools (Write Operations)
        if self.tools:
            non_readonly = self._categorize_non_readonly_tools()
            if non_readonly:
                details += "### Non-Read-Only Tools (Write Operations)\n\n"
                for category, tools in non_readonly.items():
                    if tools:
                        details += f"**{category}**\n"
                        for tool in tools:
                            operation = tool.get('operation', tool.get('name', 'Unknown'))
                            tool_name = tool.get('name', 'Unknown')
                            tool_desc = tool.get('description', '')
                            details += f"  • **{tool_name}**: {tool_desc if tool_desc else operation}\n"
                        details += "\n"
        
        # Resources
        if self.resources:
            details += "### Resources\n\n"
            for resource in self.resources:
                resource_name = resource.get('name', 'Unknown')
                resource_desc = resource.get('description', '')
                resource_uri = resource.get('uri', '')
                resource_mime = resource.get('mimeType', '')
                resource_size = resource.get('size')
                details += f"  • **{resource_name}**"
                if resource_uri:
                    details += f" (`{resource_uri}`)"
                if resource_mime:
                    details += f" [{resource_mime}]"
                if resource_size is not None:
                    details += f" ({resource_size} bytes)"
                if resource_desc:
                    details += f" – {resource_desc}"
                details += "\n"
            details += "\n"
        else:
            details += "### Resources\n\nNo resources available.\n\n"

        # Prompts (with full argument detail)
        if self.prompts:
            details += "### Prompts\n\n"
            for prompt in self.prompts:
                prompt_name = prompt.get('name', 'Unknown')
                prompt_desc = prompt.get('description', '')
                details += f"  • **{prompt_name}**"
                if prompt_desc:
                    details += f" – {prompt_desc}"
                details += "\n"
                for arg in (prompt.get('arguments') or []):
                    if not isinstance(arg, dict):
                        continue
                    arg_name = arg.get('name', '')
                    arg_req = ' (required)' if arg.get('required') else ''
                    arg_desc = arg.get('description') or ''
                    details += f"      - `{arg_name}`{arg_req}"
                    if arg_desc:
                        details += f": {arg_desc}"
                    details += "\n"
            details += "\n"
        else:
            details += "### Prompts\n\nNo prompts available.\n\n"

        # Declared-only capabilities + mismatches (from the handshake)
        report_caps = self.report.get('capabilities') or {}
        details += "### Server-Declared Capabilities\n\n"
        details += f"  • Sampling: {'Yes' if report_caps.get('sampling') else 'No'} (declared in handshake)\n"
        details += f"  • Logging: {'Yes' if report_caps.get('logging') else 'No'}\n"
        details += f"  • Completions: {'Yes' if report_caps.get('completions') else 'No'}\n"
        experimental = report_caps.get('experimental')
        if experimental:
            details += f"  • Experimental: {json.dumps(experimental)}\n"
        mismatches = [
            name for name in ('resources', 'prompts')
            if report_caps.get(f'{name}_mismatch')
        ]
        if mismatches:
            details += (
                f"\n  ⚠️ Capability mismatch: the server answers {', '.join(mismatches)} "
                f"list calls but did not declare the capability in the initialize handshake.\n"
            )
        details += "\n"

        return details
    
    def generate_markdown_checklist(self) -> str:
        """Generate a Markdown checklist report."""
        
        md = f"# MCP Server Attribute Checklist\n\n"
        md += f"**Server**: {self.server_name}\n"
        md += f"**Inspection Date**: {self.report.get('discovery_timestamp', 'N/A')}\n\n"
        md += "---\n\n"
        
        # Server info — above Distribution Type. Input-sourced identity fields
        # (name, endpoint, GitHub repo) plus the resolved traffic name.
        sinfo = self.attributes.get('server_info') or {}
        server_input = (self.report.get('metadata') or {}).get('server_input') or {}
        t_name = sinfo.get('traffic_name')
        t_src = sinfo.get('traffic_name_source')
        md += "## Server Info\n\n"
        md += f"- **Name**: {self.server_name}\n"
        md += f"- **Endpoint URL**: {server_input.get('endpoint_url') or 'N/A'}\n"
        md += f"- **GitHub Repository**: {server_input.get('repository') or 'N/A'}\n"
        md += f"- **Traffic name**: `{t_name or 'NA'}`" + (f" (source: {t_src})" if t_name and t_src else "") + "\n"
        if not t_name:
            md += "  - _(traffic name unconfirmed — no proxy source configured and no vendor/registry/handshake name found)_\n"
        md += "\n"

        # Distribution Type
        md += "## Distribution Type\n\n"
        dist = self.attributes.get('distribution_type') or {}
        md += f"- [{'x' if dist.get('official') else ' '}] Official\n"
        md += f"- [{'x' if dist.get('community') else ' '}] Community\n\n"
        
        # MCP Protocol Version
        md += "## MCP Protocol Version\n\n"
        protocol = self.attributes.get('protocol_version') or {}
        from src.models.structured_output import SUPPORTED_PROTOCOL_VERSIONS, protocol_version_field
        for version in reversed(SUPPORTED_PROTOCOL_VERSIONS):
            md += f"- [{'x' if protocol.get(protocol_version_field(version)) else ' '}] {version}\n"
        if protocol.get('detected_version'):
            md += f"\n**Detected Version**: `{protocol.get('detected_version')}`\n"
        md += "\n"
        
        # Pricing
        md += "## Pricing\n\n"
        pricing = self.attributes.get('pricing') or {}
        md += f"- [{'x' if pricing.get('free') else ' '}] Free\n"
        md += f"- [{'x' if pricing.get('paid') else ' '}] Paid\n\n"
        
        # Hosting Provider
        md += "## Hosting Provider\n\n"
        hosting = self.attributes.get('hosting_provider') or {}
        md += f"- [{'x' if hosting.get('saas_vendor') else ' '}] SaaS Vendor\n"
        md += f"- [{'x' if hosting.get('third_party_saas') else ' '}] 3rd Party SaaS vendor\n"
        md += f"- [{'x' if hosting.get('github') else ' '}] GitHub\n"
        md += f"- [{'x' if hosting.get('gitlab') else ' '}] GitLab\n"
        md += f"- [{'x' if hosting.get('bitbucket') else ' '}] Bitbucket\n"
        md += f"- [{'x' if hosting.get('sourcehut_gitea_gogs') else ' '}] SourceHut/Gitea/Gogs\n\n"
        
        # Authentication
        md += "## Authentication\n\n"
        auth = self.attributes.get('authentication') or {}
        md += f"- [{'x' if auth.get('oauth2_1_authorization_code') else ' '}] OAuth 2.1 - Authorization Code Flow\n"
        md += f"- [{'x' if auth.get('oauth2_1_client_credentials') else ' '}] OAuth 2.1 - Client Credentials Flow\n"
        md += f"- [{'x' if auth.get('bearer_token') else ' '}] Bearer Token\n"
        md += f"- [{'x' if auth.get('personal_access_token') else ' '}] Personal Access Token\n"
        md += f"- [{'x' if auth.get('api_token') else ' '}] API Token\n\n"
        
        # Data Protection (TLS)
        md += "## Data Protection through Encryption with TLS\n\n"
        tls = self.attributes.get('data_protection') or {}
        md += f"- [{'x' if tls.get('tls_1_3') else ' '}] TLS 1.3\n"
        md += f"- [{'x' if tls.get('tls_1_2') else ' '}] TLS 1.2\n"
        md += f"- [{'x' if tls.get('lower_or_no_encryption') else ' '}] Lower versions or no encryption\n\n"
        
        # Transport Protocol
        md += "## Transport Protocol\n\n"
        transport = self.attributes.get('transport_protocol') or {}
        md += f"- [{'x' if transport.get('stdio') else ' '}] STDIO\n"
        md += f"- [{'x' if transport.get('http_sse') else ' '}] HTTP/SSE\n"
        md += f"- [{'x' if transport.get('streamable_http') else ' '}] StreamableHttp\n"
        md += f"- [{'x' if transport.get('fast_api') else ' '}] FastAPI\n\n"
        
        # Tools Operations Type
        md += "## Tools Operations Type\n\n"
        ops = self.attributes.get('tools_operation_type') or {}
        md += f"- [{'x' if ops.get('read_only') else ' '}] Read-only operations tools\n"
        md += f"- [{'x' if ops.get('read_update') else ' '}] Has read-only and/or update operations tools\n"
        md += f"- [{'x' if ops.get('read_update_delete') else ' '}] Has read-only, update and/or delete operations tools\n\n"
        
        # Deployment Approach
        md += "## Deployment Approach\n\n"
        deploy = self.attributes.get('deployment_approach') or {}
        md += f"- [{'x' if deploy.get('local') else ' '}] Local\n"
        md += f"- [{'x' if deploy.get('container') else ' '}] Container\n"
        md += f"- [{'x' if deploy.get('remote') else ' '}] Remote\n\n"
        
        # Capabilities
        md += "## Capabilities\n\n"
        capabilities = self.report.get('capabilities', {})
        md += f"- [{'x' if capabilities.get('tools') else ' '}] Tools\n"
        md += f"- [{'x' if capabilities.get('resources') else ' '}] Resources\n"
        md += f"- [{'x' if capabilities.get('prompts') else ' '}] Prompts\n"
        md += f"- [{'x' if capabilities.get('sampling') else ' '}] Sampling\n\n"
        
        # Statistics
        stats = self.report.get('statistics', {})
        if stats:
            md += "## Statistics\n\n"
            md += f"- **Total Tools**: {stats.get('total_tools', 0)}\n"
            md += f"- **Total Resources**: {stats.get('total_resources', 0)}\n"
            md += f"- **Total Prompts**: {stats.get('total_prompts', 0)}\n\n"
        
        # Capabilities Details
        md += self._generate_capabilities_details()
        
        return md
    
    def generate_html_table(self) -> str:
        """Generate an HTML table report."""
        
        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>MCP Server Attributes - {self.server_name}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1 {{ color: #333; }}
        .info {{ margin-bottom: 20px; color: #666; }}
        table {{ border-collapse: collapse; width: 100%; margin-bottom: 30px; }}
        th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
        th {{ background-color: #4CAF50; color: white; }}
        tr:nth-child(even) {{ background-color: #f2f2f2; }}
        .yes {{ color: green; font-weight: bold; }}
        .no {{ color: #ccc; }}
        .highlight {{ background-color: #90EE90; }}
    </style>
</head>
<body>
    <h1>MCP Server Attribute Report</h1>
    <div class="info">
        <p><strong>Server Name:</strong> {self.server_name}</p>
        <p><strong>Inspection Date:</strong> {self.report.get('discovery_timestamp', 'N/A')}</p>
        <p><strong>Connection Type:</strong> {self.report.get('connection_type', 'N/A')}</p>
    </div>
"""
        
        # Create table
        html += """    <table>
        <tr>
            <th>Category</th>
            <th>Attribute</th>
            <th>Status</th>
        </tr>
"""
        
        rows = self._generate_table_rows()
        html += rows
        
        html += """    </table>
</body>
</html>
"""
        return html
    
    def generate_csv(self) -> str:
        """Generate a CSV report."""
        
        # Use separate CSV files or sections - for now, use a single file with different column structures
        # Attributes section uses: Category,Attribute,Status
        # Capabilities section uses: Type,Name,Category,Description
        # Non-Read-Only Tools section uses: Type,Name,Category,Operation,Description
        
        csv = "Category,Attribute,Status\n"

        # Server info — above Distribution Type. Input-sourced identity fields
        # (name, endpoint, GitHub repo) plus the resolved traffic name.
        sinfo = self.attributes.get('server_info') or {}
        server_input = (self.report.get('metadata') or {}).get('server_input') or {}
        t_name = sinfo.get('traffic_name') or ""
        csv += f"Server Info,Name,{self.server_name}\n"
        csv += f"Server Info,Endpoint URL,{server_input.get('endpoint_url') or 'NA'}\n"
        csv += f"Server Info,GitHub Repository,{server_input.get('repository') or 'NA'}\n"
        csv += f"Server Info,Traffic name,{t_name if t_name else 'NA'}\n"

        # Distribution Type
        dist = self.attributes.get('distribution_type') or {}
        csv += f"Distribution Type,Official,{'Yes' if dist.get('official') else 'No'}\n"
        csv += f"Distribution Type,Community,{'Yes' if dist.get('community') else 'No'}\n"
        
        # Protocol Version
        protocol = self.attributes.get('protocol_version') or {}
        from src.models.structured_output import SUPPORTED_PROTOCOL_VERSIONS, protocol_version_field
        for version in SUPPORTED_PROTOCOL_VERSIONS:
            csv += f"MCP Protocol Version,{version},{'Yes' if protocol.get(protocol_version_field(version)) else 'No'}\n"
        
        # Pricing
        pricing = self.attributes.get('pricing') or {}
        csv += f"Pricing,Free,{'Yes' if pricing.get('free') else 'No'}\n"
        csv += f"Pricing,Paid,{'Yes' if pricing.get('paid') else 'No'}\n"
        
        # Hosting Provider
        hosting = self.attributes.get('hosting_provider') or {}
        csv += f"Hosting Provider,SaaS Vendor,{'Yes' if hosting.get('saas_vendor') else 'No'}\n"
        csv += f"Hosting Provider,3rd Party SaaS vendor,{'Yes' if hosting.get('third_party_saas') else 'No'}\n"
        csv += f"Hosting Provider,GitHub,{'Yes' if hosting.get('github') else 'No'}\n"
        csv += f"Hosting Provider,GitLab,{'Yes' if hosting.get('gitlab') else 'No'}\n"
        csv += f"Hosting Provider,Bitbucket,{'Yes' if hosting.get('bitbucket') else 'No'}\n"
        csv += f"Hosting Provider,SourceHut/Gitea/Gogs,{'Yes' if hosting.get('sourcehut_gitea_gogs') else 'No'}\n"
        
        # Authentication
        auth = self.attributes.get('authentication') or {}
        csv += f"Authentication,OAuth 2.1 - Authorization Code Flow,{'Yes' if auth.get('oauth2_1_authorization_code') else 'No'}\n"
        csv += f"Authentication,OAuth 2.1 - Client Credentials Flow,{'Yes' if auth.get('oauth2_1_client_credentials') else 'No'}\n"
        csv += f"Authentication,Bearer Token,{'Yes' if auth.get('bearer_token') else 'No'}\n"
        csv += f"Authentication,Personal Access Token,{'Yes' if auth.get('personal_access_token') else 'No'}\n"
        csv += f"Authentication,API Token,{'Yes' if auth.get('api_token') else 'No'}\n"
        
        # Data Protection
        tls = self.attributes.get('data_protection') or {}
        csv += f"Data Protection,TLS 1.3,{'Yes' if tls.get('tls_1_3') else 'No'}\n"
        csv += f"Data Protection,TLS 1.2,{'Yes' if tls.get('tls_1_2') else 'No'}\n"
        csv += f"Data Protection,Lower versions or no encryption,{'Yes' if tls.get('lower_or_no_encryption') else 'No'}\n"
        
        # Transport Protocol
        transport = self.attributes.get('transport_protocol') or {}
        csv += f"Transport Protocol,STDIO,{'Yes' if transport.get('stdio') else 'No'}\n"
        csv += f"Transport Protocol,HTTP/SSE,{'Yes' if transport.get('http_sse') else 'No'}\n"
        csv += f"Transport Protocol,StreamableHttp,{'Yes' if transport.get('streamable_http') else 'No'}\n"
        csv += f"Transport Protocol,FastAPI,{'Yes' if transport.get('fast_api') else 'No'}\n"
        
        # Tools Operations
        ops = self.attributes.get('tools_operation_type') or {}
        csv += f"Tools Operations,Read-only operations,{'Yes' if ops.get('read_only') else 'No'}\n"
        csv += f"Tools Operations,Read-only and/or update operations,{'Yes' if ops.get('read_update') else 'No'}\n"
        csv += f"Tools Operations,Read-only update and/or delete operations,{'Yes' if ops.get('read_update_delete') else 'No'}\n"
        
        # Deployment
        deploy = self.attributes.get('deployment_approach') or {}
        csv += f"Deployment Approach,Local,{'Yes' if deploy.get('local') else 'No'}\n"
        csv += f"Deployment Approach,Container,{'Yes' if deploy.get('container') else 'No'}\n"
        csv += f"Deployment Approach,Remote,{'Yes' if deploy.get('remote') else 'No'}\n"
        
        # Capabilities
        capabilities = self.report.get('capabilities', {})
        csv += f"Capabilities,Tools,{'Yes' if capabilities.get('tools') else 'No'}\n"
        csv += f"Capabilities,Resources,{'Yes' if capabilities.get('resources') else 'No'}\n"
        csv += f"Capabilities,Prompts,{'Yes' if capabilities.get('prompts') else 'No'}\n"
        csv += f"Capabilities,Sampling,{'Yes' if capabilities.get('sampling') else 'No'}\n"
        
        # Helper function to escape CSV fields
        def escape_csv_field(field: str) -> str:
            """Escape CSV field by wrapping in quotes and escaping internal quotes."""
            if field is None:
                return ""
            # Replace quotes with double quotes
            field = str(field).replace('"', '""')
            # Wrap in quotes if contains comma, newline, or quote
            if ',' in field or '\n' in field or '"' in field:
                return f'"{field}"'
            return field
        
        # Capabilities Details - Tools (one row with all tools formatted)
        if self.tools:
            categorized = self._categorize_tools()
            tools_details = ""
            for category, tools in categorized.items():
                if tools:
                    tools_details += f"{category}\n"
                    for tool in tools:
                        action = tool.get('action', tool.get('name', 'Unknown'))
                        tool_name = tool.get('name', 'Unknown')
                        tool_desc = tool.get('description', '')
                        tools_details += f"  • {action} – {tool_desc if tool_desc else tool_name}\n"
                    tools_details += "\n"
            if tools_details:
                csv += f"Capabilities - Tools,detailed_info,{escape_csv_field(tools_details.strip())}\n"
        
        # Capabilities Details - Resources (one row with all resources formatted)
        if self.resources:
            resources_details = ""
            for resource in self.resources:
                resource_name = resource.get('name', 'Unknown')
                resource_desc = resource.get('description', '')
                resource_uri = resource.get('uri', '')
                resources_details += f"  • {resource_name}"
                if resource_uri:
                    resources_details += f" ({resource_uri})"
                if resource_desc:
                    resources_details += f" – {resource_desc}"
                resources_details += "\n"
            if resources_details:
                csv += f"Capabilities - Resources,detailed_info,{escape_csv_field(resources_details.strip())}\n"
        
        # Capabilities Details - Prompts (one row with all prompts formatted)
        if self.prompts:
            prompts_details = ""
            for prompt in self.prompts:
                prompt_name = prompt.get('name', 'Unknown')
                prompt_desc = prompt.get('description', '')
                prompts_details += f"  • {prompt_name}"
                if prompt_desc:
                    prompts_details += f" – {prompt_desc}"
                prompts_details += "\n"
            if prompts_details:
                csv += f"Capabilities - Prompts,detailed_info,{escape_csv_field(prompts_details.strip())}\n"
        
        # Non-Read-Only Tools (Write Operations) - one row with all non-read-only tools formatted
        if self.tools:
            non_readonly = self._categorize_non_readonly_tools()
            if non_readonly:
                non_readonly_details = ""
                for category, tools in non_readonly.items():
                    if tools:
                        non_readonly_details += f"{category}\n"
                        for tool in tools:
                            operation = tool.get('operation', tool.get('name', 'Unknown'))
                            tool_name = tool.get('name', 'Unknown')
                            tool_desc = tool.get('description', '')
                            non_readonly_details += f"  • {tool_name}: {tool_desc if tool_desc else operation}\n"
                        non_readonly_details += "\n"
                if non_readonly_details:
                    csv += f"Non-Read-Only Tools,detailed_info,{escape_csv_field(non_readonly_details.strip())}\n"
        
        return csv
    
    def generate_console_table(self) -> str:
        """Generate a formatted console table."""
        
        output = "\n"
        output += "="*80 + "\n"
        output += f"  MCP SERVER ATTRIBUTE CHECKLIST - {self.server_name}\n"
        output += "="*80 + "\n\n"
        
        output += f"Server: {self.server_name}\n"
        output += f"Timestamp: {self.report.get('discovery_timestamp', 'N/A')}\n"
        output += f"Connection: {self.report.get('connection_type', 'N/A')}\n\n"
        
        # Table header
        output += f"{'Category':<35} {'Attribute':<45} {'Status':<10}\n"
        output += "-"*80 + "\n"

        # Server info — above Distribution Type. Input-sourced identity fields
        # (name, endpoint, GitHub repo) plus the resolved traffic name.
        sinfo = self.attributes.get('server_info') or {}
        server_input = (self.report.get('metadata') or {}).get('server_input') or {}
        t_name = sinfo.get('traffic_name') or 'NA'
        output += f"{'Server Info':<35} {'Name':<45} {self.server_name:<10}\n"
        output += f"{'':<35} {'Endpoint URL':<45} {(server_input.get('endpoint_url') or 'NA'):<10}\n"
        output += f"{'':<35} {'GitHub Repository':<45} {(server_input.get('repository') or 'NA'):<10}\n"
        output += f"{'':<35} {'Traffic name':<45} {t_name:<10}\n"

        # Distribution Type
        dist = self.attributes.get('distribution_type') or {}
        output += f"{'Distribution Type':<35} {'Official':<45} {'Yes' if dist.get('official') else 'No':<10}\n"
        output += f"{'':<35} {'Community':<45} {'Yes' if dist.get('community') else 'No':<10}\n"
        
        # Protocol Version
        protocol = self.attributes.get('protocol_version') or {}
        from src.models.structured_output import SUPPORTED_PROTOCOL_VERSIONS, protocol_version_field
        for i, version in enumerate(reversed(SUPPORTED_PROTOCOL_VERSIONS)):
            label = 'MCP Protocol Version' if i == 0 else ''
            supported = 'Yes' if protocol.get(protocol_version_field(version)) else 'No'
            output += f"{label:<35} {version:<45} {supported:<10}\n"
        
        # Pricing
        pricing = self.attributes.get('pricing') or {}
        output += f"{'Pricing':<35} {'Free':<45} {'Yes' if pricing.get('free') else 'No':<10}\n"
        output += f"{'':<35} {'Paid':<45} {'Yes' if pricing.get('paid') else 'No':<10}\n"
        
        # Hosting Provider
        hosting = self.attributes.get('hosting_provider') or {}
        output += f"{'Hosting Provider':<35} {'SaaS Vendor':<45} {'Yes' if hosting.get('saas_vendor') else 'No':<10}\n"
        output += f"{'':<35} {'3rd Party SaaS vendor':<45} {'Yes' if hosting.get('third_party_saas') else 'No':<10}\n"
        output += f"{'':<35} {'GitHub':<45} {'Yes' if hosting.get('github') else 'No':<10}\n"
        output += f"{'':<35} {'GitLab':<45} {'Yes' if hosting.get('gitlab') else 'No':<10}\n"
        output += f"{'':<35} {'Bitbucket':<45} {'Yes' if hosting.get('bitbucket') else 'No':<10}\n"
        output += f"{'':<35} {'SourceHut/Gitea/Gogs':<45} {'Yes' if hosting.get('sourcehut_gitea_gogs') else 'No':<10}\n"
        
        # Authentication
        auth = self.attributes.get('authentication') or {}
        output += f"{'Authentication':<35} {'OAuth 2.1 - Authorization Code Flow':<45} {'Yes' if auth.get('oauth2_1_authorization_code') else 'No':<10}\n"
        output += f"{'':<35} {'OAuth 2.1 - Client Credentials Flow':<45} {'Yes' if auth.get('oauth2_1_client_credentials') else 'No':<10}\n"
        output += f"{'':<35} {'Bearer Token':<45} {'Yes' if auth.get('bearer_token') else 'No':<10}\n"
        output += f"{'':<35} {'Personal Access Token':<45} {'Yes' if auth.get('personal_access_token') else 'No':<10}\n"
        output += f"{'':<35} {'API Token':<45} {'Yes' if auth.get('api_token') else 'No':<10}\n"
        
        # Data Protection
        tls = self.attributes.get('data_protection') or {}
        output += f"{'Data Protection (TLS)':<35} {'TLS 1.3':<45} {'Yes' if tls.get('tls_1_3') else 'No':<10}\n"
        output += f"{'':<35} {'TLS 1.2':<45} {'Yes' if tls.get('tls_1_2') else 'No':<10}\n"
        output += f"{'':<35} {'Lower versions or no encryption':<45} {'Yes' if tls.get('lower_or_no_encryption') else 'No':<10}\n"
        
        # Transport Protocol
        transport = self.attributes.get('transport_protocol') or {}
        output += f"{'Transport Protocol':<35} {'STDIO':<45} {'Yes' if transport.get('stdio') else 'No':<10}\n"
        output += f"{'':<35} {'HTTP/SSE':<45} {'Yes' if transport.get('http_sse') else 'No':<10}\n"
        output += f"{'':<35} {'StreamableHttp':<45} {'Yes' if transport.get('streamable_http') else 'No':<10}\n"
        output += f"{'':<35} {'FastAPI':<45} {'Yes' if transport.get('fast_api') else 'No':<10}\n"
        
        # Tools Operations
        ops = self.attributes.get('tools_operation_type') or {}
        output += f"{'Tools Operations Type':<35} {'Read-only operations':<45} {'Yes' if ops.get('read_only') else 'No':<10}\n"
        output += f"{'':<35} {'Read-only and/or update operations':<45} {'Yes' if ops.get('read_update') else 'No':<10}\n"
        output += f"{'':<35} {'Read update and/or delete operations':<45} {'Yes' if ops.get('read_update_delete') else 'No':<10}\n"
        
        # Deployment
        deploy = self.attributes.get('deployment_approach') or {}
        output += f"{'Deployment Approach':<35} {'Local':<45} {'Yes' if deploy.get('local') else 'No':<10}\n"
        output += f"{'':<35} {'Container':<45} {'Yes' if deploy.get('container') else 'No':<10}\n"
        output += f"{'':<35} {'Remote':<45} {'Yes' if deploy.get('remote') else 'No':<10}\n"
        
        # Capabilities
        capabilities = self.report.get('capabilities', {})
        output += f"{'Capabilities':<35} {'Tools':<45} {'Yes' if capabilities.get('tools') else 'No':<10}\n"
        output += f"{'':<35} {'Resources':<45} {'Yes' if capabilities.get('resources') else 'No':<10}\n"
        output += f"{'':<35} {'Prompts':<45} {'Yes' if capabilities.get('prompts') else 'No':<10}\n"
        output += f"{'':<35} {'Sampling':<45} {'Yes' if capabilities.get('sampling') else 'No':<10}\n"
        
        output += "="*80 + "\n\n"
        
        # Statistics
        stats = self.report.get('statistics', {})
        if stats:
            output += f"Statistics:\n"
            output += f"  - Total Tools: {stats.get('total_tools', 0)}\n"
            output += f"  - Total Resources: {stats.get('total_resources', 0)}\n"
            output += f"  - Total Prompts: {stats.get('total_prompts', 0)}\n\n"
        
        # Capabilities Details
        output += "\n" + "="*80 + "\n"
        output += "  CAPABILITIES DETAILS\n"
        output += "="*80 + "\n\n"
        
        # Tools
        if self.tools:
            categorized = self._categorize_tools()
            for category, tools in categorized.items():
                if tools:
                    output += f"{category}\n"
                    for tool in tools:
                        action = tool.get('action', tool.get('name', 'Unknown'))
                        tool_desc = tool.get('description', '')
                        output += f"  • {action}"
                        if tool_desc:
                            output += f" – {tool_desc}"
                        output += "\n"
                    output += "\n"
        else:
            output += "No tools available.\n\n"
        
        # Non-Read-Only Tools (Write Operations)
        if self.tools:
            non_readonly = self._categorize_non_readonly_tools()
            if non_readonly:
                output += "\n" + "="*80 + "\n"
                output += "  NON-READ-ONLY TOOLS (WRITE OPERATIONS)\n"
                output += "="*80 + "\n\n"
                for category, tools in non_readonly.items():
                    if tools:
                        output += f"{category}\n"
                        for tool in tools:
                            tool_name = tool.get('name', 'Unknown')
                            operation = tool.get('operation', tool_name)
                            tool_desc = tool.get('description', '')
                            output += f"  • {tool_name}: {tool_desc if tool_desc else operation}\n"
                        output += "\n"
        
        # Resources
        if self.resources:
            output += "Resources\n"
            for resource in self.resources:
                resource_name = resource.get('name', 'Unknown')
                resource_desc = resource.get('description', '')
                output += f"  • {resource_name}"
                if resource_desc:
                    output += f" – {resource_desc}"
                output += "\n"
            output += "\n"
        else:
            output += "No resources available.\n\n"
        
        # Prompts
        if self.prompts:
            output += "Prompts\n"
            for prompt in self.prompts:
                prompt_name = prompt.get('name', 'Unknown')
                prompt_desc = prompt.get('description', '')
                output += f"  • {prompt_name}"
                if prompt_desc:
                    output += f" – {prompt_desc}"
                output += "\n"
            output += "\n"
        else:
            output += "No prompts available.\n\n"
        
        return output
    
    def _generate_table_rows(self) -> str:
        """Helper to generate HTML table rows."""
        rows = ""
        
        def add_row(category, attribute, status):
            status_class = "yes" if status else "no"
            status_text = "Yes" if status else "No"
            row_class = ' class="highlight"' if status else ''
            return f'        <tr{row_class}><td>{category}</td><td>{attribute}</td><td class="{status_class}">{status_text}</td></tr>\n'
        
        # Distribution Type
        dist = self.attributes.get('distribution_type') or {}
        rows += add_row("Distribution Type", "Official", dist.get('official'))
        rows += add_row("Distribution Type", "Community", dist.get('community'))
        
        # Protocol Version — iterate the same SUPPORTED_PROTOCOL_VERSIONS the CSV
        # generators use (via protocol_version_field) so the HTML report never
        # silently omits a newer revision the model already carries.
        from src.models.structured_output import (
            SUPPORTED_PROTOCOL_VERSIONS, protocol_version_field)
        protocol = self.attributes.get('protocol_version') or {}
        for version in reversed(SUPPORTED_PROTOCOL_VERSIONS):
            rows += add_row("MCP Protocol Version", version,
                            protocol.get(protocol_version_field(version)))
        
        # Pricing
        pricing = self.attributes.get('pricing') or {}
        rows += add_row("Pricing", "Free", pricing.get('free'))
        rows += add_row("Pricing", "Paid", pricing.get('paid'))
        
        # Hosting Provider
        hosting = self.attributes.get('hosting_provider') or {}
        rows += add_row("Hosting Provider", "SaaS Vendor", hosting.get('saas_vendor'))
        rows += add_row("Hosting Provider", "3rd Party SaaS vendor", hosting.get('third_party_saas'))
        rows += add_row("Hosting Provider", "GitHub", hosting.get('github'))
        rows += add_row("Hosting Provider", "GitLab", hosting.get('gitlab'))
        rows += add_row("Hosting Provider", "Bitbucket", hosting.get('bitbucket'))
        rows += add_row("Hosting Provider", "SourceHut/Gitea/Gogs", hosting.get('sourcehut_gitea_gogs'))
        
        # Authentication
        auth = self.attributes.get('authentication') or {}
        rows += add_row("Authentication", "OAuth 2.1 - Authorization Code Flow", auth.get('oauth2_1_authorization_code'))
        rows += add_row("Authentication", "OAuth 2.1 - Client Credentials Flow", auth.get('oauth2_1_client_credentials'))
        rows += add_row("Authentication", "Bearer Token", auth.get('bearer_token'))
        rows += add_row("Authentication", "Personal Access Token", auth.get('personal_access_token'))
        rows += add_row("Authentication", "API Token", auth.get('api_token'))
        
        # Data Protection
        tls = self.attributes.get('data_protection') or {}
        rows += add_row("Data Protection", "TLS 1.3", tls.get('tls_1_3'))
        rows += add_row("Data Protection", "TLS 1.2", tls.get('tls_1_2'))
        rows += add_row("Data Protection", "Lower versions or no encryption", tls.get('lower_or_no_encryption'))
        
        # Transport Protocol
        transport = self.attributes.get('transport_protocol') or {}
        rows += add_row("Transport Protocol", "STDIO", transport.get('stdio'))
        rows += add_row("Transport Protocol", "HTTP/SSE", transport.get('http_sse'))
        rows += add_row("Transport Protocol", "StreamableHttp", transport.get('streamable_http'))
        rows += add_row("Transport Protocol", "FastAPI", transport.get('fast_api'))
        
        # Tools Operations
        ops = self.attributes.get('tools_operation_type') or {}
        rows += add_row("Tools Operations", "Read-only operations", ops.get('read_only'))
        rows += add_row("Tools Operations", "Read-only and/or update operations", ops.get('read_update'))
        rows += add_row("Tools Operations", "Read update and/or delete operations", ops.get('read_update_delete'))
        
        # Deployment
        deploy = self.attributes.get('deployment_approach') or {}
        rows += add_row("Deployment Approach", "Local", deploy.get('local'))
        rows += add_row("Deployment Approach", "Container", deploy.get('container'))
        rows += add_row("Deployment Approach", "Remote", deploy.get('remote'))
        
        # Capabilities
        capabilities = self.report.get('capabilities', {})
        rows += add_row("Capabilities", "Tools", capabilities.get('tools'))
        rows += add_row("Capabilities", "Resources", capabilities.get('resources'))
        rows += add_row("Capabilities", "Prompts", capabilities.get('prompts'))
        rows += add_row("Capabilities", "Sampling", capabilities.get('sampling'))
        
        return rows
    
    def generate_attributes_csv(self) -> str:
        """
        Full attribute checklist in CSV form — one row per (category, option)
        with a Yes/No status, covering every attribute category the checklist
        report has: Distribution Type, MCP Protocol Version (every spec
        revision plus negotiated/detected/latest-supported summary), Pricing,
        Hosting Provider, Authentication, Data Protection, Transport Protocol,
        Tools Operations, Deployment Approach and the Capabilities flags
        (Tools / Resources / Prompts / Sampling).

        Only the per-item capability detail blobs (Capabilities – Tools /
        Resources / Prompts detailed_info and the Non-Read-Only Tools
        detailed_info) are excluded — those stay in the checklist CSV; the
        capabilities CSV carries the per-tool detail.

        Distinct from generate_csv() (checklist CSV): same attribute rows plus
        the protocol summary rows, minus the detailed_info blobs.
        """
        import csv as _csv
        import io as _io

        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(["Category", "Attribute", "Status"])

        def yn(flag) -> str:
            return "Yes" if flag else "No"

        def row(category: str, attribute: str, flag) -> None:
            writer.writerow([category, attribute, yn(flag)])

        # Server Info — above Distribution Type. Input-sourced identity fields
        # (name, endpoint, GitHub repo) plus the resolved traffic name.
        sinfo = self.attributes.get('server_info') or {}
        server_input = (self.report.get('metadata') or {}).get('server_input') or {}
        writer.writerow(["Server Info", "Name", self.server_name])
        writer.writerow(["Server Info", "Endpoint URL", server_input.get('endpoint_url') or "NA"])
        writer.writerow(["Server Info", "GitHub Repository", server_input.get('repository') or "NA"])
        writer.writerow(["Server Info", "Traffic name", sinfo.get('traffic_name') or "NA"])

        # Distribution Type
        dist = self.attributes.get('distribution_type') or {}
        row("Distribution Type", "Official", dist.get('official'))
        row("Distribution Type", "Community", dist.get('community'))

        # MCP Protocol Version — every spec revision, then the summary values
        protocol = self.attributes.get('protocol_version') or {}
        from src.models.structured_output import (
            SUPPORTED_PROTOCOL_VERSIONS, protocol_version_field)
        for version in SUPPORTED_PROTOCOL_VERSIONS:
            row("MCP Protocol Version", version,
                protocol.get(protocol_version_field(version)))
        detected = protocol.get('detected_version') or ""
        negotiated = protocol.get('negotiated_version') or ""
        writer.writerow(["MCP Protocol Version", "Negotiated / Detected",
                         negotiated or detected or "Unknown"])
        writer.writerow(["MCP Protocol Version", "Latest Supported",
                         protocol.get('latest_supported') or "Unknown"])

        # Pricing
        pricing = self.attributes.get('pricing') or {}
        row("Pricing", "Free", pricing.get('free'))
        row("Pricing", "Paid", pricing.get('paid'))

        # Hosting Provider
        hosting = self.attributes.get('hosting_provider') or {}
        row("Hosting Provider", "SaaS Vendor", hosting.get('saas_vendor'))
        row("Hosting Provider", "3rd Party SaaS vendor",
            hosting.get('third_party_saas'))
        row("Hosting Provider", "GitHub", hosting.get('github'))
        row("Hosting Provider", "GitLab", hosting.get('gitlab'))
        row("Hosting Provider", "Bitbucket", hosting.get('bitbucket'))
        row("Hosting Provider", "SourceHut/Gitea/Gogs",
            hosting.get('sourcehut_gitea_gogs'))

        # Authentication
        auth = self.attributes.get('authentication') or {}
        row("Authentication", "OAuth 2.1 - Authorization Code Flow",
            auth.get('oauth2_1_authorization_code'))
        row("Authentication", "OAuth 2.1 - Client Credentials Flow",
            auth.get('oauth2_1_client_credentials'))
        row("Authentication", "Bearer Token", auth.get('bearer_token'))
        row("Authentication", "Personal Access Token",
            auth.get('personal_access_token'))
        row("Authentication", "API Token", auth.get('api_token'))

        # Data Protection
        tls = self.attributes.get('data_protection') or {}
        row("Data Protection", "TLS 1.3", tls.get('tls_1_3'))
        row("Data Protection", "TLS 1.2", tls.get('tls_1_2'))
        row("Data Protection", "Lower versions or no encryption",
            tls.get('lower_or_no_encryption'))

        # Transport Protocol
        transport = self.attributes.get('transport_protocol') or {}
        row("Transport Protocol", "STDIO", transport.get('stdio'))
        row("Transport Protocol", "HTTP/SSE", transport.get('http_sse'))
        row("Transport Protocol", "StreamableHttp",
            transport.get('streamable_http'))
        row("Transport Protocol", "FastAPI", transport.get('fast_api'))

        # Tools Operations
        ops = self.attributes.get('tools_operation_type') or {}
        row("Tools Operations", "Read-only operations", ops.get('read_only'))
        row("Tools Operations", "Read-only and/or update operations",
            ops.get('read_update'))
        row("Tools Operations", "Read-only update and/or delete operations",
            ops.get('read_update_delete'))

        # Deployment Approach
        deploy = self.attributes.get('deployment_approach') or {}
        row("Deployment Approach", "Local", deploy.get('local'))
        row("Deployment Approach", "Container", deploy.get('container'))
        row("Deployment Approach", "Remote", deploy.get('remote'))

        # Capabilities flags (declared in the handshake) — detail blobs excluded
        capabilities = self.report.get('capabilities', {}) or {}
        row("Capabilities", "Tools", capabilities.get('tools'))
        row("Capabilities", "Resources", capabilities.get('resources'))
        row("Capabilities", "Prompts", capabilities.get('prompts'))
        row("Capabilities", "Sampling", capabilities.get('sampling'))

        return buf.getvalue()

    def save_all_formats(self, output_dir: Path):
        """
        Save reports in all formats to the specified directory.
        
        Args:
            output_dir: Directory to save reports
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Save Markdown
        md_path = output_dir / "attribute_checklist.md"
        with open(md_path, 'w') as f:
            f.write(self.generate_markdown_checklist())
        print(f"✅ Saved Markdown checklist: {md_path}")
        
        # Save HTML
        html_path = output_dir / "attribute_report.html"
        with open(html_path, 'w') as f:
            f.write(self.generate_html_table())
        print(f"✅ Saved HTML report: {html_path}")
        
        # Save CSV
        csv_path = output_dir / "attribute_checklist.csv"
        with open(csv_path, 'w') as f:
            f.write(self.generate_csv())
        print(f"✅ Saved CSV report: {csv_path}")

        # Save the attribute key-value CSV (one row per attribute)
        attrs_path = output_dir / "attributes.csv"
        with open(attrs_path, 'w') as f:
            f.write(self.generate_attributes_csv())
        print(f"✅ Saved attributes CSV: {attrs_path}")
        
        # Print console table
        print(self.generate_console_table())


def generate_aggregated_html(reports: List[Dict[str, Any]], server_names: List[str]) -> str:
    """
    Generate an aggregated HTML report from multiple server inspection reports.
    
    Args:
        reports: List of inspection report dictionaries
        server_names: List of server names corresponding to reports
    
    Returns:
        HTML string with aggregated table
    """
    # Escape curly braces in CSS by doubling them
    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>MCP Server Attributes - Multi-Server Report</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1 {{ color: #333; }}
        .info {{ margin-bottom: 20px; color: #666; }}
        table {{ border-collapse: collapse; width: 100%; margin-bottom: 30px; }}
        th, td {{ border: 1px solid #ddd; padding: 12px; text-align: left; }}
        th {{ background-color: #4CAF50; color: white; }}
        tr:nth-child(even) {{ background-color: #f2f2f2; }}
        .yes {{ color: green; font-weight: bold; }}
        .no {{ color: #ccc; }}
        .highlight {{ background-color: #90EE90; }}
        .server-name {{ font-weight: bold; background-color: #e8f5e9; }}
    </style>
</head>
<body>
    <h1>MCP Server Attribute Report - Multi-Server</h1>
    <div class="info">
        <p><strong>Total Servers:</strong> {len(server_names)}</p>
        <p><strong>Servers:</strong> {", ".join(server_names)}</p>
    </div>
    <table>
        <tr>
            <th>Server Name</th>
            <th>Category</th>
            <th>Attribute</th>
            <th>Status</th>
        </tr>
"""
    
    def add_row(server_name, category, attribute, status):
        status_class = "yes" if status else "no"
        status_text = "Yes" if status else "No"
        row_class = ' class="highlight"' if status else ''
        return f'        <tr{row_class}><td class="server-name">{server_name}</td><td>{category}</td><td>{attribute}</td><td class="{status_class}">{status_text}</td></tr>\n'
    
    # Generate rows for each server
    for report, server_name in zip(reports, server_names):
        generator = AttributeReportGenerator(report)
        attributes = generator.attributes
        
        # Distribution Type
        dist = attributes.get('distribution_type', {})
        html += add_row(server_name, "Distribution Type", "Official", dist.get('official'))
        html += add_row(server_name, "Distribution Type", "Community", dist.get('community'))
        
        # Protocol Version
        protocol = attributes.get('protocol_version', {})
        from src.models.structured_output import SUPPORTED_PROTOCOL_VERSIONS, protocol_version_field
        for version in reversed(SUPPORTED_PROTOCOL_VERSIONS):
            html += add_row(server_name, "MCP Protocol Version", version, protocol.get(protocol_version_field(version)))
        
        # Pricing
        pricing = attributes.get('pricing', {})
        html += add_row(server_name, "Pricing", "Free", pricing.get('free'))
        html += add_row(server_name, "Pricing", "Paid", pricing.get('paid'))
        
        # Hosting Provider
        hosting = attributes.get('hosting_provider', {})
        html += add_row(server_name, "Hosting Provider", "SaaS Vendor", hosting.get('saas_vendor'))
        html += add_row(server_name, "Hosting Provider", "3rd Party SaaS vendor", hosting.get('third_party_saas'))
        html += add_row(server_name, "Hosting Provider", "GitHub", hosting.get('github'))
        html += add_row(server_name, "Hosting Provider", "GitLab", hosting.get('gitlab'))
        html += add_row(server_name, "Hosting Provider", "Bitbucket", hosting.get('bitbucket'))
        html += add_row(server_name, "Hosting Provider", "SourceHut/Gitea/Gogs", hosting.get('sourcehut_gitea_gogs'))
        
        # Authentication
        auth = attributes.get('authentication', {})
        html += add_row(server_name, "Authentication", "OAuth 2.1 - Authorization Code Flow", auth.get('oauth2_1_authorization_code'))
        html += add_row(server_name, "Authentication", "OAuth 2.1 - Client Credentials Flow", auth.get('oauth2_1_client_credentials'))
        html += add_row(server_name, "Authentication", "Bearer Token", auth.get('bearer_token'))
        html += add_row(server_name, "Authentication", "Personal Access Token", auth.get('personal_access_token'))
        html += add_row(server_name, "Authentication", "API Token", auth.get('api_token'))
        
        # Data Protection
        tls = attributes.get('data_protection', {})
        html += add_row(server_name, "Data Protection (TLS)", "TLS 1.3", tls.get('tls_1_3'))
        html += add_row(server_name, "Data Protection (TLS)", "TLS 1.2", tls.get('tls_1_2'))
        html += add_row(server_name, "Data Protection (TLS)", "Lower versions or no encryption", tls.get('lower_or_no_encryption'))
        
        # Transport Protocol
        transport = attributes.get('transport_protocol', {})
        html += add_row(server_name, "Transport Protocol", "STDIO", transport.get('stdio'))
        html += add_row(server_name, "Transport Protocol", "HTTP/SSE", transport.get('http_sse'))
        html += add_row(server_name, "Transport Protocol", "StreamableHttp", transport.get('streamable_http'))
        html += add_row(server_name, "Transport Protocol", "FastAPI", transport.get('fast_api'))
        
        # Tools Operations
        ops = attributes.get('tools_operation_type', {})
        html += add_row(server_name, "Tools Operations Type", "Read-only operations", ops.get('read_only'))
        html += add_row(server_name, "Tools Operations Type", "Read-only and/or update operations", ops.get('read_update'))
        html += add_row(server_name, "Tools Operations Type", "Read update and/or delete operations", ops.get('read_update_delete'))
        
        # Deployment
        deploy = attributes.get('deployment_approach', {})
        html += add_row(server_name, "Deployment Approach", "Local", deploy.get('local'))
        html += add_row(server_name, "Deployment Approach", "Container", deploy.get('container'))
        html += add_row(server_name, "Deployment Approach", "Remote", deploy.get('remote'))
        
        # Capabilities
        capabilities = report.get('capabilities', {})
        html += add_row(server_name, "Capabilities", "Tools", capabilities.get('tools'))
        html += add_row(server_name, "Capabilities", "Resources", capabilities.get('resources'))
        html += add_row(server_name, "Capabilities", "Prompts", capabilities.get('prompts'))
        html += add_row(server_name, "Capabilities", "Sampling", capabilities.get('sampling'))
    
    html += """    </table>
</body>
</html>
"""
    return html


# Standalone function for easy use
def generate_attribute_reports(
    inspection_report_path: str,
    output_dir: str = None
):
    """
    Generate attribute reports from an inspection report JSON file.
    
    Args:
        inspection_report_path: Path to inspection_report.json
        output_dir: Directory to save reports (defaults to same dir as input)
    """
    with open(inspection_report_path, 'r') as f:
        report = json.load(f)
    
    if output_dir is None:
        output_dir = Path(inspection_report_path).parent
    
    generator = AttributeReportGenerator(report)
    generator.save_all_formats(output_dir)


if __name__ == "__main__":
    """Command-line usage."""
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python attribute_report_generator.py <inspection_report.json> [output_dir]")
        sys.exit(1)
    
    report_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None
    
    generate_attribute_reports(report_path, output_dir)

