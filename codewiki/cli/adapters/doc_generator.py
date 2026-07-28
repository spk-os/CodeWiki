"""
CLI adapter for documentation generator backend.

This adapter wraps the existing backend documentation_generator.py
and provides CLI-specific functionality like progress reporting.
"""

from pathlib import Path
from typing import Dict, Any
import time
import asyncio
import gc
import os
import logging
import sys


from codewiki.cli.utils.progress import ProgressTracker
from codewiki.cli.models.job import DocumentationJob, LLMConfig
from codewiki.cli.utils.errors import APIError, IncompleteGenerationError

# Import backend modules
from codewiki.src.be.documentation_generator import DocumentationGenerator
from codewiki.src.config import Config as BackendConfig, set_cli_context


class CLIDocumentationGenerator:
    """
    CLI adapter for documentation generation with progress reporting.
    
    This class wraps the backend documentation generator and adds
    CLI-specific features like progress tracking and error handling.
    """
    
    def __init__(
        self,
        repo_path: Path,
        output_dir: Path,
        config: Dict[str, Any],
        verbose: bool = False,
        generate_html: bool = False,
        commit_id: str = None,
        quiet: bool = False,
    ):
        """
        Initialize the CLI documentation generator.

        Args:
            repo_path: Repository path
            output_dir: Output directory
            config: LLM configuration
            verbose: Enable verbose output
            generate_html: Whether to generate HTML viewer
            commit_id: Git commit SHA for incremental update tracking
            quiet: Suppress INFO logs (only WARNING+ on console); verbose wins
        """
        self.repo_path = repo_path
        self.output_dir = output_dir
        self.config = config
        self.verbose = verbose
        self.quiet = quiet
        self.generate_html = generate_html
        self.commit_id = commit_id
        self.progress_tracker = ProgressTracker(total_stages=5, verbose=verbose)
        self.job = DocumentationJob()

        self.api_keys = config.get('api_keys', '') or ''
        self.concurrency = config.get('concurrency', 0) or 0
        self.disable_proxy = config.get('disable_proxy', True)
        self.cache_dir = config.get('cache_dir', '.codewiki_cache')
        self.resume = config.get('resume', True)
        self.model_context_window = config.get('model_context_window', 0)
        self.llm_timeout = config.get('llm_timeout', 1200)
        self.llm_max_retries = config.get('llm_max_retries', 10)
        self.llm_retry_interval = config.get('llm_retry_interval', 60)
        self.analysis_mode = config.get('analysis_mode', 'standard')
        self.fast_batch_size = config.get('fast_batch_size', 8)
        self.checkpoint = None
        self.key_pool = None
        # The OpenAI client requires a single API key (not comma-separated).
        # For multi-key configs, use the first key for client auth; the
        # ApiKeyPool handles round-robin distribution during concurrent calls.
        raw_single_key = config.get('api_key', '') or ''
        self.effective_first_key = (
            raw_single_key.split(',')[0].strip()
            if raw_single_key
            else (self.api_keys.split(',')[0].strip() if self.api_keys else '')
        )

        # Setup job metadata
        self.job.repository_path = str(repo_path)
        self.job.repository_name = repo_path.name
        self.job.output_directory = str(output_dir)
        self.job.llm_config = LLMConfig(
            main_model=config.get('main_model', ''),
            cluster_model=config.get('cluster_model', ''),
            base_url=config.get('base_url', '')
        )

        # Configure backend logging
        self._configure_backend_logging()

        if self.resume:
            try:
                from codewiki.src.be.checkpoint import CheckpointManager
                self.checkpoint = CheckpointManager(
                    repo_path=str(repo_path),
                    cache_root=self.cache_dir,
                )
                self.checkpoint.load_or_create()
            except ImportError:
                self.checkpoint = None

        keys = [k.strip() for k in self.api_keys.split(',') if k.strip()] if self.api_keys else []
        if len(keys) > 1:
            try:
                from codewiki.src.be.key_pool import ApiKeyPool
                self.key_pool = ApiKeyPool(keys)
                backend_logger = logging.getLogger('codewiki.src.be')
                backend_logger.info(
                    f"[KeyPool] Initialized with {len(keys)} keys "
                    f"(concurrency={self.concurrency or len(keys)})"
                )
            except ImportError:
                self.key_pool = None
    
    def _configure_backend_logging(self):
        """Configure backend logger for CLI use with colored output and file logging."""
        from codewiki.src.be.dependency_analyzer.utils.logging_config import ColoredFormatter, _build_file_handler
        
        # Get backend logger (parent of all backend modules)
        backend_logger = logging.getLogger('codewiki.src.be')
        
        # Remove existing handlers to avoid duplicates
        backend_logger.handlers.clear()
        
        # Determine effective log levels.
        # Default: INFO on console so codewiki's own logs print out of the box.
        # verbose → DEBUG; quiet → WARNING (verbose takes precedence).
        if self.verbose:
            console_level = logging.DEBUG
        elif self.quiet:
            console_level = logging.WARNING
        else:
            console_level = logging.INFO
        
        # Create console handler with formatting
        console_handler = logging.StreamHandler(sys.stdout if self.verbose else sys.stderr)
        console_handler.setLevel(console_level)
        colored_formatter = ColoredFormatter()
        console_handler.setFormatter(colored_formatter)
        backend_logger.addHandler(console_handler)
        
        # Add file handler for persistent logging to /usr/log/codewiki/
        # The file handler captures ALL levels (DEBUG+) regardless of verbose mode,
        # so full diagnostic data is always available even in non-verbose runs.
        file_handler = _build_file_handler()
        if file_handler is not None:
            # Use DEBUG level on file handler so everything gets logged to disk
            file_handler.setLevel(logging.DEBUG)
            backend_logger.addHandler(file_handler)
        
        # Set the logger's overall level to the *lower* of console and file
        # so that the file handler can capture DEBUG even when console shows WARNING.
        backend_logger.setLevel(min(console_level, logging.DEBUG))
        
        # Prevent propagation to root logger to avoid duplicate messages
        backend_logger.propagate = False
    
    def generate(self) -> DocumentationJob:
        """
        Generate documentation with progress tracking.
        
        Returns:
            Completed DocumentationJob
            
        Raises:
            APIError: If LLM API call fails
        """
        self.job.start()
        start_time = time.time()
        
        try:
            # Set CLI context for backend
            set_cli_context(True)

            # Create backend config with CLI settings.  Forward newer multi-key
            # / checkpoint params only when the backend Config.from_cli accepts
            # them — keeps us compatible across the parallel backend rollout.
            import inspect as _inspect
            backend_params = _inspect.signature(BackendConfig.from_cli).parameters
            extra_kwargs = {}
            for name, value in (
                ('api_keys', self.api_keys),
                ('concurrency', self.concurrency),
                ('disable_proxy', self.disable_proxy),
                ('cache_dir', self.cache_dir),
                ('resume', self.resume),
                ('model_context_window', self.model_context_window),
                ('llm_timeout', self.llm_timeout),
                ('llm_max_retries', self.llm_max_retries),
                ('llm_retry_interval', self.llm_retry_interval),
                ('analysis_mode', self.analysis_mode),
                ('fast_batch_size', self.fast_batch_size),
            ):
                if name in backend_params:
                    extra_kwargs[name] = value

            backend_config = BackendConfig.from_cli(
                repo_path=str(self.repo_path),
                output_dir=str(self.output_dir),
                llm_base_url=self.config.get('base_url'),
                llm_api_key=self.effective_first_key,
                main_model=self.config.get('main_model'),
                cluster_model=self.config.get('cluster_model'),
                fallback_model=self.config.get('fallback_model'),
                provider=self.config.get('provider', 'openai-compatible'),
                aws_region=self.config.get('aws_region', 'us-east-1'),
                max_tokens=self.config.get('max_tokens', 32768),
                max_token_per_module=self.config.get('max_token_per_module', 36369),
                max_token_per_leaf_module=self.config.get('max_token_per_leaf_module', 16000),
                max_depth=self.config.get('max_depth', 2),
                agent_instructions=self.config.get('agent_instructions'),
                **extra_kwargs,
            )

            # Run backend documentation generation
            asyncio.run(self._run_backend_generation(backend_config))
            
            # Stage 4: HTML Generation (optional)
            if self.generate_html:
                self._run_html_generation()
            
            # Stage 5: Finalization (metadata already created by backend)
            self._finalize_job()
            
            # Complete job
            generation_time = time.time() - start_time
            self.job.complete()
            
            return self.job
            
        except APIError as e:
            self.job.fail(str(e))
            raise
        except Exception as e:
            self.job.fail(str(e))
            raise
    
    async def _run_backend_generation(self, backend_config: BackendConfig):
        """Run the backend documentation generation with progress tracking."""
        
        # Stage 1: Dependency Analysis
        self.progress_tracker.start_stage(1, "Dependency Analysis")
        if self.verbose:
            self.progress_tracker.update_stage(0.2, "Initializing dependency analyzer...")
        
        # Create documentation generator
        doc_generator = DocumentationGenerator(backend_config, commit_id=self.commit_id, ckpt=self.checkpoint, key_pool=self.key_pool)
        
        # Checkpoint: resume from completed analysis phase
        components = None
        leaf_nodes = None
        analysis_done = False
        
        if self.checkpoint is not None and self.checkpoint.is_done("dep_analysis"):
            try:
                artifacts_path = self.checkpoint.state.analysis_artifacts_path
                if artifacts_path and os.path.exists(artifacts_path):
                    from codewiki.src.be.dependency_analyzer.utils.serialization import load_analysis_artifacts
                    components, leaf_nodes = load_analysis_artifacts(artifacts_path)
                    if components is not None and leaf_nodes is not None:
                        analysis_done = True
                        backend_logger = logging.getLogger('codewiki.src.be')
                        backend_logger.info(
                            "[Resume] Skipped dependency analysis — loaded %d components, %d leaf nodes from checkpoint",
                            len(components), len(leaf_nodes),
                        )
                        if self.verbose:
                            self.progress_tracker.update_stage(0.9, f"Resumed: {len(components)} components, {len(leaf_nodes)} leaf nodes (analysis skipped)")
            except Exception as e:
                backend_logger = logging.getLogger('codewiki.src.be')
                backend_logger.warning("[Resume] Failed to load analysis checkpoint: %s; re-running analysis", e)
                analysis_done = False
        
        if not analysis_done:
            if self.verbose:
                self.progress_tracker.update_stage(0.5, "Parsing source files...")
            
            try:
                components, leaf_nodes = doc_generator.graph_builder.build_dependency_graph()
                self.job.statistics.total_files_analyzed = len(components)
                self.job.statistics.leaf_nodes = len(leaf_nodes)

                if self.verbose:
                    self.progress_tracker.update_stage(0.8, f"Analyzed {len(components)} files, found {len(leaf_nodes)} leaf nodes")
                    for comp_name in sorted(components.keys())[:20]:
                        self.progress_tracker.update_stage(0.9, f"  File: {comp_name}")
                    if len(components) > 20:
                        self.progress_tracker.update_stage(0.9, f"  ... and {len(components) - 20} more files")
                
                # Save analysis artifacts for resume
                if self.checkpoint is not None:
                    from codewiki.src.be.dependency_analyzer.utils.serialization import save_analysis_artifacts
                    working_dir = str(self.output_dir.absolute())
                    from codewiki.src.utils import file_manager
                    file_manager.ensure_directory(working_dir)
                    artifacts_path = os.path.join(working_dir, "analysis_artifacts.json")
                    save_analysis_artifacts(components, leaf_nodes, artifacts_path)
                    self.checkpoint.set_stage_artifact("analysis_artifacts_path", artifacts_path)
                    self.checkpoint.mark_done("dep_analysis")
            except Exception as e:
                raise APIError(f"Dependency analysis failed: {e}")
        
        # Free source_code from components to reduce memory footprint
        # After analysis, source_code is no longer needed in-memory — it will
        # be read from disk on demand by read_code_components_tool and
        # format_user_prompt during the documentation generation phase.
        if components is not None:
            freed_bytes = 0
            for node in components.values():
                if node.source_code is not None:
                    freed_bytes += len(node.source_code.encode("utf-8", errors="ignore"))
                    node.source_code = None
            if freed_bytes > 0:
                backend_logger = logging.getLogger('codewiki.src.be')
                backend_logger.info(
                    "[Memory] Freed %d bytes by stripping source_code from %d Node objects",
                    freed_bytes, len(components),
                )
                gc.collect()
        
        self.progress_tracker.complete_stage()
        
        # Stage 2: Module Clustering
        self.progress_tracker.start_stage(2, "Module Clustering")
        if self.verbose:
            self.progress_tracker.update_stage(0.5, "Clustering modules with LLM...")
        
        # Import clustering function
        from codewiki.src.be.cluster_modules import (
            cluster_modules,
            get_clustering_input_token_count,
        )
        from codewiki.src.utils import file_manager
        from codewiki.src.config import FIRST_MODULE_TREE_FILENAME, MODULE_TREE_FILENAME

        working_dir = str(self.output_dir.absolute())
        file_manager.ensure_directory(working_dir)
        first_module_tree_path = os.path.join(working_dir, FIRST_MODULE_TREE_FILENAME)
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)

        clustering_done = False
        
        # Checkpoint: resume from completed clustering phase
        if self.checkpoint is not None and self.checkpoint.is_done("module_clustering"):
            if os.path.exists(first_module_tree_path):
                clustering_done = True
                backend_logger = logging.getLogger('codewiki.src.be')
                backend_logger.info("[Resume] Skipped module clustering — loaded cached module tree")
                if self.verbose:
                    self.progress_tracker.update_stage(0.9, "Resumed: module clustering skipped (cached tree found)")

        try:
            if clustering_done or os.path.exists(first_module_tree_path):
                module_tree = file_manager.load_json(first_module_tree_path)
                if self.verbose and not clustering_done:
                    self.progress_tracker.update_stage(0.5, "Loaded cached module tree")
            else:
                if self.verbose:
                    clustering_tokens = get_clustering_input_token_count(
                        leaf_nodes, components
                    )
                    self.progress_tracker.update_stage(
                        0.3,
                        (
                            f"Preparing {len(leaf_nodes)} leaf nodes for clustering "
                            f"({clustering_tokens} tokens, threshold "
                            f"{backend_config.max_token_per_module})"
                        ),
                    )
                    if clustering_tokens <= backend_config.max_token_per_module:
                        self.progress_tracker.update_stage(
                            0.4,
                            "Skipping LLM clustering; selected leaf nodes fit within the module token threshold",
                        )
                    else:
                        self.progress_tracker.update_stage(
                            0.4,
                            "Clustering modules with LLM...",
                        )
                cluster_model = backend_config.cluster_model or None
                module_tree = cluster_modules(
                    leaf_nodes,
                    components,
                    backend_config,
                    completer=lambda p: doc_generator.backend.complete(p, model=cluster_model),
                )
                # Only freshly clustered trees are deduped: renaming a cached
                # key whose .md already exists would orphan the doc.
                from codewiki.src.be.module_naming import dedupe_module_tree_names
                module_tree = dedupe_module_tree_names(module_tree)
                file_manager.save_json(module_tree, first_module_tree_path)
                
                if self.checkpoint is not None:
                    self.checkpoint.mark_done("module_clustering")

            file_manager.save_json(module_tree, module_tree_path)
            self.job.module_count = len(module_tree)

            if self.verbose:
                if len(module_tree) == 0:
                    self.progress_tracker.update_stage(
                        1.0,
                        "Created 0 modules; continuing in whole-repository documentation mode",
                    )
                else:
                    self.progress_tracker.update_stage(
                        1.0,
                        f"Created {len(module_tree)} modules",
                    )
                for mod_name in sorted(module_tree.keys()):
                    file_count = len(module_tree[mod_name]) if isinstance(module_tree[mod_name], list) else "?"
                    self.progress_tracker.update_stage(1.0, f"  Module: {mod_name} ({file_count} files)")
        except Exception as e:
            raise APIError(f"Module clustering failed: {e}")
        
        self.progress_tracker.complete_stage()
        
        # Stage 3: Documentation Generation
        self.progress_tracker.start_stage(3, "Documentation Generation")
        if self.verbose:
            self.progress_tracker.update_stage(0.1, "Generating module documentation...")
        
        try:
            if self.verbose:
                self.progress_tracker.update_stage(0.2, f"Generating documentation for {self.job.module_count} modules...")

            # Run the actual documentation generation
            await doc_generator.generate_module_documentation(components, leaf_nodes)

            if self.verbose:
                self.progress_tracker.update_stage(0.9, "Creating repository overview...")
            
            # Create metadata
            doc_generator.create_documentation_metadata(working_dir, components, len(leaf_nodes))
            
            # Collect generated files
            for file_path in os.listdir(working_dir):
                if file_path.endswith('.md') or file_path.endswith('.json'):
                    self.job.files_generated.append(file_path)

        except Exception as e:
            raise APIError(f"Documentation generation failed: {e}")

        # Reconcile the final module tree against the docs on disk so name
        # collisions or failed sub-agents can't pass as success (issue #76).
        # Outside the try/except above so it is not reported as an API error.
        missing_docs = doc_generator.validate_generated_docs(working_dir)
        if missing_docs:
            raise IncompleteGenerationError(
                "Documentation generation finished but these module docs are missing: "
                + ", ".join(f"{name}.md" for name in missing_docs),
                missing_modules=missing_docs,
            )

        self.progress_tracker.complete_stage()

        if self.checkpoint is not None:
            try:
                prog = self.checkpoint.progress()
                backend_logger = logging.getLogger('codewiki.src.be')
                backend_logger.info(
                    f"[Checkpoint] Final progress: {prog.get('done', 0)}/{prog.get('total', 0)} "
                    f"completed ({prog.get('pct', 0)}%), failed={prog.get('failed', 0)}"
                )
            except Exception:
                pass
    
    def _run_html_generation(self):
        """Run HTML generation stage."""
        self.progress_tracker.start_stage(4, "HTML Generation")
        
        from codewiki.cli.html_generator import HTMLGenerator
        
        # Generate HTML
        html_generator = HTMLGenerator()
        
        if self.verbose:
            self.progress_tracker.update_stage(0.3, "Loading module tree and metadata...")
        
        repo_info = html_generator.detect_repository_info(self.repo_path)
        
        # Generate HTML with auto-loading of module_tree and metadata from docs_dir
        output_path = self.output_dir / "index.html"
        html_generator.generate(
            output_path=output_path,
            title=repo_info['name'],
            repository_url=repo_info['url'],
            github_pages_url=repo_info['github_pages_url'],
            docs_dir=self.output_dir  # Auto-load module_tree and metadata from here
        )
        
        self.job.files_generated.append("index.html")
        
        if self.verbose:
            self.progress_tracker.update_stage(1.0, "Generated index.html")
        
        self.progress_tracker.complete_stage()
    
    def _finalize_job(self):
        """Finalize the job (metadata already created by backend)."""
        # Just verify metadata exists
        metadata_path = self.output_dir / "metadata.json"
        if not metadata_path.exists():
            # Create our own if backend didn't
            with open(metadata_path, 'w') as f:
                f.write(self.job.to_json())
