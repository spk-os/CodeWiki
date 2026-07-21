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


def format_user_prompt(
    module_name: str,
    core_component_ids: list[str],
    components: Dict[str, Any],
    module_tree: dict[str, any],
    context_window: int = 0,
) -> str:
    """Format the user prompt with module name and organized core component codes.

    Args:
        module_name: Name of the module to document
        core_component_ids: List of component IDs to include
        components: Dictionary mapping component IDs to CodeComponent objects
        module_tree: Module tree structure for context
        context_window: Maximum model context window in tokens (0 = unlimited).
            When set, file content is truncated to stay within the limit.
    """

    from codewiki.src.be.utils import count_tokens

    # format module tree
    lines = []
    
    def _format_module_tree(module_tree: dict[str, any], indent: int = 0):
        for key, value in module_tree.items():
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
                _format_module_tree(value["children"], indent + 2)

    _format_module_tree(module_tree, 0)
    formatted_module_tree = "\n".join(lines)

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

    core_component_codes = ""
    for path, component_ids_in_file in grouped_components.items():
        component_ids_str = "\n".join(f"- {cid}" for cid in component_ids_in_file)

        core_component_codes += f"# File: {path}\n"
        core_component_codes += f"## Core Components in this file:\n{component_ids_str}\n"
        core_component_codes += f"\n## File Content:\ ```{EXTENSION_TO_LANGUAGE['.'+path.split('.')[-1]]}\n"

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
        core_component_codes += "\``\n\n"

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