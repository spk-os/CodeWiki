from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import argparse
import os
import sys
from dotenv import load_dotenv
load_dotenv()

# Constants
OUTPUT_BASE_DIR = 'output'
DEPENDENCY_GRAPHS_DIR = 'dependency_graphs'
DOCS_DIR = 'docs'
FIRST_MODULE_TREE_FILENAME = 'first_module_tree.json'
MODULE_TREE_FILENAME = 'module_tree.json'
OVERVIEW_FILENAME = 'overview.md'
MAX_DEPTH = 2
# Default max token settings
DEFAULT_MAX_TOKENS = 32_768
DEFAULT_MAX_TOKEN_PER_MODULE = 36_369
DEFAULT_MAX_TOKEN_PER_LEAF_MODULE = 16_000
DEFAULT_LLM_TIMEOUT = 1200   # 20 minutes
DEFAULT_LLM_MAX_RETRIES = 10
DEFAULT_LLM_RETRY_INTERVAL = 60  # 1 minute
# Legacy constants (for backward compatibility)
MAX_TOKEN_PER_MODULE = DEFAULT_MAX_TOKEN_PER_MODULE
MAX_TOKEN_PER_LEAF_MODULE = DEFAULT_MAX_TOKEN_PER_LEAF_MODULE

# CLI context detection
_CLI_CONTEXT = False

def set_cli_context(enabled: bool = True):
    """Set whether we're running in CLI context (vs web app)."""
    global _CLI_CONTEXT
    _CLI_CONTEXT = enabled

def is_cli_context() -> bool:
    """Check if running in CLI context."""
    return _CLI_CONTEXT

# LLM services
# In CLI mode, these will be loaded from ~/.codewiki/config.json + keyring
# In web app mode, use environment variables
MAIN_MODEL = os.getenv('MAIN_MODEL', 'claude-sonnet-4')
FALLBACK_MODEL_1 = os.getenv('FALLBACK_MODEL_1', 'glm-4p5')
CLUSTER_MODEL = os.getenv('CLUSTER_MODEL', MAIN_MODEL)
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'http://0.0.0.0:4000/')
LLM_API_KEY = os.getenv('LLM_API_KEY', 'sk-1234')

# Atlas Cloud default endpoint (OpenAI-compatible). Used to auto-fill the base URL
# when the user selects the `atlas-cloud` provider without passing --base-url.
ATLAS_CLOUD_BASE_URL = "https://api.atlascloud.ai/v1"

@dataclass
class Config:
    """Configuration class for CodeWiki."""
    repo_path: str
    output_dir: str
    dependency_graph_dir: str
    docs_dir: str
    max_depth: int
    # LLM configuration
    llm_base_url: str
    llm_api_key: str
    main_model: str
    cluster_model: str
    fallback_model: str = FALLBACK_MODEL_1
    # Provider configuration
    provider: str = "openai-compatible"  # openai-compatible, atlas-cloud, anthropic, bedrock, azure-openai
    aws_region: str = "us-east-1"
    api_version: str = "2024-12-01-preview"  # Azure OpenAI API version
    azure_deployment: str = ""  # Azure OpenAI deployment name
    # Max token settings
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_token_per_module: int = DEFAULT_MAX_TOKEN_PER_MODULE
    max_token_per_leaf_module: int = DEFAULT_MAX_TOKEN_PER_LEAF_MODULE
    # Agent instructions for customization
    agent_instructions: Optional[Dict[str, Any]] = None
    # Multi-key concurrency / proxy / checkpoint
    api_keys: str = ""
    concurrency: int = 0
    disable_proxy: bool = True
    cache_dir: str = ".codewiki_cache"
    resume: bool = True
    # Model context window limit (in tokens). Prompts exceeding this limit
    # are automatically truncated.  Defaults to 0 (auto-detect from model
    # name), set explicitly for proxy setups where the actual model's
    # context limit differs from the model name's known limit.
    model_context_window: int = 0
    # LLM call resilience
    llm_timeout: int = DEFAULT_LLM_TIMEOUT
    llm_max_retries: int = DEFAULT_LLM_MAX_RETRIES
    llm_retry_interval: int = DEFAULT_LLM_RETRY_INTERVAL
    # Analysis mode: "standard" (default), "coarse" (fast, shallow), "fine" (detailed, deep), "fast" (batch, fewest LLM calls)
    analysis_mode: str = "standard"
    # Fast mode: how many leaf modules to bundle into a single LLM `complete()`
    # call.  Larger = fewer calls (LLM is the bottleneck) but lower per-module
    # fidelity.  Ignored outside fast mode.
    fast_batch_size: int = 8
    # Condensed view (A): in coarse/fast modes the leaf prompt replaces full
    # file source with a signature + call-graph + L0-summary card, so the big
    # model no longer ingests raw source.  standard/fine keep full source.
    condensed_view: bool = False
    # L0 file-summary layer (C): a small model digests each file into 1-3
    # sentences once, cached by file path + source hash.  Source digestion
    # moves off the big model (the bottleneck).  Serves fast mode by default.
    l0_summary_enabled: bool = False
    l0_model: Optional[str] = None
    l0_batch_size: int = 8

    def __post_init__(self):
        # When multi-key api_keys is configured but llm_api_key is empty or
        # contains the full comma-separated string, populate llm_api_key with
        # the first individual key so OpenAI/litellm clients get a valid
        # single key for authentication.
        keys = [k.strip() for k in (self.api_keys or "").split(",") if k.strip()]
        if keys:
            if not self.llm_api_key or "," in self.llm_api_key:
                self.llm_api_key = keys[0]

    @property
    def effective_keys(self) -> List[str]:
        """Return de-duplicated, non-empty API keys.

        Prefers ``api_keys`` (comma-separated) over the single ``llm_api_key``.
        """
        raw = self.api_keys or ""
        parts = [p.strip() for p in raw.split(",") if p and p.strip()]
        if not parts and self.llm_api_key:
            parts = [self.llm_api_key.strip()]
        seen = set()
        result: List[str] = []
        for p in parts:
            if p and p not in seen:
                seen.add(p)
                result.append(p)
        return result

    @property
    def effective_concurrency(self) -> int:
        if self.concurrency and self.concurrency > 0:
            return self.concurrency
        return max(1, len(self.effective_keys))

    @property
    def effective_max_depth(self) -> int:
        if self.analysis_mode in ("coarse", "fast"):
            return 1
        if self.analysis_mode == "fine":
            return max(self.max_depth, 3)
        return self.max_depth

    @property
    def effective_condensed_view(self) -> bool:
        """Whether leaf prompts use the signature+summary card instead of full source."""
        return self.analysis_mode in ("coarse", "fast") or self.condensed_view

    @property
    def effective_l0_enabled(self) -> bool:
        """Whether the L0 file-summary layer runs before leaf generation."""
        return self.analysis_mode == "fast" or self.l0_summary_enabled

    @property
    def effective_l0_model(self) -> str:
        """Small model used to digest files into L0 summaries."""
        return self.l0_model or self.cluster_model or self.main_model

    MODEL_CONTEXT_MAP = {
        "deepseek-v4-flash-free": 1048565,
        "deepseek-ai/deepseek-v4-pro": 1048565,
        "deepseek-chat": 128000,
        "deepseek-reasoner": 128000,
        "claude-sonnet-4": 200000,
        "claude-opus-4": 200000,
        "gpt-4": 128000,
        "gpt-4-turbo": 128000,
        "gpt-4o": 128000,
        "gpt-4o-mini": 128000,
    }

    @property
    def effective_context_window(self) -> int:
        """Return the model context window in tokens.

        Uses ``model_context_window`` if explicitly set (> 0), otherwise
        looks up the model name in the known context map, falling back to
        128000 (a conservative default) if not found.
        """
        if self.model_context_window and self.model_context_window > 0:
            return self.model_context_window
        model = self.main_model or ""
        for known_name, ctx in self.MODEL_CONTEXT_MAP.items():
            if known_name in model:
                return ctx
        return 128000

    @property
    def include_patterns(self) -> Optional[List[str]]:
        """Get file include patterns from agent instructions."""
        if self.agent_instructions:
            return self.agent_instructions.get('include_patterns')
        return None
    
    @property
    def exclude_patterns(self) -> Optional[List[str]]:
        """Get file exclude patterns from agent instructions."""
        if self.agent_instructions:
            return self.agent_instructions.get('exclude_patterns')
        return None
    
    @property
    def focus_modules(self) -> Optional[List[str]]:
        """Get focus modules from agent instructions."""
        if self.agent_instructions:
            return self.agent_instructions.get('focus_modules')
        return None
    
    @property
    def doc_type(self) -> Optional[str]:
        """Get documentation type from agent instructions."""
        if self.agent_instructions:
            return self.agent_instructions.get('doc_type')
        return None
    
    @property
    def custom_instructions(self) -> Optional[str]:
        """Get custom instructions from agent instructions."""
        if self.agent_instructions:
            return self.agent_instructions.get('custom_instructions')
        return None
    
    def get_prompt_addition(self) -> str:
        """Generate prompt additions based on agent instructions."""
        if not self.agent_instructions:
            return ""
        
        additions = []
        
        if self.doc_type:
            doc_type_instructions = {
                'api': "Focus on API documentation: endpoints, parameters, return types, and usage examples.",
                'architecture': "Focus on architecture documentation: system design, component relationships, and data flow.",
                'user-guide': "Focus on user guide documentation: how to use features, step-by-step tutorials.",
                'developer': "Focus on developer documentation: code structure, contribution guidelines, and implementation details.",
            }
            if self.doc_type.lower() in doc_type_instructions:
                additions.append(doc_type_instructions[self.doc_type.lower()])
            else:
                additions.append(f"Focus on generating {self.doc_type} documentation.")
        
        if self.focus_modules:
            additions.append(f"Pay special attention to and provide more detailed documentation for these modules: {', '.join(self.focus_modules)}")
        
        if self.custom_instructions:
            additions.append(f"Additional instructions: {self.custom_instructions}")
        
        return "\n".join(additions) if additions else ""
    
    @classmethod
    def from_args(cls, args: argparse.Namespace) -> 'Config':
        """Create configuration from parsed arguments."""
        repo_name = os.path.basename(os.path.normpath(args.repo_path))
        sanitized_repo_name = ''.join(c if c.isalnum() else '_' for c in repo_name)
        
        return cls(
            repo_path=args.repo_path,
            output_dir=OUTPUT_BASE_DIR,
            dependency_graph_dir=os.path.join(OUTPUT_BASE_DIR, DEPENDENCY_GRAPHS_DIR),
            docs_dir=os.path.join(OUTPUT_BASE_DIR, DOCS_DIR, f"{sanitized_repo_name}-docs"),
            max_depth=MAX_DEPTH,
            llm_base_url=LLM_BASE_URL,
            llm_api_key=LLM_API_KEY,
            main_model=MAIN_MODEL,
            cluster_model=CLUSTER_MODEL,
            fallback_model=FALLBACK_MODEL_1
        )
    
    @classmethod
    def from_cli(
        cls,
        repo_path: str,
        output_dir: str,
        llm_base_url: str,
        llm_api_key: str,
        main_model: str,
        cluster_model: str,
        fallback_model: str = FALLBACK_MODEL_1,
        provider: str = "openai-compatible",
        aws_region: str = "us-east-1",
        api_version: str = "2024-12-01-preview",
        azure_deployment: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_token_per_module: int = DEFAULT_MAX_TOKEN_PER_MODULE,
        max_token_per_leaf_module: int = DEFAULT_MAX_TOKEN_PER_LEAF_MODULE,
        max_depth: int = MAX_DEPTH,
        agent_instructions: Optional[Dict[str, Any]] = None,
        api_keys: str = "",
        concurrency: int = 0,
        disable_proxy: bool = True,
        cache_dir: str = ".codewiki_cache",
        resume: bool = True,
        model_context_window: int = 0,
        llm_timeout: int = DEFAULT_LLM_TIMEOUT,
        llm_max_retries: int = DEFAULT_LLM_MAX_RETRIES,
        llm_retry_interval: int = DEFAULT_LLM_RETRY_INTERVAL,
        analysis_mode: str = "standard",
        fast_batch_size: int = 8,
        condensed_view: bool = False,
        l0_summary_enabled: bool = False,
        l0_model: Optional[str] = None,
        l0_batch_size: int = 8,
    ) -> 'Config':
        """
        Create configuration for CLI context.

        Args:
            repo_path: Repository path
            output_dir: Output directory for generated docs
            llm_base_url: LLM API base URL
            llm_api_key: LLM API key
            main_model: Primary model
            cluster_model: Clustering model
            fallback_model: Fallback model
            provider: LLM provider type (openai-compatible, atlas-cloud, anthropic, bedrock, azure-openai)
            aws_region: AWS region for Bedrock provider
            api_version: Azure OpenAI API version
            azure_deployment: Azure OpenAI deployment name
            max_tokens: Maximum tokens for LLM response
            max_token_per_module: Maximum tokens per module for clustering
            max_token_per_leaf_module: Maximum tokens per leaf module
            max_depth: Maximum depth for hierarchical decomposition
            agent_instructions: Custom agent instructions dict

        Returns:
            Config instance
        """
        repo_name = os.path.basename(os.path.normpath(repo_path))
        base_output_dir = os.path.join(output_dir, "temp")

        return cls(
            repo_path=repo_path,
            output_dir=base_output_dir,
            dependency_graph_dir=os.path.join(base_output_dir, DEPENDENCY_GRAPHS_DIR),
            docs_dir=output_dir,
            max_depth=max_depth,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            main_model=main_model,
            cluster_model=cluster_model,
            fallback_model=fallback_model,
            provider=provider,
            aws_region=aws_region,
            api_version=api_version,
            azure_deployment=azure_deployment,
            max_tokens=max_tokens,
            max_token_per_module=max_token_per_module,
            max_token_per_leaf_module=max_token_per_leaf_module,
            agent_instructions=agent_instructions,
            api_keys=api_keys,
            concurrency=concurrency,
            disable_proxy=disable_proxy,
            cache_dir=cache_dir,
            resume=resume,
            model_context_window=model_context_window,
            llm_timeout=llm_timeout,
            llm_max_retries=llm_max_retries,
            llm_retry_interval=llm_retry_interval,
            analysis_mode=analysis_mode,
            fast_batch_size=fast_batch_size,
            condensed_view=condensed_view,
            l0_summary_enabled=l0_summary_enabled,
            l0_model=l0_model,
            l0_batch_size=l0_batch_size,
        )