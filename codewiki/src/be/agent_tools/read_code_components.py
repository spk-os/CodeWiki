from pydantic_ai import RunContext, Tool
from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from pathlib import Path


async def read_code_components(ctx: RunContext[CodeWikiDeps], component_ids: list[str]) -> str:
    """Read the code of a given component id

    Args:
        component_ids: The ids of the components to read, e.g. ["sweagent/types.py::AgentRunResult", "auth/middleware.py::verify_token"] where the part before :: is the file path and the part after :: is the component name
    """

    results = []

    for component_id in component_ids:
        if component_id not in ctx.deps.components:
            results.append(f"# Component {component_id} not found")
        else:
            node = ctx.deps.components[component_id]
            source = node.source_code
            if source is None:
                # source_code was stripped from memory after dependency analysis.
                # Read the relevant portion from the actual source file.
                source = _read_source_from_disk(
                    ctx.deps.absolute_repo_path,
                    node.file_path,
                    node.start_line,
                    node.end_line,
                )
            results.append(f"# Component {component_id}:\n{source.strip()}\n\n")

    return "\n".join(results)


def _read_source_from_disk(
    repo_path: str,
    file_path: str,
    start_line: int,
    end_line: int,
) -> str:
    """Read source code from disk when Node.source_code has been freed from memory."""
    try:
        from codewiki.src.be.dependency_analyzer.utils.security import safe_open_text
        repo_base = Path(repo_path)
        abs_file = repo_base / file_path if not Path(file_path).is_absolute() else Path(file_path)
        content = safe_open_text(repo_base, abs_file)
        lines = content.splitlines()
        if start_line > 0 and end_line > 0 and start_line <= len(lines):
            snippet = "\n".join(lines[start_line - 1:end_line])
            return snippet
        return content
    except Exception as e:
        return f"# Error reading from disk: {e}"


read_code_components_tool = Tool(function=read_code_components, name="read_code_components", description="Read the code of a given list of component ids", takes_ctx=True)