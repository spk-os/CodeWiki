import logging
import os
import json
import asyncio
import threading
from typing import Dict, List, Any
from copy import deepcopy
import traceback

# Configure logging and monitoring
logger = logging.getLogger(__name__)

# Local imports
from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder
from codewiki.src.be.backend import LLMBackend, get_backend
from codewiki.src.be.checkpoint import CheckpointManager, PipelineStage
from codewiki.src.be.prompt_template import (
    REPO_OVERVIEW_PROMPT,
    MODULE_OVERVIEW_PROMPT,
)
from codewiki.src.be.cluster_modules import (
    cluster_modules,
    get_clustering_input_token_count,
)
from codewiki.src.config import (
    Config,
    FIRST_MODULE_TREE_FILENAME,
    MODULE_TREE_FILENAME,
    OVERVIEW_FILENAME
)
from codewiki.src.be.module_naming import (
    dedupe_module_tree_names,
    find_missing_module_docs,
    resolve_module_doc_path,
)
from codewiki.src.utils import file_manager


class IncompleteDocumentationError(Exception):
    """Raised when generation finishes but some modules have no doc file on disk."""

    def __init__(self, missing_modules: List[str]):
        self.missing_modules = missing_modules
        super().__init__(
            f"Documentation generation finished but {len(missing_modules)} module doc(s) "
            f"are missing: {', '.join(missing_modules)}"
        )


class DocumentationGenerator:
    """Main documentation generation orchestrator."""

    def __init__(
        self,
        config: Config,
        commit_id: str = None,
        backend: LLMBackend = None,
        ckpt: CheckpointManager = None,
    ):
        self.config = config
        self.commit_id = commit_id
        self.graph_builder = DependencyGraphBuilder(config)
        self.ckpt = ckpt
        self.backend: LLMBackend = backend or get_backend(config)
    
    def create_documentation_metadata(self, working_dir: str, components: Dict[str, Any], num_leaf_nodes: int):
        """Create a metadata file with documentation generation information."""
        from datetime import datetime
        
        metadata = {
            "generation_info": {
                "timestamp": datetime.now().isoformat(),
                "main_model": self.config.main_model,
                "generator_version": "1.0.1",
                "repo_path": self.config.repo_path,
                "commit_id": self.commit_id
            },
            "statistics": {
                "total_components": len(components),
                "leaf_nodes": num_leaf_nodes,
                "max_depth": self.config.max_depth
            },
            "files_generated": [
                "overview.md",
                "module_tree.json",
                "first_module_tree.json"
            ]
        }
        
        # Add generated markdown files to the metadata
        try:
            for file_path in os.listdir(working_dir):
                if file_path.endswith('.md') and file_path not in metadata["files_generated"]:
                    metadata["files_generated"].append(file_path)
        except Exception as e:
            logger.warning(f"Could not list generated files: {e}")
        
        metadata_path = os.path.join(working_dir, "metadata.json")
        file_manager.save_json(metadata, metadata_path)

    
    def get_processing_order(self, module_tree: Dict[str, Any], parent_path: List[str] = []) -> List[tuple[List[str], str]]:
        """Get the processing order using topological sort (leaf modules first)."""
        processing_order = []
        
        def collect_modules(tree: Dict[str, Any], path: List[str]):
            for module_name, module_info in tree.items():
                current_path = path + [module_name]
                
                # If this module has children, process them first
                if module_info.get("children") and isinstance(module_info["children"], dict) and module_info["children"]:
                    collect_modules(module_info["children"], current_path)
                    # Add this parent module after its children
                    processing_order.append((current_path, module_name))
                else:
                    # This is a leaf module, add it immediately
                    processing_order.append((current_path, module_name))
        
        collect_modules(module_tree, parent_path)
        return processing_order

    def is_leaf_module(self, module_info: Dict[str, Any]) -> bool:
        """Check if a module is a leaf module (has no children or empty children)."""
        children = module_info.get("children", {})
        return not children or (isinstance(children, dict) and len(children) == 0)

    def build_overview_structure(self, module_tree: Dict[str, Any], module_path: List[str],
                                 working_dir: str) -> Dict[str, Any]:
        """Build structure for overview generation with 1-depth children docs and target indicator."""
        
        processed_module_tree = deepcopy(module_tree)
        module_info = processed_module_tree
        for path_part in module_path:
            module_info = module_info[path_part]
            if path_part != module_path[-1]:
                module_info = module_info.get("children", {})
            else:
                module_info["is_target_for_overview_generation"] = True

        if "children" in module_info:
            module_info = module_info["children"]

        for child_name, child_info in module_info.items():
            child_docs_path = self._resolve_child_docs_path(working_dir, child_name)
            if child_docs_path is not None:
                child_info["docs"] = file_manager.load_text(child_docs_path)
            else:
                logger.warning(f"Module docs not found at {os.path.join(working_dir, f'{child_name}.md')}")
                child_info["docs"] = ""

        return processed_module_tree

    @staticmethod
    def _resolve_child_docs_path(working_dir: str, child_name: str) -> str | None:
        """Resolve the on-disk path for a child module's .md doc.

        Sub-agents sometimes save files under a sanitized variant of the
        module name (spaces → underscores, lowercased, etc.) rather than the
        exact key in the module tree. Try a small set of common variants
        before giving up so the overview prompt still gets the children's
        content as context.
        """
        return resolve_module_doc_path(working_dir, child_name)

    def validate_generated_docs(self, working_dir: str) -> List[str]:
        """Check the final module tree against the docs on disk.

        Returns the names of modules whose .md file is missing (plus
        "overview" if overview.md was never written).
        """
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        if not os.path.exists(module_tree_path):
            return []
        module_tree = file_manager.load_json(module_tree_path)
        return find_missing_module_docs(module_tree, working_dir)

    async def generate_module_documentation(self, components: Dict[str, Any], leaf_nodes: List[str]) -> str:
        """Generate documentation for all modules using dynamic programming approach."""
        # Prepare output directory
        working_dir = os.path.abspath(self.config.docs_dir)
        file_manager.ensure_directory(working_dir)

        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        first_module_tree_path = os.path.join(working_dir, FIRST_MODULE_TREE_FILENAME)
        module_tree = file_manager.load_json(module_tree_path)
        first_module_tree = file_manager.load_json(first_module_tree_path)
        
        # Get processing order (leaf modules first)
        processing_order = self.get_processing_order(first_module_tree)

        if self.ckpt is not None and processing_order:
            task_ids = ["/".join(p) for p, _ in processing_order]
            self.ckpt.register_tasks(task_ids, PipelineStage.LEAF_DOC)

        # Process modules in dependency order
        final_module_tree = module_tree
        processed_modules = set()
        tree_lock = threading.RLock()

        if len(module_tree) > 0:
            # Split processing_order into leaf and parent modules.
            # Leaf modules are independent (no dependency on other modules'
            # docs) and can be processed in parallel.  Parent modules depend
            # on their children's docs and must be processed sequentially
            # in topological order.
            leaf_modules = []
            parent_modules = []
            for module_path, module_name in processing_order:
                module_key = "/".join(module_path)
                if self.ckpt is not None and self.ckpt.is_done(module_key):
                    logger.info(f"[Resume] skipping completed module: {module_key}")
                    processed_modules.add(module_key)
                    continue

                # Look up module info in first_module_tree to determine
                # leaf vs. parent.  The tree may change during processing
                # (sub-agents can add children), but the initial clustering
                # result is the stable source of truth for the processing
                # plan.
                module_info = first_module_tree
                for path_part in module_path:
                    if path_part not in module_info:
                        module_info = {}
                        break
                    module_info = module_info[path_part]
                    if path_part != module_path[-1]:
                        module_info = module_info.get("children", {})

                if self.is_leaf_module(module_info):
                    leaf_modules.append((module_path, module_name, module_key))
                else:
                    parent_modules.append((module_path, module_name, module_key))

            if leaf_modules:
                concurrency = self.config.effective_concurrency
                semaphore = asyncio.Semaphore(concurrency)
                logger.info(
                    f"📄 Processing {len(leaf_modules)} leaf modules "
                    f"with concurrency={concurrency}"
                )

                async def process_leaf(mp, mn, mk):
                    async with semaphore:
                        try:
                            mt = file_manager.load_json(module_tree_path)
                            mi = mt
                            for pp in mp:
                                mi = mi[pp]
                                if pp != mp[-1]:
                                    mi = mi.get("children", {})

                            if self.ckpt is not None:
                                self.ckpt.mark_running(mk)

                            logger.info(f"📄 Processing leaf module: {mk}")
                            await self.backend.run_module_agent(
                                module_name=mn,
                                components=components,
                                core_component_ids=mi["components"],
                                module_path=mp,
                                working_dir=working_dir,
                                tree_lock=tree_lock,
                            )

                            expected_md = os.path.join(working_dir, f"{mn}.md")
                            if not os.path.exists(expected_md):
                                raise RuntimeError(
                                    f"Module agent completed but {mn}.md was not created"
                                )

                            if self.ckpt is not None:
                                self.ckpt.mark_done(mk)
                            processed_modules.add(mk)
                        except Exception as e:
                            logger.error(f"Failed to process module {mk}: {str(e)}")
                            logger.error(f"Traceback: {traceback.format_exc()}")
                            if self.ckpt is not None:
                                self.ckpt.mark_failed(mk, str(e))

                await asyncio.gather(
                    *[process_leaf(mp, mn, mk) for mp, mn, mk in leaf_modules]
                )

            # Process parent modules sequentially (they depend on children's docs)
            for module_path, module_name, module_key in parent_modules:
                try:
                    if module_key in processed_modules:
                        continue

                    module_tree = file_manager.load_json(module_tree_path)

                    module_info = module_tree
                    for path_part in module_path:
                        module_info = module_info[path_part]
                        if path_part != module_path[-1]:
                            module_info = module_info.get("children", {})

                    if self.ckpt is not None:
                        self.ckpt.mark_running(module_key)

                    logger.info(f"📁 Processing parent module: {module_key}")
                    parent_md_path = os.path.join(working_dir, f"{module_name}.md")
                    if (
                        self.ckpt is not None
                        and os.path.exists(parent_md_path)
                        and self.ckpt.is_done(module_key)
                    ):
                        logger.info(
                            f"[Resume] parent doc already complete for {module_key}; skipping regeneration"
                        )
                        final_module_tree = file_manager.load_json(module_tree_path)
                    else:
                        final_module_tree = await self.generate_parent_module_docs(
                            module_path, working_dir
                        )

                    if self.ckpt is not None:
                        self.ckpt.mark_done(module_key)
                    processed_modules.add(module_key)
                except Exception as e:
                    logger.error(f"Failed to process module {module_key}: {str(e)}")
                    logger.error(f"Traceback: {traceback.format_exc()}")
                    if self.ckpt is not None:
                        self.ckpt.mark_failed(module_key, str(e))
                    continue

            # Generate repo overview
            logger.info(f"📚 Generating repository overview")
            final_module_tree = await self.generate_parent_module_docs(
                [], working_dir
            )
        else:
            logger.info(f"Processing whole repo because repo can fit in the context window")
            repo_name = os.path.basename(os.path.normpath(self.config.repo_path))
            final_module_tree = await self.backend.run_module_agent(
                module_name=repo_name,
                components=components,
                core_component_ids=leaf_nodes,
                module_path=[],
                working_dir=working_dir,
                tree_lock=tree_lock,
            )

            # save final_module_tree to module_tree.json
            file_manager.save_json(final_module_tree, os.path.join(working_dir, MODULE_TREE_FILENAME))

            # rename repo_name.md to overview.md
            repo_overview_path = os.path.join(working_dir, f"{repo_name}.md")
            if os.path.exists(repo_overview_path):
                os.rename(repo_overview_path, os.path.join(working_dir, OVERVIEW_FILENAME))
        
        return working_dir

    async def generate_parent_module_docs(self, module_path: List[str], 
                                        working_dir: str) -> Dict[str, Any]:
        """Generate documentation for a parent module based on its children's documentation."""
        module_name = module_path[-1] if len(module_path) >= 1 else os.path.basename(os.path.normpath(self.config.repo_path))

        logger.info(f"Generating parent documentation for: {module_name}")
        
        # Load module tree
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        module_tree = file_manager.load_json(module_tree_path)

        # check if overview docs already exists
        overview_docs_path = os.path.join(working_dir, OVERVIEW_FILENAME)
        if os.path.exists(overview_docs_path):
            logger.info(f"✓ Overview docs already exists at {overview_docs_path}")
            return module_tree

        # check if parent docs already exists
        parent_docs_path = os.path.join(working_dir, f"{module_name if len(module_path) >= 1 else OVERVIEW_FILENAME.replace('.md', '')}.md")
        if os.path.exists(parent_docs_path):
            logger.info(f"✓ Parent docs already exists at {parent_docs_path}")
            return module_tree

        # Create repo structure with 1-depth children docs and target indicator
        repo_structure = self.build_overview_structure(module_tree, module_path, working_dir)

        _ci = self.config.get_prompt_addition()
        _custom_section = ""
        _priority_directive = ""
        if _ci:
            _custom_section = f"\n\n<CUSTOM_INSTRUCTIONS>\n{_ci}\n</CUSTOM_INSTRUCTIONS>"
            _priority_directive = (
                "<PRIORITY_DIRECTIVE>\n"
                "The following instructions OVERRIDE the default behavior of this prompt. "
                "You MUST follow them strictly, even if they conflict with the language or "
                "style of the surrounding prompt.\n"
                f"{_ci}\n"
                "</PRIORITY_DIRECTIVE>\n"
            )

        prompt = MODULE_OVERVIEW_PROMPT.format(
            module_name=module_name,
            repo_structure=json.dumps(repo_structure, indent=4),
            custom_instructions=_custom_section,
            priority_directive=_priority_directive,
        ) if len(module_path) >= 1 else REPO_OVERVIEW_PROMPT.format(
            repo_name=module_name,
            repo_structure=json.dumps(repo_structure, indent=4),
            custom_instructions=_custom_section,
            priority_directive=_priority_directive,
        )
        
        try:
            parent_docs = self.backend.complete(prompt)

            # Parse and save parent documentation. Subscription-CLI backends
            # (claude-code / codex) sometimes ignore the <OVERVIEW> wrapper and
            # return raw markdown; fall back to the response as-is in that case
            # rather than crashing with an index error.
            if "<OVERVIEW>" in parent_docs and "</OVERVIEW>" in parent_docs:
                parent_content = parent_docs.split("<OVERVIEW>")[1].split("</OVERVIEW>")[0].strip()
            else:
                logger.warning(
                    f"Overview response for {module_name} missing <OVERVIEW> wrapper; "
                    f"using raw response as markdown."
                )
                parent_content = parent_docs.strip()
            file_manager.save_text(parent_content, parent_docs_path)
            
            logger.debug(f"Successfully generated parent documentation for: {module_name}")
            return module_tree
            
        except Exception as e:
            logger.error(f"Error generating parent documentation for {module_name}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
    
    async def run(self) -> None:
        """Run the complete documentation generation process using dynamic programming."""
        try:
            # --- Checkpoint: Resume from analysis phase if available ---
            analysis_done = False
            if self.ckpt is not None and self.ckpt.is_done("dep_analysis"):
                from codewiki.src.be.dependency_analyzer.utils.serialization import (
                    load_analysis_artifacts,
                )
                artifacts_path = self.ckpt.state.analysis_artifacts_path
                if artifacts_path and os.path.exists(artifacts_path):
                    logger.info("[Resume] Loading analysis artifacts from checkpoint...")
                    components, leaf_nodes = load_analysis_artifacts(artifacts_path)
                    analysis_done = True
                    logger.info(
                        "[Resume] Skipped dependency analysis — loaded %d components, %d leaf nodes",
                        len(components), len(leaf_nodes),
                    )

            if not analysis_done:
                # Build dependency graph
                components, leaf_nodes = self.graph_builder.build_dependency_graph()

                # Strip source_code from all components to free memory (source is read
                # from disk on demand during doc generation).  This can free 30-50+ MB
                # for large repos with 10000+ components.
                import gc
                freed_bytes = 0
                for node in components.values():
                    if node.source_code is not None:
                        freed_bytes += len(node.source_code.encode("utf-8"))
                        node.source_code = None
                gc.collect()
                if freed_bytes > 0:
                    logger.info(
                        "[Memory] Freed %d bytes by stripping source_code from %d Node objects",
                        freed_bytes, len(components),
                    )

                # Save analysis artifacts to checkpoint (lightweight, no source_code)
                if self.ckpt is not None:
                    from codewiki.src.be.dependency_analyzer.utils.serialization import (
                        save_analysis_artifacts,
                    )
                    analysis_path = os.path.join(working_dir, "analysis_artifacts.json")
                    save_analysis_artifacts(components, leaf_nodes, analysis_path)
                    self.ckpt.set_stage_artifact("analysis_artifacts_path", analysis_path)
                    self.ckpt.mark_done("dep_analysis")

            logger.debug(f"Found {len(leaf_nodes)} leaf nodes")
            
            # Cluster modules
            working_dir = os.path.abspath(self.config.docs_dir)
            file_manager.ensure_directory(working_dir)
            first_module_tree_path = os.path.join(working_dir, FIRST_MODULE_TREE_FILENAME)
            module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)

            if self.ckpt is not None:
                dep_graph_path = os.path.join(working_dir, "dep_graph.json")
                try:
                    file_manager.save_json(
                        {"components_count": len(components), "leaf_nodes": list(leaf_nodes)},
                        dep_graph_path,
                    )
                    self.ckpt.set_stage_artifact("dep_graph_path", dep_graph_path)
                except Exception as e:
                    logger.warning(f"Failed to persist dep_graph artifact: {e}")

            # --- Checkpoint: Resume from clustering phase if available ---
            clustering_done = False
            if self.ckpt is not None and self.ckpt.is_done("module_clustering"):
                clustering_done = True
                module_tree_path_artifact = self.ckpt.state.module_tree_path
                if module_tree_path_artifact and os.path.exists(module_tree_path_artifact):
                    module_tree = file_manager.load_json(module_tree_path_artifact)
                    first_module_tree = file_manager.load_json(first_module_tree_path) if os.path.exists(first_module_tree_path) else module_tree
                    logger.info("[Resume] Skipped module clustering — loaded cached module tree")
                else:
                    clustering_done = False  # Artifact missing, re-cluster

            if not clustering_done:
                # Check if module tree exists
                if os.path.exists(first_module_tree_path):
                    logger.debug(f"Module tree found at {first_module_tree_path}")
                    module_tree = file_manager.load_json(first_module_tree_path)
                else:
                    logger.debug(f"Module tree not found at {module_tree_path}, clustering modules")
                    clustering_tokens = get_clustering_input_token_count(
                        leaf_nodes, components
                    )
                    logger.info(
                        "Preparing %d leaf nodes for module clustering (%d tokens, threshold %d)",
                        len(leaf_nodes),
                        clustering_tokens,
                        self.config.max_token_per_module,
                    )
                    cluster_model = self.config.cluster_model or None
                    module_tree = cluster_modules(
                        leaf_nodes,
                        components,
                        self.config,
                        completer=lambda p: self.backend.complete(p, model=cluster_model),
                        checkpoint=self.ckpt,
                    )
                    file_manager.save_json(module_tree, first_module_tree_path)

                if self.ckpt is not None:
                    self.ckpt.mark_done("module_clustering")
            
            # Check if module tree exists
            if os.path.exists(first_module_tree_path):
                logger.debug(f"Module tree found at {first_module_tree_path}")
                module_tree = file_manager.load_json(first_module_tree_path)
            else:
                logger.debug(f"Module tree not found at {module_tree_path}, clustering modules")
                clustering_tokens = get_clustering_input_token_count(
                    leaf_nodes, components
                )
                logger.info(
                    "Preparing %d leaf nodes for module clustering (%d tokens, threshold %d)",
                    len(leaf_nodes),
                    clustering_tokens,
                    self.config.max_token_per_module,
                )
                # Bind cluster_model into the completer so the backend uses the
                # configured clustering model (separate from main_model) when
                # one is set.  Caw mode's cluster_model is typically empty —
                # complete() falls back to its own _model in that case.
                cluster_model = self.config.cluster_model or None
                module_tree = cluster_modules(
                    leaf_nodes,
                    components,
                    self.config,
                    completer=lambda p: self.backend.complete(p, model=cluster_model),
                )
                # Only freshly clustered trees are deduped: renaming a cached
                # key whose .md already exists would orphan the doc.
                module_tree = dedupe_module_tree_names(module_tree)
                file_manager.save_json(module_tree, first_module_tree_path)
            
            file_manager.save_json(module_tree, module_tree_path)

            if self.ckpt is not None:
                self.ckpt.set_stage_artifact("module_tree_path", module_tree_path)
            
            if len(module_tree) == 0:
                logger.info(
                    "Module clustering produced no top-level modules; continuing in "
                    "whole-repository documentation mode"
                )
            else:
                logger.info(
                    "Grouped components into %d top-level modules",
                    len(module_tree),
                )
            
            # Generate module documentation using dynamic programming approach
            # This processes leaf modules first, then parent modules
            working_dir = await self.generate_module_documentation(components, leaf_nodes)
            
            # Create documentation metadata
            self.create_documentation_metadata(working_dir, components, len(leaf_nodes))

            # Reconcile the final module tree against the docs on disk so
            # name collisions or failed sub-agents can't pass silently (issue #76)
            missing_docs = self.validate_generated_docs(working_dir)
            if missing_docs:
                for module_name in missing_docs:
                    logger.error(f"Module doc missing after generation: {module_name}.md")
                raise IncompleteDocumentationError(missing_docs)

            logger.debug(f"Documentation generation completed successfully using dynamic programming!")
            logger.debug(f"Processing order: leaf modules → parent modules → repository overview")
            logger.debug(f"Documentation saved to: {working_dir}")
            
        except Exception as e:
            logger.error(f"Documentation generation failed: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
