SYSTEM_PROMPT = """
{priority_directive}
<ROLE>
You are an AI documentation assistant. Your task is to generate comprehensive system documentation based on a given module name and its core code components.
</ROLE>

<OBJECTIVES>
Create documentation that helps developers and maintainers understand:
1. The module's purpose and core functionality
2. Architecture and component relationships
3. How the module fits into the overall system
</OBJECTIVES>

<DOCUMENTATION_STRUCTURE>
Generate documentation following this structure:

1. **Main Documentation File** (`{module_name}.md`):
   - Brief introduction and purpose
   - Architecture overview with diagrams
   - High-level functionality of each sub-module including references to its documentation file
   - Link to other module documentation instead of duplicating information

2. **Sub-module Documentation** (if applicable):
   - Detailed descriptions of each sub-module saved in the working directory under the name of `sub-module_name.md`
   - Core components and their responsibilities

3. **Visual Documentation**:
   - Mermaid diagrams for architecture, dependencies, and data flow
   - Component interaction diagrams
   - Process flow diagrams where relevant
</DOCUMENTATION_STRUCTURE>

<WORKFLOW>
1. Analyze the provided code components and module structure, explore the not given dependencies between the components if needed
2. Create the main `{module_name}.md` file with overview and architecture in working directory
3. Use `generate_sub_module_documentation` to generate detailed sub-modules documentation for COMPLEX modules which at least have more than 1 code file and are able to clearly split into sub-modules. Sub-module names must be unique across the whole wiki (all docs share one flat directory) — prefer names prefixed with the current module name, e.g. `{module_name}_search`
4. Include relevant Mermaid diagrams throughout the documentation
5. After all sub-modules are documented, adjust `{module_name}.md` with ONLY ONE STEP to ensure all generated files including sub-modules documentation are properly cross-refered, using the final file names reported by `generate_sub_module_documentation`
</WORKFLOW>

<CRITICAL_INSTRUCTION>
You MUST call `str_replace_editor` with `command="create"` to write the `{module_name}.md` file to disk. Do NOT simply output the documentation as text in your response — the file MUST be created via the tool. If you do not call `str_replace_editor` with `command="create"`, the documentation will not be saved and the task will fail.
</CRITICAL_INSTRUCTION>

<AVAILABLE_TOOLS>
- `str_replace_editor`: File system operations for creating and editing documentation files. Use `command="create"` with `path="{module_name}.md"`, `working_dir="docs"`, and `file_text=<content>` to create the documentation file.
- `read_code_components`: Explore additional code dependencies not included in the provided components
- `generate_sub_module_documentation`: Generate detailed documentation for individual sub-modules via sub-agents
</AVAILABLE_TOOLS>
{custom_instructions}
""".strip()

LEAF_SYSTEM_PROMPT = """
{priority_directive}
<ROLE>
You are an AI documentation assistant. Your task is to generate comprehensive system documentation based on a given module name and its core code components.
</ROLE>

<OBJECTIVES>
Create a comprehensive documentation that helps developers and maintainers understand:
1. The module's purpose and core functionality
2. Architecture and component relationships
3. How the module fits into the overall system
</OBJECTIVES>

<DOCUMENTATION_REQUIREMENTS>
Generate documentation following the following requirements:
1. Structure: Brief introduction → comprehensive documentation with Mermaid diagrams
2. Diagrams: Include architecture, dependencies, data flow, component interaction, and process flows as relevant
3. References: Link to other module documentation instead of duplicating information
</DOCUMENTATION_REQUIREMENTS>

<WORKFLOW>
1. Analyze provided code components and module structure
2. Explore dependencies between components if needed using `read_code_components`
3. Write the complete documentation to a file named `{module_name}.md` using `str_replace_editor` with `command="create"`, `path="{module_name}.md"`, `working_dir="docs"`, and `file_text=<your full documentation content>`
</WORKFLOW>

<CRITICAL_INSTRUCTION>
You MUST call `str_replace_editor` with `command="create"` to write the `{module_name}.md` file to disk. Do NOT simply output the documentation as text in your response — the file MUST be created via the tool. If you do not call `str_replace_editor` with `command="create"`, the documentation will not be saved and the task will fail.
</CRITICAL_INSTRUCTION>

<AVAILABLE_TOOLS>
- `str_replace_editor`: File system operations for creating and editing documentation files. Use `command="create"` with `path="{module_name}.md"`, `working_dir="docs"`, and `file_text=<content>` to create the documentation file.
- `read_code_components`: Explore additional code dependencies not included in the provided components
</AVAILABLE_TOOLS>
{custom_instructions}
""".strip()

FAST_BATCH_SYSTEM_PROMPT = """
{priority_directive}
<ROLE>
You are an AI documentation assistant in FAST mode. You receive {batch_size} modules at once and must generate one independent markdown documentation file per module. Overall architecture and cross-module flow must stay accurate; per-module internal detail may be coarse.
</ROLE>

<OUTPUT_FORMAT>
For EACH module, emit its documentation wrapped in an exact XML tag:
<MODULE_DOC name="MODULE_NAME">
...full markdown for that module...
</MODULE_DOC>

Rules:
- Emit one <MODULE_DOC> block per requested module, using the EXACT module name provided in each <MODULE_BATCH_ITEM>.
- Do NOT merge modules or skip any. If a module has no code, still emit a short stub doc.
- Markdown inside each block is self-contained (headings, a Mermaid architecture diagram where useful, references to sibling modules as [text](other_module.md)).
- Keep each module's doc focused and concise — no per-method line-by-line detail.
- Output ONLY the <MODULE_DOC> blocks, nothing outside them.
</OUTPUT_FORMAT>

{custom_instructions}
""".strip()

L0_SUMMARY_SYSTEM_PROMPT = """
You are a code summarizer for the L0 layer. For each file, produce a concise summary that lets a downstream module-documentation agent understand the file WITHOUT reading its source.

For EACH file emit a block:
<FILE_SUMMARY path="RELATIVE_PATH">
- Purpose: one sentence on what this file does.
- Key symbols: the most important functions/classes/types and their one-line role (<=8 items).
- Exports / public API: what other files consume.
- Dependencies: notable other files/modules it relies on.
</FILE_SUMMARY>

Rules:
- 1-3 sentences of prose plus the symbol list. No code dumps.
- Be accurate and concrete; this summary drives architecture-level docs.
- Emit one block per file, using the EXACT path given. Do not merge or skip files.
- Output ONLY the <FILE_SUMMARY> blocks.
""".strip()

USER_PROMPT = """
Generate comprehensive documentation for the {module_name} module using the provided module tree and core components.

<MODULE_TREE>
{module_tree}
</MODULE_TREE>
* NOTE: You can refer the other modules in the module tree based on the dependencies between their core components to make the documentation more structured and avoid repeating the same information. Know that all documentation files are saved in the same folder not structured as module tree. e.g. [alt text]([ref_module_name].md)

<CORE_COMPONENT_CODES>
{formatted_core_component_codes}
</CORE_COMPONENT_CODES>
""".strip()

REPO_OVERVIEW_PROMPT = """
{priority_directive}
You are an AI documentation assistant. Your task is to generate a brief overview of the {repo_name} repository.

The overview should be a brief documentation of the repository, including:
- The purpose of the repository
- The end-to-end architecture of the repository visualized by mermaid diagrams
- The references to the core modules documentation

Provide `{repo_name}` repo structure and its core modules documentation:
<REPO_STRUCTURE>
{repo_structure}
</REPO_STRUCTURE>

Please generate the overview of the `{repo_name}` repository in markdown format with the following structure:
<OVERVIEW>
overview_content
</OVERVIEW>
{custom_instructions}
""".strip()

MODULE_OVERVIEW_PROMPT = """
{priority_directive}
You are an AI documentation assistant. Your task is to generate a brief overview of `{module_name}` module.

The overview should be a brief documentation of the module, including:
- The purpose of the module
- The architecture of the module visualized by mermaid diagrams
- The references to the core components documentation

Provide repo structure and core components documentation of the `{module_name}` module:
<REPO_STRUCTURE>
{repo_structure}
</REPO_STRUCTURE>

Please generate the overview of the `{module_name}` module in markdown format with the following structure:
<OVERVIEW>
overview_content
</OVERVIEW>
{custom_instructions}
""".strip()

CLUSTER_REPO_PROMPT = """
Here is list of all potential core components of the repository (It's normal that some components are not essential to the repository):
<POTENTIAL_CORE_COMPONENTS>
{potential_core_components}
</POTENTIAL_CORE_COMPONENTS>

Please group the components into groups such that each group is a set of components that are closely related to each other and together they form a module. DO NOT include components that are not essential to the repository.

Each component ID has the form `<file_path>::<name>`. Return the IDs EXACTLY as given — do NOT strip the `<file_path>::` prefix or shorten the ID to the bare name.

Firstly reason about the components and then group them and return the result in the following format:
<GROUPED_COMPONENTS>
{{
    "module_name_1": {{
        "path": <path_to_the_module_1>, # the path to the module can be file or directory
        "components": [
            <component_name_1>,
            <component_name_2>,
            ...
        ]
    }},
    "module_name_2": {{
        "path": <path_to_the_module_2>,
        "components": [
            <component_name_1>,
            <component_name_2>,
            ...
        ]
    }},
    ...
}}
</GROUPED_COMPONENTS>
""".strip()

CLUSTER_MODULE_PROMPT = """
Here is the module tree of a repository:

<MODULE_TREE>
{module_tree}
</MODULE_TREE>

Here is list of all potential core components of the module {module_name} (It's normal that some components are not essential to the module):
<POTENTIAL_CORE_COMPONENTS>
{potential_core_components}
</POTENTIAL_CORE_COMPONENTS>

Please group the components into groups such that each group is a set of components that are closely related to each other and together they form a smaller module. DO NOT include components that are not essential to the module.

Each component ID has the form `<file_path>::<name>`. Return the IDs EXACTLY as given — do NOT strip the `<file_path>::` prefix or shorten the ID to the bare name.

Firstly reason based on given context and then group them and return the result in the following format:
<GROUPED_COMPONENTS>
{{
    "module_name_1": {{
        "path": <path_to_the_module_1>, # the path to the module can be file or directory
        "components": [
            <component_name_1>,
            <component_name_2>,
            ...
        ]
    }},
    "module_name_2": {{
        "path": <path_to_the_module_2>,
        "components": [
            <component_name_1>,
            <component_name_2>,
            ...
        ]
    }},
    ...
}}
</GROUPED_COMPONENTS>
""".strip()

CLUSTER_FILES_PROMPT = """
Here is a list of all files in the {scope} containing code components:
<FILES>
{files}
</FILES>

Please group the files into modules such that each group is a set of files that are closely related to each other and together they form a module. DO NOT include files that are not essential to the {scope}.

Return the file paths EXACTLY as given — do NOT modify, shorten, or strip any part of the path.

Firstly reason about the files and then group them and return the result in the following format:
<GROUPED_COMPONENTS>
{{
    "module_name_1": {{
        "path": <path_to_the_module_1>,
        "components": [
            <file_path_1>,
            <file_path_2>,
            ...
        ]
    }},
    "module_name_2": {{
        "path": <path_to_the_module_2>,
        "components": [
            <file_path_1>,
            <file_path_2>,
            ...
        ]
    }},
    ...
}}
</GROUPED_COMPONENTS>
""".strip()


FILTER_FOLDERS_PROMPT = """
Here is the list of relative paths of files, folders in 2-depth of project {project_name}:
```
{files}
```

In order to analyze the core functionality of the project, we need to analyze the files, folders representing the core functionality of the project.

Please shortlist the files, folders representing the core functionality and ignore the files, folders that are not essential to the core functionality of the project (e.g. test files, documentation files, etc.) from the list above.

Reasoning at first, then return the list of relative paths in JSON format.
"""

from typing import Dict, Any
from collections import defaultdict
from codewiki.src.utils import file_manager

EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".md": "markdown",
    ".sh": "bash",
    ".json": "json",
    ".yaml": "yaml",
    ".java": "java",
    ".js": "javascript",
    ".ts": "typescript",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".tsx": "typescript",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cxx": "cpp",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".php": "php",
    ".phtml": "php",
    ".inc": "php"
}


def _format_signature_card(node: Any) -> str:
    """Condensed signature card for a component (A: source digestion off the big model)."""
    name = node.qualified_name or node.display_name or node.name
    lines = [f"### {name} ({node.component_type})"]
    if node.base_classes:
        lines.append(f"- bases: {', '.join(node.base_classes)}")
    if node.parameters:
        lines.append(f"- params: {', '.join(list(node.parameters)[:12])}")
    if node.docstring:
        ds = " ".join(node.docstring.split())
        if len(ds) > 400:
            ds = ds[:400] + "..."
        lines.append(f"- docstring: {ds}")
    return "\n".join(lines) + "\n"


def _format_condensed_components(
    grouped_components: dict,
    components: Dict[str, Any],
    l0_summaries: dict,
    reverse_call_index: dict,
    max_content_tokens: int,
) -> str:
    """Build the CORE_COMPONENT_CODES body for condensed mode (A+C).

    Per file: L0 summary (if available) + per-component signature card +
    call-graph edges.  Falls back to a short source snippet (first N chars)
    when no L0 summary exists, so a file is never silently empty.

    The WHOLE body is bounded by ``max_content_tokens`` (tokens): summaries,
    signature cards, call-graph lines and fallback snippets all count toward
    it.  Once exhausted, remaining files are skipped with a one-line
    truncation notice — so a batch covering a giant module can never blow the
    big model's context window.  A budget of 0 means unlimited (used when the
    caller does not pass a context window).
    """
    from codewiki.src.be.utils import count_tokens

    l0 = l0_summaries or {}
    rev = reverse_call_index or {}
    parts = []
    used_tokens = 0
    # Stop adding content at 90% of the budget, leaving headroom for the
    # wrapper, module tree and the model's response.
    cap = int(max_content_tokens * 0.9) if max_content_tokens > 0 else 0
    truncated_files = 0

    def _beyond_budget(extra_tokens: int = 0) -> bool:
        # No cap => never truncate.
        return cap > 0 and (used_tokens + extra_tokens) > cap

    # Call-graph section across this module's components.
    graph_lines = []
    for path, cids in grouped_components.items():
        for cid in cids:
            node = components.get(cid)
            if not node:
                continue
            deps = [d for d in (node.depends_on or []) if d in components]
            if deps:
                name = node.qualified_name or node.name
                line = f"- {name} -> {deps}"
                callers = rev.get(cid, [])
                if callers:
                    line += f"  (called by: {callers})"
                graph_lines.append(line)
    if graph_lines:
        graph_block = "## Call Graph (component dependencies)\n" + "\n".join(graph_lines) + "\n"
        gt = count_tokens(graph_block)
        if not _beyond_budget(gt):
            parts.append(graph_block)
            used_tokens += gt

    for path, cids in grouped_components.items():
        # Once over budget, just count the rest as truncated and move on.
        if _beyond_budget():
            truncated_files += 1
            continue
        file_block_parts = [f"# File: {path}"]
        summary = l0.get(path)
        if summary:
            summary_block = f"## File Summary (L0)\n{summary}\n"
            file_block_parts.append(summary_block)
        for cid in cids:
            node = components.get(cid)
            if node:
                file_block_parts.append(_format_signature_card(node))
        # Fallback snippet when no L0 summary: first chunk of source so the
        # big model is never blind to a file it must document.
        if not summary and cids:
            node = components.get(cids[0])
            if node and node.source_code:
                remaining = cap - used_tokens if cap > 0 else len(node.source_code) // 4
                if remaining > 200:
                    char_budget = min(len(node.source_code), remaining * 4)
                    snippet = node.source_code[:char_budget]
                    if char_budget < len(node.source_code):
                        snippet += "\n# ... (snippet; full source via read_code_components)\n"
                    file_block_parts.append(
                        f"## Source snippet (no L0 summary):\n```\n{snippet}\n```\n"
                    )
        file_block = "\n".join(file_block_parts) + "\n"
        ft = count_tokens(file_block)
        # If this single file alone pushes past the cap, emit it only if we
        # still have meaningful room; otherwise count it as truncated so the
        # notice is accurate.
        if _beyond_budget(ft) and used_tokens > 0:
            truncated_files += 1
            continue
        parts.append(file_block)
        used_tokens += ft

    if truncated_files:
        parts.append(
            f"# Note: {truncated_files} additional file(s) omitted to fit "
            f"within the {max_content_tokens}-token content budget.\n"
        )

    return "\n".join(parts)


def format_l0_batch_prompt(file_paths: list[str], components: Dict[str, Any]) -> str:
    """Build a prompt asking for one <FILE_SUMMARY> per file (L0 layer, C)."""
    items = []
    for path in file_paths:
        node = next((n for n in components.values() if n.relative_path == path), None)
        if not node:
            continue
        try:
            src = file_manager.load_text(node.file_path)
        except (FileNotFoundError, IOError):
            src = node.source_code or ""
        items.append(
            f'<FILE_BATCH_ITEM path="{path}">\n```\n{src}\n```\n</FILE_BATCH_ITEM>'
        )
    return (
        f"Summarize each of the {len(items)} files below. Emit one "
        '<FILE_SUMMARY path="..."> block per file, EXACT path, 1-3 sentences '
        "+ key symbols. No code dumps.\n\n" + "\n\n".join(items)
    )


def _render_module_tree(module_tree: dict[str, any], module_name: str = "") -> str:
    """Render the full module tree as an indented string, marking the current
    module.  Extracted so fast-batch mode can render it ONCE for the whole
    batch instead of once per module (a 600-module tree is the dominant token
    cost and would otherwise be duplicated N times in one batch prompt)."""
    lines = []

    def _walk(nodes: dict[str, any], indent: int = 0):
        for key, value in nodes.items():
            if key == module_name:
                lines.append(f"{'  ' * indent}{key} (current module)")
            else:
                lines.append(f"{'  ' * indent}{key}")

            by_file = defaultdict(list)
            for c in value['components']:
                if "::" in c:
                    fpath, name = c.split("::", 1)
                    by_file[fpath].append(name)
                else:
                    by_file[""].append(c)
            for fpath, names in by_file.items():
                if fpath:
                    lines.append(f"{'  ' * (indent + 1)} {fpath}: {', '.join(names)}")
                else:
                    lines.append(f"{'  ' * (indent + 1)} {', '.join(names)}")

            if isinstance(value["children"], dict) and len(value["children"]) > 0:
                lines.append(f"{'  ' * (indent + 1)} Children:")
                _walk(value["children"], indent + 2)

    _walk(module_tree, 0)
    return "\n".join(lines)


def format_user_prompt(
    module_name: str,
    core_component_ids: list[str],
    components: Dict[str, Any],
    module_tree: dict[str, any],
    context_window: int = 0,
    condensed: bool = False,
    l0_summaries: dict = None,
    reverse_call_index: dict = None,
    render_module_tree: bool = True,
) -> str:
    """Format the user prompt with module name and organized core component codes.

    Args:
        module_name: Name of the module to document
        core_component_ids: List of component IDs to include
        components: Dictionary mapping component IDs to CodeComponent objects
        module_tree: Module tree structure for context
        context_window: Maximum model context window in tokens (0 = unlimited).
            When set, file content is truncated to stay within the limit.
        render_module_tree: When False, the (potentially huge) full module
            tree is omitted from this prompt — the caller is expected to emit
            it once for the whole batch instead (fast batch mode).  Rendering a
            600-module tree inside every per-module prompt is the dominant
            token cost and would blow the context window on its own.
    """

    from codewiki.src.be.utils import count_tokens

    if render_module_tree:
        formatted_module_tree = _render_module_tree(module_tree, module_name)
    else:
        # The shared tree is emitted once by format_fast_batch_user_prompt.
        formatted_module_tree = "(see the shared module tree above)"

    # Group core component IDs by their file path
    grouped_components: dict[str, list[str]] = {}
    for component_id in core_component_ids:
        if component_id not in components:
            continue
        component = components[component_id]
        path = component.relative_path
        if path not in grouped_components:
            grouped_components[path] = []
        grouped_components[path].append(component_id)

    # Build the core component codes section with context-window-aware truncation.
    # Reserve 60% of the context window for file content, 20% for the module tree
    # and system prompt, 20% for the model's response.
    max_content_tokens = int(context_window * 0.6) if context_window > 0 else 0
    current_content_tokens = 0
    truncated_files = []

    # Condensed mode (A+C): emit signature cards + L0 summaries + call graph
    # instead of full file source, so the big model no longer ingests raw code.
    if condensed:
        core_component_codes = _format_condensed_components(
            grouped_components, components, l0_summaries,
            reverse_call_index, max_content_tokens,
        )
        return USER_PROMPT.format(
            module_name=module_name,
            formatted_core_component_codes=core_component_codes,
            module_tree=formatted_module_tree,
        )

    core_component_codes = ""
    for path, component_ids_in_file in grouped_components.items():
        component_ids_str = "\n".join(f"- {cid}" for cid in component_ids_in_file)

        core_component_codes += f"# File: {path}\n"
        core_component_codes += f"## Core Components in this file:\n{component_ids_str}\n"
        core_component_codes += f"\n## File Content:\n```{EXTENSION_TO_LANGUAGE['.'+path.split('.')[-1]]}\n"

        try:
            file_content = file_manager.load_text(components[component_ids_in_file[0]].file_path)
        except (FileNotFoundError, IOError) as e:
            file_content = f"# Error reading file: {e}\n"

        if max_content_tokens > 0:
            content_tokens = count_tokens(file_content)
            if current_content_tokens + content_tokens > max_content_tokens:
                remaining = max_content_tokens - current_content_tokens
                if remaining > 500:
                    # Truncate file content to fit remaining budget
                    char_budget = remaining * 4  # ~4 chars per token
                    file_content = file_content[:char_budget] + "\n# ... (truncated to fit context window)\n"
                    truncated_files.append(path)
                    current_content_tokens = max_content_tokens
                else:
                    # No room left — only include file name + component list
                    file_content = "# File content omitted to fit within context window\n"
                    truncated_files.append(path)
            else:
                current_content_tokens += content_tokens

        core_component_codes += file_content
        core_component_codes += "```\n\n"

    if truncated_files:
        core_component_codes += f"# Note: {len(truncated_files)} files were truncated to fit within the {context_window}-token context window.\n"
        core_component_codes += "# You can explore the full content using the `read_code_components` tool.\n"

    return USER_PROMPT.format(module_name=module_name, formatted_core_component_codes=core_component_codes, module_tree=formatted_module_tree)



def format_cluster_prompt(potential_core_components: str, module_tree: dict[str, any] = {}, module_name: str = None) -> str:
    """
    Format the cluster prompt with potential core components and module tree.
    """

    # format module tree
    lines = []

    # print(f"Module tree:\n{json.dumps(module_tree, indent=2)}")
    
    def _format_module_tree(module_tree: dict[str, any], indent: int = 0):
        for key, value in module_tree.items():
            if key == module_name:
                lines.append(f"{'  ' * indent}{key} (current module)")
            else:
                lines.append(f"{'  ' * indent}{key}")
            
            # Group components by file
            from collections import defaultdict
            by_file = defaultdict(list)
            for c in value['components']:
                if "::" in c:
                    fpath, name = c.split("::", 1)
                    by_file[fpath].append(name)
                else:
                    by_file[""].append(c)
            for fpath, names in by_file.items():
                if fpath:
                    lines.append(f"{'  ' * (indent + 1)} {fpath}: {', '.join(names)}")
                else:
                    lines.append(f"{'  ' * (indent + 1)} {', '.join(names)}")

            if ("children" in value) and isinstance(value["children"], dict) and len(value["children"]) > 0:
                lines.append(f"{'  ' * (indent + 1)} Children:")
                _format_module_tree(value["children"], indent + 2)
    
    _format_module_tree(module_tree, 0)
    formatted_module_tree = "\n".join(lines)


    if module_tree == {}:
        return CLUSTER_REPO_PROMPT.format(potential_core_components=potential_core_components)
    else:
        return CLUSTER_MODULE_PROMPT.format(potential_core_components=potential_core_components, module_tree=formatted_module_tree, module_name=module_name)


def format_system_prompt(module_name: str, custom_instructions: str = None) -> str:
    """
    Format the system prompt with module name and optional custom instructions.
    
    Custom instructions are placed BOTH at the top (as a <PRIORITY_DIRECTIVE>)
    and at the end (as <CUSTOM_INSTRUCTIONS>) of the prompt. The top placement
    ensures language/style directives (e.g. "write in Chinese") are not
    drowned out by the dominant English prompt body.

    Args:
        module_name: Name of the module to document
        custom_instructions: Optional custom instructions to append

    Returns:
        Formatted system prompt string
    """
    custom_section = ""
    priority_directive = ""
    if custom_instructions:
        custom_section = f"\n\n<CUSTOM_INSTRUCTIONS>\n{custom_instructions}\n</CUSTOM_INSTRUCTIONS>"
        priority_directive = (
            "<PRIORITY_DIRECTIVE>\n"
            "The following instructions OVERRIDE the default behavior of this prompt. "
            "You MUST follow them strictly, even if they conflict with the language or "
            "style of the surrounding prompt.\n"
            f"{custom_instructions}\n"
            "</PRIORITY_DIRECTIVE>\n"
        )

    return SYSTEM_PROMPT.format(
        module_name=module_name,
        custom_instructions=custom_section,
        priority_directive=priority_directive,
    ).strip()


def format_leaf_system_prompt(module_name: str, custom_instructions: str = None) -> str:
    """
    Format the leaf system prompt with module name and optional custom instructions.

    Custom instructions are placed BOTH at the top (as a <PRIORITY_DIRECTIVE>)
    and at the end (as <CUSTOM_INSTRUCTIONS>) of the prompt. The top placement
    ensures language/style directives (e.g. "write in Chinese") are not
    drowned out by the dominant English prompt body.

    Args:
        module_name: Name of the module to document
        custom_instructions: Optional custom instructions to append

    Returns:
        Formatted leaf system prompt string
    """
    custom_section = ""
    priority_directive = ""
    if custom_instructions:
        custom_section = f"\n\n<CUSTOM_INSTRUCTIONS>\n{custom_instructions}\n</CUSTOM_INSTRUCTIONS>"
        priority_directive = (
            "<PRIORITY_DIRECTIVE>\n"
            "The following instructions OVERRIDE the default behavior of this prompt. "
            "You MUST follow them strictly, even if they conflict with the language or "
            "style of the surrounding prompt.\n"
            f"{custom_instructions}\n"
            "</PRIORITY_DIRECTIVE>\n"
        )

    return LEAF_SYSTEM_PROMPT.format(
        module_name=module_name,
        custom_instructions=custom_section,
        priority_directive=priority_directive,
    ).strip()


def format_fast_batch_user_prompt(
    modules: list[tuple[str, list[str]]],
    components: Dict[str, Any],
    module_tree: dict[str, any],
    context_window: int = 0,
    condensed: bool = False,
    l0_summaries: dict = None,
    reverse_call_index: dict = None,
) -> str:
    """Build a single user prompt covering *modules* at once (fast mode).

    *modules* is a list of ``(module_name, core_component_ids)`` tuples.  Each
    module is rendered via :func:`format_user_prompt` (which already embeds the
    component source code, truncated to the context window) and wrapped in a
    ``<MODULE_BATCH_ITEM>`` tag so the model can map outputs back to modules.

    When ``context_window`` is set, it is split **evenly across the modules in
    the batch** (each module gets ``0.6 × context_window / len(modules)``
    tokens of content budget) so the assembled batch stays within a single
    context window — without this, N modules each allowed 0.6×context would
    together blow the limit.
    """
    # Split the content budget evenly across modules so the whole batch
    # (sum of per-module bodies) fits in one context window.
    n_modules = max(1, len(modules))
    if context_window > 0:
        per_module_context = max(1, int(context_window * 0.6) // n_modules)
    else:
        per_module_context = 0
    # Render the (potentially huge) full module tree ONCE for the whole batch.
    # Rendering it per module would duplicate a 600-module tree N times and
    # alone blow the context window.
    shared_tree = _render_module_tree(module_tree)
    items = []
    for module_name, core_component_ids in modules:
        if not core_component_ids:
            body = f"# Module: {module_name}\n(no core components)\n"
        else:
            body = format_user_prompt(
                module_name=module_name,
                core_component_ids=core_component_ids,
                components=components,
                module_tree=module_tree,
                context_window=per_module_context,
                condensed=condensed,
                l0_summaries=l0_summaries,
                reverse_call_index=reverse_call_index,
                render_module_tree=False,
            )
        items.append(f'<MODULE_BATCH_ITEM name="{module_name}">\n{body}\n</MODULE_BATCH_ITEM>')

    return (
        f"Generate documentation for each of the {len(modules)} modules below. "
        "Emit one <MODULE_DOC name=\"...\"> block per module, in any order, "
        "using the EXACT module name from each <MODULE_BATCH_ITEM>.\n\n"
        f"<SHARED_MODULE_TREE>\n{shared_tree}\n</SHARED_MODULE_TREE>\n\n"
        + "\n\n".join(items)
    )


def format_file_cluster_prompt(file_paths: list[str], module_name: str = None) -> str:
    """Format the file-level cluster prompt for two-pass clustering.

    Used when the component-level prompt would exceed the LLM's context
    window or output token limit.  Instead of sending individual component
    IDs, we send only file paths and ask the LLM to group files into
    top-level modules.  Each module's components are then clustered in a
    follow-up recursive call.

    Args:
        file_paths: List of file paths to cluster
        module_name: Name of the current module (None for repository root)

    Returns:
        Formatted prompt string
    """
    scope = f"module {module_name}" if module_name else "repository"
    files_str = "\n".join(file_paths)
    return CLUSTER_FILES_PROMPT.format(scope=scope, files=files_str)