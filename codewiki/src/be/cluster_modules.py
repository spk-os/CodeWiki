from typing import List, Dict, Any, Callable, Optional
from collections import defaultdict
from pathlib import PurePosixPath
import json
import logging
import traceback
from codewiki.src.be.checkpoint import CheckpointManager
logger = logging.getLogger(__name__)

from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.llm_services import call_llm
from codewiki.src.be.utils import count_tokens
from codewiki.src.config import Config
from codewiki.src.be.prompt_template import format_cluster_prompt, format_file_cluster_prompt

Completer = Callable[[str], str]

# When whole-repo mode is chosen but leaf entry points touch fewer than this
# fraction of parsed files, warn that coverage depends on agent exploration.
LOW_COVERAGE_RATIO = 0.5

_CLUSTER_TREE_MODEL = "__cluster_tree__"


def format_potential_core_components(leaf_nodes: List[str], components: Dict[str, Node]) -> tuple[str, str]:
    """Format the potential core components into two strings:
    
    1. A names-only string used in the clustering prompt (lightweight, no source_code)
    2. A names+source_code string used for token counting to decide whether
       clustering is needed (measures repo complexity against threshold)
    """
    valid_leaf_nodes = []
    for leaf_node in leaf_nodes:
        if leaf_node in components:
            valid_leaf_nodes.append(leaf_node)
        else:
            logger.warning(f"Skipping invalid leaf node '{leaf_node}' - not found in components")
    
    leaf_nodes_by_file = defaultdict(list)
    for leaf_node in valid_leaf_nodes:
        leaf_nodes_by_file[components[leaf_node].relative_path].append(leaf_node)

    potential_core_components = ""
    potential_core_components_with_code = ""
    for file, leaf_nodes in dict(sorted(leaf_nodes_by_file.items())).items():
        potential_core_components += f"# {file}\n"
        potential_core_components_with_code += f"# {file}\n"
        for leaf_node in leaf_nodes:
            potential_core_components += f"\t{leaf_node}\n"
            potential_core_components_with_code += f"\t{leaf_node}\n"
            src = components[leaf_node].source_code
            if src is not None:
                potential_core_components_with_code += f"{src}\n"
            else:
                # source_code was stripped after analysis; read from disk for
                # accurate token counting (needed to decide if clustering is required)
                try:
                    from pathlib import Path
                    from codewiki.src.be.dependency_analyzer.utils.security import safe_open_text
                    repo_path = Path(components[leaf_node].file_path)
                    # Find repo root
                    while repo_path != repo_path.parent and not (repo_path / '.git').exists():
                        repo_path = repo_path.parent
                    if not (repo_path / '.git').exists():
                        repo_path = Path(components[leaf_node].file_path)
                    abs_file = repo_path / components[leaf_node].relative_path
                    src = safe_open_text(repo_path, abs_file)
                    # Extract the relevant lines if start/end are known
                    sl = components[leaf_node].start_line
                    el = components[leaf_node].end_line
                    if sl > 0 and el > 0:
                        lines = src.splitlines()
                        if sl <= len(lines):
                            src = "\n".join(lines[sl-1:el])
                    potential_core_components_with_code += f"{src}\n"
                except Exception:
                    # If we can't read the file, use an estimated size
                    # (~3 tokens per line, ~15 lines per function)
                    estimated_tokens = 45
                    potential_core_components_with_code += f"# [source unavailable, ~{estimated_tokens} tokens]\n"

    return potential_core_components, potential_core_components_with_code


def estimate_clustering_tokens(num_leaf_nodes: int, num_components: int) -> int:
    """Estimate clustering input tokens without building the full source-code string.

    Only used for very large repos (>2000 leaf nodes) where building the
    source-code string would consume excessive memory.  For most repos,
    get_clustering_input_token_count builds the string precisely.
    """
    avg_lines_per_function = 15
    avg_tokens_per_line = 3
    header_tokens_per_component = 5
    estimated = num_leaf_nodes * (avg_lines_per_function * avg_tokens_per_line + header_tokens_per_component)
    return estimated


def get_clustering_input_token_count(
    leaf_nodes: List[str], components: Dict[str, Node]
) -> int:
    """Count the tokens used to decide whether a module needs clustering.

    This counts the source_code version (not just names), because the
    threshold comparison measures repo complexity.  For very large repos
    (>2000 leaf nodes), uses estimation to avoid excessive memory usage.
    """
    if len(leaf_nodes) > 2000:
        return estimate_clustering_tokens(len(leaf_nodes), len(components))

    _, potential_core_components_with_code = format_potential_core_components(
        leaf_nodes, components
    )
    return count_tokens(potential_core_components_with_code)


def _format_with_code(leaf_nodes: List[str], components: Dict[str, Node]) -> str:
    """Build the full source-code string for token counting.

    This is the same as the second element of format_potential_core_components.
    Kept for backward compatibility with any direct callers.
    """
    _, with_code = format_potential_core_components(leaf_nodes, components)
    return with_code


def _safe_prompt_limit(config: Config) -> int:
    """Max tokens for the names-only clustering prompt before two-pass kicks in.

    Reserves 60% of the context window for the prompt template, system
    prompt, and the LLM's response (which contains all component IDs in
    JSON format — roughly equal to the input size).
    """
    return int(config.effective_context_window * 0.4)


def _max_leaf_nodes_per_call(config: Config) -> int:
    """Max leaf nodes per single LLM clustering call.

    The LLM must return every component ID in its JSON response.  Each
    ID costs ~12 tokens plus JSON overhead (~25 tokens total).  We cap
    the number of IDs per call so the response fits within
    ``config.max_tokens``.
    """
    return max(50, config.max_tokens // 25)


def _cluster_cache_key(leaf_nodes: List[str], module_name: str) -> str:
    """Build a deterministic cache key from sorted leaf nodes and module name."""
    return json.dumps({"m": module_name or "root", "n": sorted(leaf_nodes)}, ensure_ascii=False)


def _get_cached_tree(
    checkpoint: Optional[CheckpointManager],
    leaf_nodes: List[str],
    module_name: str,
) -> Optional[Dict[str, Any]]:
    if checkpoint is None:
        return None
    key = _cluster_cache_key(leaf_nodes, module_name)
    cached = checkpoint.get_llm_cache(key, _CLUSTER_TREE_MODEL)
    if cached is None:
        return None
    try:
        tree = json.loads(cached)
        if isinstance(tree, dict):
            return tree
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _save_cached_tree(
    checkpoint: Optional[CheckpointManager],
    leaf_nodes: List[str],
    module_name: str,
    tree: Dict[str, Any],
) -> None:
    if checkpoint is None or not tree:
        return
    key = _cluster_cache_key(leaf_nodes, module_name)
    checkpoint.save_llm_cache(key, _CLUSTER_TREE_MODEL, json.dumps(tree, ensure_ascii=False))


def _cached_llm_call(
    prompt: str,
    config: Config,
    *,
    model: Optional[str] = None,
    completer: Optional[Completer] = None,
    checkpoint: Optional[CheckpointManager] = None,
    label: str = "",
) -> str:
    effective_model = model or config.cluster_model or config.main_model
    if checkpoint is not None:
        cached = checkpoint.get_llm_cache(prompt, effective_model)
        if cached is not None:
            logger.info("[Resume] Clustering LLM cache hit for %s", label or effective_model)
            return cached
    if completer is not None:
        response = completer(prompt)
    else:
        response = call_llm(prompt, config, model=model)
    if checkpoint is not None and response:
        checkpoint.save_llm_cache(prompt, effective_model, response)
        logger.info("[Checkpoint] Saved clustering LLM response for %s", label or effective_model)
    return response


def _parse_cluster_response(response: str, module_label: str) -> Dict[str, Any]:
    """Parse the LLM's ``<GROUPED_COMPONENTS>`` response into a module tree.

    Returns an empty dict on any parse failure.
    """
    try:
        if "<GROUPED_COMPONENTS>" not in response or "</GROUPED_COMPONENTS>" not in response:
            logger.warning(
                "Invalid LLM clustering response for %s: missing <GROUPED_COMPONENTS> "
                "tags; falling back. Response preview: %s...",
                module_label,
                response[:200],
            )
            return {}

        response_content = response.split("<GROUPED_COMPONENTS>")[1].split("</GROUPED_COMPONENTS>")[0]
        try:
            module_tree = json.loads(response_content)
        except (json.JSONDecodeError, ValueError):
            import ast
            module_tree = ast.literal_eval(response_content)

        if not isinstance(module_tree, dict):
            logger.error(f"Invalid module tree format - expected dict, got {type(module_tree)}")
            return {}

        return module_tree

    except Exception as e:
        logger.warning(
            "Failed to parse LLM clustering response for %s; falling back. "
            "Error: %s. Response preview: %s...",
            module_label,
            e,
            response[:200],
        )
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {}


def _merge_module_tree(
    current_module_tree: dict[str, Any],
    module_tree: dict[str, Any],
    current_module_path: List[str],
) -> None:
    """Merge *module_tree* into *current_module_tree* at *current_module_path*.

    Mirrors the merge logic originally inline in ``cluster_modules`` so that
    both the single-pass and two-pass paths stay consistent.
    """
    if current_module_tree == {}:
        current_module_tree.clear()
        current_module_tree.update(module_tree)
    else:
        value = current_module_tree
        for key in current_module_path:
            value = value[key]["children"]
        for module_name, module_info in module_tree.items():
            if "path" in module_info:
                del module_info["path"]
            value[module_name] = module_info


def _directory_pre_cluster(
    leaf_nodes: List[str],
    components: Dict[str, Node],
    config: Config,
    current_module_tree: dict[str, Any],
    current_module_name: str,
    current_module_path: List[str],
    completer: Optional[Completer] = None,
    _depth: int = 0,
    checkpoint: Optional[CheckpointManager] = None,
) -> Optional[Dict[str, Any]]:
    """Fallback: split leaf nodes by directory and recursively cluster each group.

    Returns ``None`` to signal the caller that it should proceed with
    single-pass LLM clustering (when no further splitting is possible).
    """
    dir_groups: dict[str, list[str]] = defaultdict(list)
    for leaf_node in leaf_nodes:
        rel_path = components[leaf_node].relative_path
        dir_path = str(PurePosixPath(rel_path).parent)
        dir_groups[dir_path].append(leaf_node)

    if len(dir_groups) <= 1:
        file_groups: dict[str, list[str]] = defaultdict(list)
        for leaf_node in leaf_nodes:
            rel_path = components[leaf_node].relative_path
            file_groups[rel_path].append(leaf_node)

        if len(file_groups) <= 1:
            return None
        dir_groups = file_groups

    module_label = current_module_name or "repository"
    logger.info(
        "Pre-clustering %s into %d groups by directory structure",
        module_label,
        len(dir_groups),
    )

    module_tree: dict[str, Any] = {}
    used_names: set[str] = set()
    for group_path, group_leaf_nodes in sorted(dir_groups.items()):
        group_name = PurePosixPath(group_path).name or group_path
        base_name = group_name
        counter = 2
        while group_name in used_names:
            group_name = f"{base_name}_{counter}"
            counter += 1
        used_names.add(group_name)

        module_tree[group_name] = {
            "path": group_path,
            "components": list(group_leaf_nodes),
            "children": {},
        }

    _merge_module_tree(current_module_tree, module_tree, current_module_path)

    for group_name, group_info in module_tree.items():
        group_leaf_nodes = group_info["components"]
        if len(group_leaf_nodes) <= 1:
            continue
        current_module_path.append(group_name)
        try:
            group_info["children"] = cluster_modules(
                group_leaf_nodes,
                components,
                config,
                current_module_tree,
                group_name,
                current_module_path,
                completer=completer,
                _depth=_depth + 1,
                checkpoint=checkpoint,
            )
        finally:
            current_module_path.pop()

    return module_tree


def _two_pass_cluster(
    leaf_nodes: List[str],
    components: Dict[str, Node],
    config: Config,
    current_module_tree: dict[str, Any],
    current_module_name: str,
    current_module_path: List[str],
    completer: Optional[Completer] = None,
    _depth: int = 0,
    checkpoint: Optional[CheckpointManager] = None,
) -> Dict[str, Any]:
    """Two-pass clustering for super-large projects.

    Pass 1 — file-level: send only file paths (not individual component
    IDs) to the LLM.  The LLM groups files into top-level modules.
    This prompt is dramatically smaller: ~2K file paths vs ~11K
    component IDs.

    Pass 2 — component-level: for each module from Pass 1, recursively
    call ``cluster_modules`` with only that module's components.  Each
    call is small enough for the LLM to handle.

    If the file-level prompt is *still* too large, or the LLM response
    is invalid, falls back to ``_directory_pre_cluster``.
    """
    module_label = current_module_name or "repository"

    file_to_leaf_nodes: dict[str, list[str]] = defaultdict(list)
    for leaf_node in leaf_nodes:
        file_path = components[leaf_node].relative_path
        file_to_leaf_nodes[file_path].append(leaf_node)

    file_list = sorted(file_to_leaf_nodes.keys())

    file_prompt_str = "\n".join(file_list)
    file_prompt_tokens = count_tokens(file_prompt_str)

    if file_prompt_tokens > _safe_prompt_limit(config):
        logger.info(
            "File-level prompt for %s also too large (%d tokens); using "
            "directory-based pre-clustering",
            module_label,
            file_prompt_tokens,
        )
        result = _directory_pre_cluster(
            leaf_nodes, components, config,
            current_module_tree, current_module_name,
            current_module_path, completer,
            _depth=_depth,
            checkpoint=checkpoint,
        )
        if result is not None:
            return result
        # Can't split further — fall through to single-pass LLM
        return _single_pass_cluster(
            leaf_nodes, components, config,
            current_module_tree, current_module_name,
            current_module_path, completer,
            _depth=_depth,
            checkpoint=checkpoint,
        )

    logger.info(
        "Pass 1: clustering %d files into top-level modules for %s (%d tokens)",
        len(file_list),
        module_label,
        file_prompt_tokens,
    )

    file_prompt = format_file_cluster_prompt(file_list, current_module_name)
    response = _cached_llm_call(
        file_prompt, config,
        model=config.cluster_model,
        completer=completer,
        checkpoint=checkpoint,
        label=f"Pass 1 file-level for {module_label}",
    )

    file_module_tree = _parse_cluster_response(response, module_label)

    if not file_module_tree or len(file_module_tree) <= 1:
        logger.warning(
            "File-level clustering for %s produced %d modules; falling back "
            "to directory-based pre-clustering",
            module_label,
            len(file_module_tree) if file_module_tree else 0,
        )
        result = _directory_pre_cluster(
            leaf_nodes, components, config,
            current_module_tree, current_module_name,
            current_module_path, completer,
            _depth=_depth,
            checkpoint=checkpoint,
        )
        if result is not None:
            return result
        return _single_pass_cluster(
            leaf_nodes, components, config,
            current_module_tree, current_module_name,
            current_module_path, completer,
            _depth=_depth,
            checkpoint=checkpoint,
        )

    logger.info(
        "Pass 1 complete: %d top-level modules for %s",
        len(file_module_tree),
        module_label,
    )

    module_tree: dict[str, Any] = {}
    for mod_name, mod_info in file_module_tree.items():
        mod_files = mod_info.get("components", [])
        mod_leaf_nodes: list[str] = []
        for fp in mod_files:
            if fp in file_to_leaf_nodes:
                mod_leaf_nodes.extend(file_to_leaf_nodes[fp])
            else:
                logger.warning(
                    "File '%s' from LLM clustering response not found in "
                    "components for %s; skipping",
                    fp,
                    module_label,
                )
        module_tree[mod_name] = {
            "path": mod_info.get("path", ""),
            "components": mod_leaf_nodes,
            "children": {},
        }

    _merge_module_tree(current_module_tree, module_tree, current_module_path)

    logger.info(
        "Pass 2: recursively clustering components within each module for %s",
        module_label,
    )

    total_leaf_count = len(leaf_nodes)
    for mod_name, mod_info in module_tree.items():
        mod_leaf_nodes = mod_info["components"]
        if len(mod_leaf_nodes) <= 1:
            continue

        # Anti-recursion: if a module contains >= 90% of all leaf nodes,
        # the LLM didn't actually split anything.  Use single-pass for
        # this module to avoid infinite recursion.
        if len(mod_leaf_nodes) >= total_leaf_count * 0.9:
            logger.warning(
                "Module '%s' contains %d/%d leaf nodes (%.0f%%); using "
                "single-pass to avoid recursion",
                mod_name,
                len(mod_leaf_nodes),
                total_leaf_count,
                len(mod_leaf_nodes) / total_leaf_count * 100,
            )
            current_module_path.append(mod_name)
            try:
                mod_info["children"] = _single_pass_cluster(
                    mod_leaf_nodes, components, config,
                    current_module_tree, mod_name,
                    current_module_path, completer,
                    _depth=_depth + 1,
                    checkpoint=checkpoint,
                )
            finally:
                current_module_path.pop()
            continue

        current_module_path.append(mod_name)
        try:
            mod_info["children"] = cluster_modules(
                mod_leaf_nodes,
                components,
                config,
                current_module_tree,
                mod_name,
                current_module_path,
                completer=completer,
                _depth=_depth + 1,
                checkpoint=checkpoint,
            )
        finally:
            current_module_path.pop()

    return module_tree


def _single_pass_cluster(
    leaf_nodes: List[str],
    components: Dict[str, Node],
    config: Config,
    current_module_tree: dict[str, Any],
    current_module_name: str,
    current_module_path: List[str],
    completer: Optional[Completer] = None,
    _depth: int = 0,
    checkpoint: Optional[CheckpointManager] = None,
) -> Dict[str, Any]:
    """Original single-pass LLM clustering — the last-resort fallback.

    Sends all component names to the LLM in one request.  May fail if the
    prompt exceeds the LLM's context window, but no further splitting is
    possible.
    """
    module_label = current_module_name or "repository"

    potential_core_components, _ = format_potential_core_components(leaf_nodes, components)
    prompt = format_cluster_prompt(potential_core_components, current_module_tree, current_module_name)

    logger.info(
        "Requesting LLM module clustering for %s (single-pass, %d leaf nodes)",
        module_label,
        len(leaf_nodes),
    )
    response = _cached_llm_call(
        prompt, config,
        model=config.cluster_model,
        completer=completer,
        checkpoint=checkpoint,
        label=f"single-pass for {module_label}",
    )

    module_tree = _parse_cluster_response(response, module_label)

    if not module_tree or len(module_tree) <= 1:
        logger.info(
            "Single-pass clustering for %s produced %d module(s); using "
            "whole-module documentation mode.",
            module_label,
            len(module_tree) if module_tree else 0,
        )
        return {}

    logger.info(
        "LLM module clustering for %s produced %d top-level modules.",
        module_label,
        len(module_tree),
    )

    _merge_module_tree(current_module_tree, module_tree, current_module_path)

    for module_name, module_info in module_tree.items():
        sub_leaf_nodes = module_info.get("components", [])
        valid_sub_leaf_nodes = [
            n for n in sub_leaf_nodes if n in components
        ]
        for n in sub_leaf_nodes:
            if n not in components:
                logger.warning(
                    "Skipping invalid sub leaf node '%s' in module '%s' - "
                    "not found in components",
                    n, module_name,
                )

        current_module_path.append(module_name)
        module_info["children"] = {}
        try:
            module_info["children"] = cluster_modules(
                valid_sub_leaf_nodes,
                components,
                config,
                current_module_tree,
                module_name,
                current_module_path,
                completer=completer,
                _depth=_depth + 1,
                checkpoint=checkpoint,
            )
        finally:
            current_module_path.pop()

    return module_tree


def cluster_modules(
    leaf_nodes: List[str],
    components: Dict[str, Node],
    config: Config,
    current_module_tree: Optional[dict[str, Any]] = None,
    current_module_name: str = None,
    current_module_path: Optional[List[str]] = None,
    completer: Optional[Completer] = None,
    _depth: int = 0,
    checkpoint: Optional[CheckpointManager] = None,
) -> Dict[str, Any]:
    """
    Cluster the potential core components into modules.

    For super-large projects where the names-only prompt would exceed the
    LLM's context window or output token limit, automatically switches to
    two-pass clustering (file-level → component-level).

    Args:
        completer: optional ``(prompt: str) -> str`` callable.  When provided,
            clustering calls go through this completer instead of the legacy
            ``call_llm``.  This is how the LLMBackend abstraction injects
            subscription-mode (caw) routing.  If ``None``, falls back to
            ``call_llm`` for backward compatibility with direct callers.
        checkpoint: optional ``CheckpointManager`` for caching LLM responses
            and module trees.  When provided, completed clustering calls are
            cached to disk and skipped on re-run.
    """
    if current_module_tree is None:
        current_module_tree = {}
    if current_module_path is None:
        current_module_path = []

    module_label = current_module_name or "repository"

    cached_tree = _get_cached_tree(checkpoint, leaf_nodes, current_module_name)
    if cached_tree is not None:
        logger.info(
            "[Resume] Clustering cache hit for %s (%d leaf nodes, depth %d)",
            module_label,
            len(leaf_nodes),
            _depth,
        )
        _merge_module_tree(current_module_tree, cached_tree, current_module_path)
        return cached_tree

    potential_core_components, potential_core_components_with_code = (
        format_potential_core_components(leaf_nodes, components)
    )
    input_tokens = count_tokens(potential_core_components_with_code)
    threshold = config.max_token_per_module

    logger.info(
        "Module clustering input for %s: %d leaf nodes, %d tokens, threshold %d",
        module_label,
        len(leaf_nodes),
        input_tokens,
        threshold,
    )

    if input_tokens <= threshold:
        logger.info(
            "Skipping LLM module clustering for %s because %d tokens fit within the "
            "%d-token threshold; using whole-module documentation mode.",
            module_label,
            input_tokens,
            threshold,
        )
        if current_module_name is None:
            leaf_files = {
                components[leaf_node].relative_path
                for leaf_node in leaf_nodes
                if leaf_node in components
            }
            all_files = {c.relative_path for c in components.values()}
            if all_files and len(leaf_files) / len(all_files) < LOW_COVERAGE_RATIO:
                logger.warning(
                    "Leaf-node entry points cover only %d of %d parsed files (%.0f%%). "
                    "Whole-repository documentation will start from these entry points and "
                    "rely on agent exploration to reach the rest of the codebase.",
                    len(leaf_files),
                    len(all_files),
                    100 * len(leaf_files) / len(all_files),
                )
        return {}

    names_only_tokens = count_tokens(potential_core_components)
    safe_limit = _safe_prompt_limit(config)
    max_leaf_nodes = _max_leaf_nodes_per_call(config)

    if _depth >= 5:
        logger.warning(
            "Recursion depth %d reached for %s; forcing single-pass to "
            "prevent infinite recursion",
            _depth,
            module_label,
        )
        result = _single_pass_cluster(
            leaf_nodes, components, config,
            current_module_tree, current_module_name,
            current_module_path, completer,
            _depth=_depth,
            checkpoint=checkpoint,
        )
        _save_cached_tree(checkpoint, leaf_nodes, current_module_name, result)
        return result

    if names_only_tokens > safe_limit or len(leaf_nodes) > max_leaf_nodes:
        logger.info(
            "Using two-pass clustering for %s: %d names-only tokens (safe limit %d), "
            "%d leaf nodes (max per call %d), depth %d",
            module_label,
            names_only_tokens,
            safe_limit,
            len(leaf_nodes),
            max_leaf_nodes,
            _depth,
        )
        result = _two_pass_cluster(
            leaf_nodes, components, config,
            current_module_tree, current_module_name,
            current_module_path, completer,
            _depth=_depth,
            checkpoint=checkpoint,
        )
        _save_cached_tree(checkpoint, leaf_nodes, current_module_name, result)
        return result

    result = _single_pass_cluster(
        leaf_nodes, components, config,
        current_module_tree, current_module_name,
        current_module_path, completer,
        _depth=_depth,
        checkpoint=checkpoint,
    )
    _save_cached_tree(checkpoint, leaf_nodes, current_module_name, result)
    return result
