"""BioWatch Notion MCP server (stdio).

Exposes tools to Claude Code so every team member can read tasks from Notion
and write concise feature documentation as MRs land:
  - notion_get_subpages: recursive index of the task tree (titles + ids)
  - notion_get_page_content: full body of a task page (Markdown-like)
  - notion_create_doc: create/update a feature doc page in the
    "Documentation Technique" database, following the team template

Requires two tokens in the local .env file (gitignored):
  - NOTION_TOKEN: read access, granted on the BioWatch root page
  - NOTION_TOKEN_DOC: write access, granted on the Documentation Technique DB
"""

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx
import mcp.server.stdio
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
BIOWATCH_ROOT_PAGE_ID = "31150bea-018d-800e-843b-f3403306af0e"
DOC_DATABASE_ID = "2ed50bea-018d-80dc-82e6-fdaf63413233"
DOC_TOKEN_ENV = "NOTION_TOKEN_DOC"


def _headers(token_env: str = "NOTION_TOKEN") -> dict[str, str]:
    token = os.environ.get(token_env)
    if not token:
        raise RuntimeError(
            f"{token_env} missing. Add `{token_env}=secret_...` to the project's "
            ".env (create a Notion integration at https://www.notion.so/my-integrations, "
            "then share the relevant BioWatch page/database with it)."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
    }


def _normalize_id(raw: str) -> str:
    s = raw.replace("-", "").strip()
    if len(s) != 32:
        return raw
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"


def _extract_page_title(page: dict[str, Any]) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            parts = prop.get("title", [])
            text = "".join(p.get("plain_text", "") for p in parts)
            return text or "(untitled)"
    return "(untitled)"


def _rich_text(parts: list[dict[str, Any]]) -> str:
    return "".join(p.get("plain_text", "") for p in parts or [])


async def _paginate(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    token_env: str = "NOTION_TOKEN",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        if method == "GET":
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            r = await client.get(url, params=params, headers=_headers(token_env))
        else:
            payload: dict[str, Any] = {"page_size": 100, **(body or {})}
            if cursor:
                payload["start_cursor"] = cursor
            r = await client.post(url, json=payload, headers=_headers(token_env))
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            return results
        cursor = data.get("next_cursor")


async def _walk_page(
    client: httpx.AsyncClient, page_id: str, title: str, depth: int, max_depth: int
) -> dict[str, Any]:
    node: dict[str, Any] = {"id": page_id, "title": title, "kind": "page", "children": []}
    if depth >= max_depth:
        return node
    blocks = await _paginate(client, "GET", f"{NOTION_API}/blocks/{page_id}/children")
    for b in blocks:
        btype = b.get("type")
        if btype == "child_page":
            node["children"].append(
                await _walk_page(
                    client,
                    b["id"],
                    b.get("child_page", {}).get("title", "(untitled)"),
                    depth + 1,
                    max_depth,
                )
            )
        elif btype == "child_database":
            node["children"].append(
                await _walk_database(
                    client,
                    b["id"],
                    b.get("child_database", {}).get("title", "(untitled)"),
                    depth + 1,
                    max_depth,
                )
            )
    return node


async def _walk_database(
    client: httpx.AsyncClient, db_id: str, title: str, depth: int, max_depth: int
) -> dict[str, Any]:
    node: dict[str, Any] = {"id": db_id, "title": title, "kind": "database", "children": []}
    if depth >= max_depth:
        return node
    pages = await _paginate(client, "POST", f"{NOTION_API}/databases/{db_id}/query")
    for page in pages:
        node["children"].append(
            await _walk_page(
                client,
                page["id"],
                _extract_page_title(page),
                depth + 1,
                max_depth,
            )
        )
    return node


async def _resolve_root(client: httpx.AsyncClient, node_id: str, max_depth: int) -> dict[str, Any]:
    db_r = await client.get(f"{NOTION_API}/databases/{node_id}", headers=_headers())
    if db_r.status_code == 200:
        data = db_r.json()
        title = (
            "".join(p.get("plain_text", "") for p in data.get("title", [])) or "(untitled database)"
        )
        return await _walk_database(client, node_id, title, 0, max_depth)
    if db_r.status_code not in (400, 404):
        db_r.raise_for_status()
    pg_r = await client.get(f"{NOTION_API}/pages/{node_id}", headers=_headers())
    pg_r.raise_for_status()
    return await _walk_page(client, node_id, _extract_page_title(pg_r.json()), 0, max_depth)


def _format_tree(node: dict[str, Any], indent: int = 0) -> str:
    prefix = "  " * indent + ("- " if indent > 0 else "")
    out = f"{prefix}[{node['kind']}] {node['title']} ({node['id']})\n"
    for c in node.get("children", []):
        out += _format_tree(c, indent + 1)
    return out


async def _render_blocks(
    client: httpx.AsyncClient, block_id: str, indent: int, max_depth: int
) -> str:
    if indent >= max_depth:
        return ""
    blocks = await _paginate(client, "GET", f"{NOTION_API}/blocks/{block_id}/children")
    pad = "  " * indent
    lines: list[str] = []
    for b in blocks:
        btype: str = b.get("type") or ""
        data: dict[str, Any] = b.get(btype) or {}
        text = _rich_text(data.get("rich_text", []))

        if btype == "paragraph":
            lines.append(f"{pad}{text}" if text else "")
        elif btype == "heading_1":
            lines.append(f"{pad}# {text}")
        elif btype == "heading_2":
            lines.append(f"{pad}## {text}")
        elif btype == "heading_3":
            lines.append(f"{pad}### {text}")
        elif btype == "bulleted_list_item":
            lines.append(f"{pad}- {text}")
        elif btype == "numbered_list_item":
            lines.append(f"{pad}1. {text}")
        elif btype == "to_do":
            mark = "x" if data.get("checked") else " "
            lines.append(f"{pad}- [{mark}] {text}")
        elif btype == "toggle":
            lines.append(f"{pad}▸ {text}")
        elif btype == "quote":
            lines.append(f"{pad}> {text}")
        elif btype == "callout":
            icon_block: dict[str, Any] = data.get("icon") or {}
            icon = icon_block.get("emoji", "ℹ️")
            lines.append(f"{pad}{icon} {text}")
        elif btype == "code":
            lang = data.get("language", "")
            lines.append(f"{pad}```{lang}\n{text}\n{pad}```")
        elif btype == "divider":
            lines.append(f"{pad}---")
        elif btype == "child_page":
            lines.append(f"{pad}↳ [page] {data.get('title', '(untitled)')} ({b['id']})")
        elif btype == "child_database":
            lines.append(f"{pad}↳ [db] {data.get('title', '(untitled)')} ({b['id']})")
        elif btype in ("table_of_contents", "breadcrumb"):
            continue
        elif text:
            lines.append(f"{pad}{text}")

        if b.get("has_children") and btype not in ("child_page", "child_database"):
            nested = await _render_blocks(client, b["id"], indent + 1, max_depth)
            if nested:
                lines.append(nested)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Doc writing (Documentation Technique database)
# ---------------------------------------------------------------------------


def _rt(content: str) -> list[dict[str, Any]]:
    """Notion rich_text array, split into <=2000 char chunks (API limit)."""
    chunks = [content[i : i + 2000] for i in range(0, len(content), 2000)] or [""]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def _heading(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_1", "heading_1": {"rich_text": _rt(text)}}


def _paragraph(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rt(text)}}


def _code(text: str, language: str = "python") -> dict[str, Any]:
    return {
        "object": "block",
        "type": "code",
        "code": {"rich_text": _rt(text), "language": language},
    }


def _callout(text: str, emoji: str = "💡") -> dict[str, Any]:
    return {
        "object": "block",
        "type": "callout",
        "callout": {"rich_text": _rt(text), "icon": {"type": "emoji", "emoji": emoji}},
    }


def _build_doc_blocks(
    resume: str, structure: str, workflow: str, requirements: str | None
) -> list[dict[str, Any]]:
    """Blocks matching the BioWatch feature-doc template."""
    blocks = [
        _heading("Résumé (2 phrases)"),
        _paragraph(resume),
        _heading("Structure"),
        _code(structure, "python"),
        _heading("Exemple / workflow"),
        _callout(workflow),
    ]
    if requirements and requirements.strip():
        blocks.append(_heading("Requirements (optionnel)"))
        blocks.append(_callout(requirements))
    return blocks


async def _find_doc_page(client: httpx.AsyncClient, title: str) -> str | None:
    """Return the page id of a doc whose title exactly matches, else None."""
    r = await client.post(
        f"{NOTION_API}/databases/{DOC_DATABASE_ID}/query",
        json={"filter": {"property": "Name", "title": {"equals": title}}, "page_size": 1},
        headers=_headers(DOC_TOKEN_ENV),
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0]["id"] if results else None


async def _create_or_update_doc(
    client: httpx.AsyncClient,
    title: str,
    blocks: list[dict[str, Any]],
) -> tuple[str, str]:
    """Create the doc page, or replace the body of an existing same-title page.

    Returns (action, page_id) with action in {"created", "updated"}.
    """
    page_id = await _find_doc_page(client, title)
    if page_id:
        existing = await _paginate(
            client, "GET", f"{NOTION_API}/blocks/{page_id}/children", token_env=DOC_TOKEN_ENV
        )
        for block in existing:
            dr = await client.delete(
                f"{NOTION_API}/blocks/{block['id']}", headers=_headers(DOC_TOKEN_ENV)
            )
            dr.raise_for_status()
        ar = await client.patch(
            f"{NOTION_API}/blocks/{page_id}/children",
            json={"children": blocks},
            headers=_headers(DOC_TOKEN_ENV),
        )
        ar.raise_for_status()
        return "updated", page_id

    pr = await client.post(
        f"{NOTION_API}/pages",
        json={
            "parent": {"database_id": DOC_DATABASE_ID},
            "properties": {"Name": {"title": [{"text": {"content": title}}]}},
            "children": blocks,
        },
        headers=_headers(DOC_TOKEN_ENV),
    )
    pr.raise_for_status()
    return "created", pr.json()["id"]


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

server: Server = Server("biowatch-notion")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="notion_get_subpages",
            description=(
                "BioWatch task index (Notion). Returns the recursive tree of task pages "
                "[kind] title (id) under the project root. "
                "Use this to LOCATE a task ID — then call `notion_get_page_content` with that ID "
                "to read the task's implementation details, Definition of Ready (DoR) and Definition of Done (DoD). "
                "Defaults page_id to the BioWatch task root."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "Optional Notion page or database ID (with or without dashes). Defaults to the BioWatch task root.",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Recursion depth (default 5, max 10).",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
            },
        ),
        types.Tool(
            name="notion_get_page_content",
            description=(
                "Read the full body of a BioWatch task page from Notion as Markdown-like text. "
                "Use this AFTER `notion_get_subpages` to fetch the implementation details, "
                "Definition of Ready (DoR) and Definition of Done (DoD) of a specific task before starting work."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "Notion page ID of the task (with or without dashes).",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Nested-block depth (default 4, max 8).",
                        "default": 4,
                        "minimum": 1,
                        "maximum": 8,
                    },
                },
                "required": ["page_id"],
            },
        ),
        types.Tool(
            name="notion_create_doc",
            description=(
                "Write CONCISE BioWatch feature documentation to the 'Documentation Technique' "
                "Notion database. Call this when documenting a feature as part of an MR. "
                "The page follows the team template EXACTLY (do not add or reorder sections): "
                "Résumé (2 phrases) → Structure (code tree) → Exemple / workflow → Requirements (optional). "
                "GOAL: a teammate must grasp the feature in under 2 minutes — no long essays, "
                "keep each section tight. If a doc with the same `title` already exists, its body is "
                "REPLACED (idempotent per feature); otherwise a new page is created. "
                "Choose a clear, self-explanatory `title` (the feature name) so docs are easy to find in the DB."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Feature name — the page title. Clear and recognizable (e.g. 'Grille H3 par AOI'). Reused as the update key.",
                    },
                    "resume": {
                        "type": "string",
                        "description": "Summary in AT MOST 2 sentences: what the feature does and why.",
                    },
                    "structure": {
                        "type": "string",
                        "description": (
                            "File/folder tree of the feature, rendered in a python code block. "
                            "Annotate key files with short inline comments, e.g.:\n"
                            "folder/\n-- file1\n-- file2  # entities\n-- file3  # call api"
                        ),
                    },
                    "workflow": {
                        "type": "string",
                        "description": (
                            "A short concrete example or end-to-end workflow of the code. "
                            "E.g. 'Chaque lundi une cronjob appelle sentinel2Repository qui récupère les données de la semaine…'."
                        ),
                    },
                    "requirements": {
                        "type": "string",
                        "description": "Optional. Business rules / technical constraints (e.g. 'ne stocker que si couverture nuageuse < 20%').",
                    },
                },
                "required": ["title", "resume", "structure", "workflow"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    arguments = arguments or {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        if name == "notion_get_subpages":
            page_id = _normalize_id(arguments.get("page_id") or BIOWATCH_ROOT_PAGE_ID)
            max_depth = max(1, min(int(arguments.get("max_depth") or 5), 10))
            tree = await _resolve_root(client, page_id, max_depth)
            return [types.TextContent(type="text", text=_format_tree(tree))]

        if name == "notion_get_page_content":
            raw = arguments.get("page_id")
            if not raw:
                raise ValueError("page_id is required")
            page_id = _normalize_id(raw)
            max_depth = max(1, min(int(arguments.get("max_depth") or 4), 8))
            pg_r = await client.get(f"{NOTION_API}/pages/{page_id}", headers=_headers())
            pg_r.raise_for_status()
            title = _extract_page_title(pg_r.json())
            body = await _render_blocks(client, page_id, 0, max_depth)
            text = f"# {title}\n\n{body}".rstrip() + "\n"
            return [types.TextContent(type="text", text=text)]

        if name == "notion_create_doc":
            title = (arguments.get("title") or "").strip()
            resume = arguments.get("resume") or ""
            structure = arguments.get("structure") or ""
            workflow = arguments.get("workflow") or ""
            requirements = arguments.get("requirements")
            if not title or not resume or not structure or not workflow:
                raise ValueError(
                    "title, resume, structure and workflow are required for notion_create_doc"
                )
            blocks = _build_doc_blocks(resume, structure, workflow, requirements)
            action, page_id = await _create_or_update_doc(client, title, blocks)
            url = f"https://notion.so/{page_id.replace('-', '')}"
            return [types.TextContent(type="text", text=f"Doc {action}: {title}\n{url}")]

        raise ValueError(f"Unknown tool: {name}")


async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="biowatch-notion",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
