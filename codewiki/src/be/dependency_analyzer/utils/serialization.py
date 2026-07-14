"""Serialization helpers for checkpoint save/load of analysis artifacts.

The analysis phase (dependency analysis + call graph) is the most expensive
step — 271s for a 2063-file repo.  These helpers let us persist the result
so a resume can skip the entire analysis phase.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


def save_analysis_artifacts(
    components: Dict[str, Any],
    leaf_nodes: List[str],
    path: str,
) -> None:
    """Save lightweight component metadata (no source_code) + leaf nodes to JSON."""
    meta = {}
    for comp_id, node in components.items():
        meta[comp_id] = {
            "id": node.id,
            "name": node.name,
            "component_type": node.component_type,
            "file_path": node.file_path,
            "relative_path": node.relative_path,
            "start_line": node.start_line,
            "end_line": node.end_line,
            "has_docstring": node.has_docstring,
            "docstring": node.docstring,
            "parameters": node.parameters,
            "node_type": node.node_type,
            "base_classes": node.base_classes,
            "class_name": node.class_name,
            "display_name": node.display_name,
            "component_id": node.component_id,
            "language": node.language,
            "qualified_name": node.qualified_name,
            "depends_on": list(node.depends_on) if hasattr(node, "depends_on") else [],
        }
    payload = {
        "components": meta,
        "leaf_nodes": leaf_nodes,
    }
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        logger.info("[Checkpoint] Saved analysis artifacts (%d components, %d leaf nodes) to %s", len(meta), len(leaf_nodes), path)
    except OSError as e:
        logger.warning("[Checkpoint] Failed to save analysis artifacts: %s", e)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_analysis_artifacts(
    path: str,
) -> tuple[Dict[str, Any], List[str]]:
    """Load component metadata + leaf nodes from a checkpoint JSON file.

    Returns lightweight Node-like dicts (no source_code field) for components,
    and the original leaf_nodes list.  The caller reconstructs Node objects
    if needed.
    """
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    raw_components = payload.get("components", {})
    leaf_nodes = payload.get("leaf_nodes", [])

    from codewiki.src.be.dependency_analyzer.models.core import Node

    components: Dict[str, Node] = {}
    for comp_id, meta in raw_components.items():
        depends_on = set(meta.get("depends_on", []))
        components[comp_id] = Node(
            id=meta.get("id", comp_id),
            name=meta.get("name", ""),
            component_type=meta.get("component_type", "function"),
            file_path=meta.get("file_path", ""),
            relative_path=meta.get("relative_path", ""),
            source_code=None,
            start_line=meta.get("start_line", 0),
            end_line=meta.get("end_line", 0),
            has_docstring=meta.get("has_docstring", False),
            docstring=meta.get("docstring", ""),
            parameters=meta.get("parameters"),
            node_type=meta.get("node_type"),
            base_classes=meta.get("base_classes"),
            class_name=meta.get("class_name"),
            display_name=meta.get("display_name"),
            component_id=meta.get("component_id"),
            language=meta.get("language"),
            qualified_name=meta.get("qualified_name"),
            depends_on=depends_on,
        )

    logger.info(
        "[Checkpoint] Loaded analysis artifacts (%d components, %d leaf nodes) from %s",
        len(components), len(leaf_nodes), path,
    )
    return components, leaf_nodes
