"""PydanticAIBackend — the existing API-key based path.

This backend is a thin adapter over :func:`call_llm` and the pydantic-ai
``Agent`` machinery.  Behaviour is preserved exactly; this file only
repackages it behind the :class:`LLMBackend` interface so the rest of
CodeWiki can be backend-agnostic.
"""

from __future__ import annotations

import logging
import os
import threading
import traceback
from typing import Any, Dict, List, Optional

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from codewiki.src.be.agent_tools.deps import CodeWikiDeps
from codewiki.src.be.agent_tools.generate_sub_module_documentations import (
    generate_sub_module_documentation_tool,
)
from codewiki.src.be.agent_tools.read_code_components import read_code_components_tool
from codewiki.src.be.agent_tools.str_replace_editor import str_replace_editor_tool
from codewiki.src.be.backend import LLMBackend
from codewiki.src.be.checkpoint import CheckpointManager
from codewiki.src.be.dependency_analyzer.models.core import Node
from codewiki.src.be.llm_services import call_llm, create_fallback_models, _build_model_settings
from codewiki.src.be.prompt_template import (
    format_leaf_system_prompt,
    format_system_prompt,
    format_user_prompt,
)
from codewiki.src.be.utils import is_complex_module, merge_module_tree
from codewiki.src.config import MODULE_TREE_FILENAME, OVERVIEW_FILENAME, Config
from codewiki.src.utils import file_manager

logger = logging.getLogger(__name__)


class PydanticAIBackend(LLMBackend):
    """API-key based backend using pydantic-ai + openai/litellm clients."""

    def __init__(self, config: Config, ckpt: CheckpointManager | None = None, key_pool=None) -> None:
        self._config = config
        self._fallback_models = create_fallback_models(config)
        self._custom_instructions = config.get_prompt_addition()
        self._ckpt = ckpt
        # Multi-key concurrency: when a key pool is configured, each concurrent
        # module agent acquires a key from the pool and uses a per-key model
        # (built lazily and cached).  Quality is unchanged — same models, same
        # prompts, same temperature; only the API key varies per call.
        self._key_pool = key_pool
        self._models_by_key: Dict[str, Any] = {}
        self._models_lock = threading.Lock()

    def _models_for_key(self, api_key: Optional[str]):
        """Return the FallbackModel to use for *api_key*.

        Builds and caches one ``FallbackModel`` per distinct key (each gets its
        own OpenAIProvider / httpx client) so concurrent agents on different
        keys truly fire in parallel.  Falls back to the shared single-key
        model when no key override is given.
        """
        if not api_key or self._key_pool is None:
            return self._fallback_models
        with self._models_lock:
            models = self._models_by_key.get(api_key)
            if models is None:
                models = create_fallback_models(self._config, api_key=api_key)
                self._models_by_key[api_key] = models
            return models

    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        api_key: Optional[str] = None,
    ) -> str:
        effective_model = model or self._config.main_model
        if self._ckpt is not None:
            cached = self._ckpt.get_llm_cache(prompt, effective_model)
            if cached is not None:
                logger.info("[Resume] LLM cache hit for model=%s", effective_model)
                return cached

        response = call_llm(
            prompt, self._config, model=model, temperature=temperature, api_key=api_key
        )

        if self._ckpt is not None and response:
            self._ckpt.save_llm_cache(prompt, effective_model, response)
        return response

    async def run_module_agent(
        self,
        module_name: str,
        components: Dict[str, Node],
        core_component_ids: List[str],
        module_path: List[str],
        working_dir: str,
        tree_lock: Optional[threading.RLock] = None,
        api_key: Optional[str] = None,
        l0_summaries: Optional[dict] = None,
        reverse_call_index: Optional[dict] = None,
    ) -> Dict[str, Any]:
        config = self._config
        self._tree_lock = tree_lock
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        module_tree = file_manager.load_json(module_tree_path)

        # The overview check only applies in whole-repo mode (empty module_path).
        # For individual leaf/parent modules, overview.md existing is expected
        # (it was generated in a previous run) and must NOT short-circuit the
        # module's own documentation generation.
        if not module_path:
            overview_docs_path = os.path.join(working_dir, OVERVIEW_FILENAME)
            if os.path.exists(overview_docs_path):
                logger.info("✓ Overview docs already exists at %s", overview_docs_path)
                return module_tree
        docs_path = os.path.join(working_dir, f"{module_name}.md")
        if os.path.exists(docs_path):
            logger.info("✓ Module docs already exists at %s", docs_path)
            return module_tree

        use_delegation = (
            config.analysis_mode not in ("coarse", "fast")
            and is_complex_module(components, core_component_ids)
        )
        # Pick the model set for this call's API key (per-key pool when
        # configured, else the shared single-key fallback models).
        models = self._models_for_key(api_key)
        if use_delegation:
            agent = Agent(
                models,
                name=module_name,
                deps_type=CodeWikiDeps,
                tools=[
                    read_code_components_tool,
                    str_replace_editor_tool,
                    generate_sub_module_documentation_tool,
                ],
                system_prompt=format_system_prompt(module_name, self._custom_instructions),
                model_settings=_build_model_settings(self._config, self._config.main_model),
            )
        else:
            agent = Agent(
                models,
                name=module_name,
                deps_type=CodeWikiDeps,
                tools=[read_code_components_tool, str_replace_editor_tool],
                system_prompt=format_leaf_system_prompt(module_name, self._custom_instructions),
                model_settings=_build_model_settings(self._config, self._config.main_model),
            )

        deps = CodeWikiDeps(
            absolute_docs_path=working_dir,
            absolute_repo_path=str(os.path.abspath(config.repo_path)),
            registry={},
            components=components,
            path_to_current_module=module_path,
            current_module_name=module_name,
            module_tree=module_tree,
            max_depth=config.effective_max_depth,
            current_depth=1,
            config=config,
            custom_instructions=self._custom_instructions,
            l0_summaries=l0_summaries,
            condensed_view=config.effective_condensed_view,
            reverse_call_index=reverse_call_index,
        )

        try:
            result = await agent.run(
                format_user_prompt(
                    module_name=module_name,
                    core_component_ids=core_component_ids,
                    components=components,
                    module_tree=deps.module_tree,
                    context_window=config.effective_context_window,
                    condensed=config.effective_condensed_view,
                    l0_summaries=l0_summaries,
                    reverse_call_index=reverse_call_index,
                ),
                deps=deps,
                usage_limits=UsageLimits(request_limit=None),
            )
            result_data = result.data if hasattr(result, 'data') else str(result)
            result_preview = (result_data[:500] + '...') if len(str(result_data)) > 500 else str(result_data)
            usage = result.usage if hasattr(result, 'usage') else None
            logger.info(
                "[Agent] Module=%s | Output length=%d | Usage=%s | Preview: %s",
                module_name, len(str(result_data)), usage, result_preview,
            )
            expected_md = os.path.join(working_dir, f"{module_name}.md")
            if not os.path.exists(expected_md):
                logger.warning(
                    "[Agent] Module %s: agent completed but %s was NOT created. "
                    "The agent may not have called str_replace_editor with command='create'.",
                    module_name, expected_md,
                )
            self._save_module_tree(deps.module_tree, module_tree_path, module_path)
            if self._ckpt is not None:
                module_key = "/".join(module_path) if module_path else module_name
                self._ckpt.mark_done(module_key)
            return deps.module_tree
        except Exception as e:
            logger.error("Error processing module %s: %s", module_name, e)
            logger.error("Traceback: %s", traceback.format_exc())
            if self._ckpt is not None:
                module_key = "/".join(module_path) if module_path else module_name
                self._ckpt.mark_failed(module_key, str(e))
            raise

    def _save_module_tree(
        self,
        agent_tree: Dict[str, Any],
        module_tree_path: str,
        module_path: List[str],
    ) -> None:
        """Persist module tree, merging with on-disk version under tree_lock."""
        lock = getattr(self, "_tree_lock", None)
        if lock is not None:
            with lock:
                latest = file_manager.load_json(module_tree_path)
                merged = merge_module_tree(latest, agent_tree, module_path)
                file_manager.save_json(merged, module_tree_path)
        else:
            file_manager.save_json(agent_tree, module_tree_path)
