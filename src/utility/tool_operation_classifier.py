"""
Tool operation (Read/Write/Delete) classification.

Two tiers, per the project decision:

  Tier 1 — annotations (authoritative): the tool's declared
    `annotations.readOnlyHint` / `annotations.destructiveHint` decide. These
    come straight from the server's tools/list response. Per the MCP spec
    (schema ToolAnnotations), `destructiveHint` and `idempotentHint` are
    meaningful only when `readOnlyHint == false`.

  Tier 2 — name+description heuristic (fallback): used only for the actions
    the annotations did not resolve. Every value produced by this tier is
    marked ``heuristic`` so a guessed "Delete" is never visually identical to
    an annotation-confirmed one. The fallback NEVER overrides a declared
    annotation.

Spec reference: https://modelcontextprotocol.io/specification/2026-07-28/server/tools
ToolAnnotations defaults (schema.ts): readOnlyHint=false, destructiveHint=true,
idempotentHint=false, openWorldHint=true.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Keyword sets for the Tier-2 fallback. Generic verbs only — vendor-specific
# tool names do not belong here (the old exporter hardcoded e.g. LimaCharlie
# tools; that data belongs to one server, not to a classifier).
_READ_KEYWORDS = (
    "get", "list", "read", "fetch", "retrieve", "view", "show",
    "find", "search", "query", "describe", "check", "status", "info",
)
_WRITE_KEYWORDS = (
    "update", "modify", "edit", "change", "set", "write", "patch",
    "create", "add", "insert", "new", "post", "upload", "put", "register",
    "enable", "disable", "start", "stop", "restart", "assign", "tag",
)
_DELETE_KEYWORDS = (
    "delete", "remove", "destroy", "drop", "clear", "purge", "revoke",
    "uninstall", "archive",
)

ANNOTATION_SOURCE = "annotation"
HEURISTIC_SOURCE = "heuristic"


def _annotations_of(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Return the tool's annotations as a plain dict (never None)."""
    annotations = tool.get("annotations")
    if annotations is None:
        return {}
    if hasattr(annotations, "model_dump"):
        return annotations.model_dump(exclude_none=True)
    if isinstance(annotations, dict):
        return annotations
    return {}


def _tool_text(tool: Dict[str, Any]) -> str:
    name = (tool.get("name") or "")
    desc = (tool.get("description") or "")
    if not isinstance(name, str):
        name = ""
    if not isinstance(desc, str):
        desc = ""
    return f"{name} {desc}".lower()


def _keyword_hits(text: str, keywords: Tuple[str, ...]) -> bool:
    # Substring match on word boundaries would miss `get_user`; plain substring
    # matches the historical behavior (e.g. "target" contains "get" is the
    # known false-positive trade-off of the fallback tier — which is exactly
    # why its output is labeled heuristic).
    return any(kw in text for kw in keywords)


def classify_tool_actions(tool: Dict[str, Any]) -> Dict[str, Any]:
    """
    Classify one tool's Read/Write/Delete support.

    Returns:
        {
          "supports_read":   "Yes" | "No",
          "supports_write":  "Yes" | "No",
          "supports_delete": "Yes" | "No",
          # "annotation" when every action came from declared hints,
          # "heuristic" when any action fell back to name/description keywords,
          # "none" for tools with no usable signal at all (empty name+desc).
          "source": "annotation" | "heuristic" | "none",
        }

    Tier rules:
      - readOnlyHint is True  -> Read=Yes, Write=No, Delete=No (annotations
        settle everything; read-only tools do not modify their environment,
        and destructive/idempotent hints are not meaningful then).
      - readOnlyHint is False -> Read=No; Delete from destructiveHint
        (True->Yes, False->No, absent->keyword fallback); Write = not Delete
        (spec: destructiveHint=false means "only additive updates", i.e.
        write-but-not-delete).
      - readOnlyHint absent   -> every unresolved action falls to the
        name+description keyword tier.
    """
    annotations = _annotations_of(tool)
    # A declared-but-non-boolean hint (e.g. the string "false") must not
    # silently skip Tier 1 — only an actual boolean settles anything.
    read_only_hint = annotations.get("readOnlyHint")
    if not isinstance(read_only_hint, bool):
        read_only_hint = None
    destructive_hint = annotations.get("destructiveHint")
    if not isinstance(destructive_hint, bool):
        destructive_hint = None

    read: Optional[str] = None
    write: Optional[str] = None
    delete: Optional[str] = None

    if read_only_hint is True:
        read, write, delete = "Yes", "No", "No"
    elif read_only_hint is False:
        read = "No"
        if destructive_hint is True:
            write, delete = "Yes", "Yes"
        elif destructive_hint is False:
            write, delete = "Yes", "No"
        # destructiveHint absent -> Delete (and its Write inverse) fall through
        # to the keyword tier below.

    if read is not None and write is not None and delete is not None:
        return {
            "supports_read": read,
            "supports_write": write,
            "supports_delete": delete,
            "source": ANNOTATION_SOURCE,
        }

    # ── Tier 2: keyword fallback for whatever is unresolved ─────────────────
    text = _tool_text(tool)
    if not text.strip():
        # No annotations AND no name/description: nothing to classify on.
        # Default to the conservative "No" so reports stay complete, marked
        # source "none" so reviewers know there was zero signal.
        return {
            "supports_read": read or "No",
            "supports_write": write or "No",
            "supports_delete": delete or "No",
            "source": "none" if read is None else ANNOTATION_SOURCE,
        }

    kw_delete = _keyword_hits(text, _DELETE_KEYWORDS)
    kw_write = kw_delete or _keyword_hits(text, _WRITE_KEYWORDS)
    kw_read = _keyword_hits(text, _READ_KEYWORDS) or not kw_write

    if read is None:
        read = "Yes" if (kw_read and not kw_write) else "No"
    if delete is None:
        delete = "Yes" if kw_delete else "No"
    if write is None:
        write = "Yes" if kw_write else "No"

    # readOnlyHint=False already settled Read=No above; if keywords say the
    # tool also reads (common for "update then return the record"), keep the
    # annotation verdict — Tier 1 always wins.
    return {
        "supports_read": read,
        "supports_write": write,
        "supports_delete": delete,
        "source": HEURISTIC_SOURCE,
    }


def classify_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Classify a list of tools; returns one result dict per tool (same order)."""
    return [classify_tool_actions(t) for t in tools]


def rollup_operation_type(
    tools: List[Dict[str, Any]],
) -> Tuple[bool, bool, bool, Dict[str, int]]:
    """
    Roll per-tool classifications up to the server-level
    (read_only, read_update, read_update_delete) flags, matching the semantics
    of ToolsOperationType. Returns the flags plus annotation/heuristic counts
    so reports can state how much of the verdict was confirmed.
    """
    has_write = False
    has_delete = False
    stats = {"annotation": 0, "heuristic": 0, "none": 0}

    for tool in tools:
        result = classify_tool_actions(tool)
        stats[result["source"] if result["source"] in stats else "none"] += 1
        if result["supports_write"] == "Yes":
            has_write = True
        if result["supports_delete"] == "Yes":
            has_delete = True

    if has_delete:
        return False, False, True, stats
    if has_write:
        return False, True, False, stats
    return True, False, False, stats
