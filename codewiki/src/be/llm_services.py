"""
LLM service factory for creating configured LLM clients.

Includes a compatibility layer for OpenAI-compatible API proxies that may
return slightly non-standard responses (e.g. choices[].index = None).

Supports multiple providers: openai-compatible, anthropic, bedrock, azure-openai.
"""
import logging
import os
from typing import Optional

import httpx
from openai.types import chat

from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.models.openai import OpenAIModelSettings
from pydantic_ai.models.fallback import FallbackModel
from openai import OpenAI, BadRequestError, RateLimitError, APIConnectionError, APIStatusError

from codewiki.src.config import Config, DEFAULT_LLM_TIMEOUT, DEFAULT_LLM_MAX_RETRIES, DEFAULT_LLM_RETRY_INTERVAL

logger = logging.getLogger(__name__)


_PROXY_ENV_VARS = [
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
    "NO_PROXY", "no_proxy",
]


class ProxyDisabledContext:
    """Context manager that temporarily clears proxy environment variables.

    Some HTTP clients (httpx, openai SDK) honor ``HTTP_PROXY`` / ``HTTPS_PROXY``
    env vars. When the user provides a direct LLM endpoint, an outer corporate
    proxy can break the connection. This context strips those variables for
    the duration of the call and restores them on exit.
    """

    def __init__(self) -> None:
        self._saved: dict = {}

    def _clear(self) -> None:
        for name in _PROXY_ENV_VARS:
            if name in os.environ:
                self._saved[name] = os.environ.pop(name)

    def _restore(self) -> None:
        for name, value in self._saved.items():
            os.environ[name] = value
        self._saved.clear()

    def __enter__(self) -> "ProxyDisabledContext":
        self._clear()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._restore()

    async def __aenter__(self) -> "ProxyDisabledContext":
        self._clear()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._restore()


def _build_proxyless_httpx_client(timeout: int = DEFAULT_LLM_TIMEOUT) -> httpx.Client:
    """Create an httpx.Client that ignores environment proxies."""
    return httpx.Client(trust_env=False, timeout=httpx.Timeout(float(timeout)))


def _should_use_max_completion_tokens(model_name: str, base_url: str) -> bool:
    """
    Determine whether to use max_completion_tokens instead of max_tokens.

    Newer OpenAI models (o1, o3, o4, gpt-4o, gpt-5, etc.) require
    max_completion_tokens. Anthropic and other providers still use max_tokens.
    """
    model_lower = model_name.lower()
    # OpenAI models that require max_completion_tokens
    new_openai_patterns = ("o1", "o3", "o4", "gpt-4o", "gpt-4-turbo", "gpt-5")
    if any(pattern in model_lower for pattern in new_openai_patterns):
        return True
    # If base_url points to OpenAI directly, newer models may need it
    if base_url and "api.openai.com" in base_url:
        return True
    return False


def _build_model_settings(config: Config, model_name: str) -> OpenAIModelSettings:
    """Build model settings with the correct token parameter."""
    if _should_use_max_completion_tokens(model_name, config.llm_base_url):
        return OpenAIModelSettings(
            temperature=0.0,
            max_completion_tokens=config.max_tokens
        )
    return OpenAIModelSettings(
        temperature=0.0,
        max_tokens=config.max_tokens
    )


def _get_litellm_model_name(model_name: str, provider: str) -> str:
    """
    Get the litellm-compatible model name for a given provider.

    For Bedrock, prefixes the model name with 'bedrock/' if not already prefixed.
    For Anthropic, prefixes with 'anthropic/' if not already prefixed.
    """
    if provider == "bedrock":
        if not model_name.startswith("bedrock/"):
            return f"bedrock/{model_name}"
    elif provider == "anthropic":
        if not model_name.startswith("anthropic/"):
            return f"anthropic/{model_name}"
    return model_name


class CompatibleOpenAIModel(OpenAIModel):
    """OpenAIModel subclass that patches non-standard API proxy responses.

    Some OpenAI-compatible proxies return responses with fields like
    choices[].index set to None instead of an integer. This subclass
    fixes those fields before pydantic validation runs.
    """

    def _validate_completion(self, response: chat.ChatCompletion) -> chat.ChatCompletion:
        # Patch choices[].index: None -> sequential integer (0, 1, 2, ...)
        if response.choices:
            for i, choice in enumerate(response.choices):
                if choice.index is None:
                    choice.index = i
        return super()._validate_completion(response)


def _create_litellm_openai_client(config: Config) -> OpenAI:
    """
    Create an OpenAI-compatible client backed by litellm's proxy.

    litellm translates OpenAI API calls to Bedrock, Anthropic, etc.
    """
    import litellm
    # Configure litellm for the provider
    if config.provider == "bedrock":
        os.environ.setdefault("AWS_DEFAULT_REGION", config.aws_region)
        os.environ.setdefault("AWS_REGION_NAME", config.aws_region)

    with ProxyDisabledContext():
        llm_timeout = getattr(config, 'llm_timeout', DEFAULT_LLM_TIMEOUT)
        return OpenAI(
            api_key=config.llm_api_key or "not-needed-for-bedrock",
            base_url=config.llm_base_url or "https://api.openai.com/v1",
            http_client=_build_proxyless_httpx_client(timeout=llm_timeout),
        )


def create_main_model(config: Config, api_key: Optional[str] = None) -> CompatibleOpenAIModel:
    """Create the main LLM model from configuration."""
    return CompatibleOpenAIModel(
        model_name=config.main_model,
        provider=OpenAIProvider(
            base_url=config.llm_base_url,
            api_key=api_key or config.llm_api_key,
        ),
        settings=_build_model_settings(config, config.main_model)
    )


def create_fallback_model(config: Config, api_key: Optional[str] = None) -> CompatibleOpenAIModel:
    """Create the fallback LLM model from configuration."""
    return CompatibleOpenAIModel(
        model_name=config.fallback_model,
        provider=OpenAIProvider(
            base_url=config.llm_base_url,
            api_key=api_key or config.llm_api_key,
        ),
        settings=_build_model_settings(config, config.fallback_model)
    )


def create_fallback_models(config: Config, api_key: Optional[str] = None) -> FallbackModel:
    """Create fallback models chain from configuration."""
    main = create_main_model(config, api_key=api_key)
    fallback = create_fallback_model(config, api_key=api_key)
    return FallbackModel(main, fallback)


def create_openai_client(config: Config, api_key: Optional[str] = None, timeout: Optional[int] = None) -> OpenAI:
    """Create OpenAI client from configuration."""
    effective_timeout = timeout or getattr(config, 'llm_timeout', DEFAULT_LLM_TIMEOUT)
    with ProxyDisabledContext():
        return OpenAI(
            api_key=api_key or config.llm_api_key,
            base_url=config.llm_base_url or "https://api.openai.com/v1",
            http_client=_build_proxyless_httpx_client(timeout=effective_timeout),
        )


def call_llm(
    prompt: str,
    config: Config,
    model: str = None,
    temperature: float = 0.0,
    api_key: Optional[str] = None,
) -> str:
    """Call LLM with timeout, retry, and logging.

    Retries on transient errors (timeout, rate-limit, server errors)
    up to ``config.llm_max_retries`` times, with ``config.llm_retry_interval``
    seconds between attempts.  Each failure and retry is logged.

    Args:
        prompt: The prompt to send
        config: Configuration containing LLM settings
        model: Model name (defaults to config.main_model)
        temperature: Temperature setting
        api_key: Override API key (for multi-key pool)

    Returns:
        LLM response text
    """
    if model is None:
        model = config.main_model

    max_retries = getattr(config, 'llm_max_retries', DEFAULT_LLM_MAX_RETRIES)
    retry_interval = getattr(config, 'llm_retry_interval', DEFAULT_LLM_RETRY_INTERVAL)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return _call_llm_single(prompt, config, model, temperature, api_key=api_key)
        except _RETRIABLE_ERRORS as e:
            last_error = e
            error_type = type(e).__name__
            # Rate-limit errors: respect server's retry-after if present
            wait_seconds = retry_interval
            if isinstance(e, RateLimitError):
                retry_after = _extract_retry_after(e)
                if retry_after:
                    wait_seconds = max(wait_seconds, retry_after)

            if attempt < max_retries:
                logger.warning(
                    "[LLM Retry] Attempt %d/%d failed (%s: %s). "
                    "Retrying in %ds...",
                    attempt, max_retries, error_type,
                    _truncate_error_msg(str(e), 200),
                    wait_seconds,
                )
                _sleep(wait_seconds)
            else:
                logger.error(
                    "[LLM] All %d attempts exhausted. Last error: %s: %s",
                    max_retries, error_type, _truncate_error_msg(str(e), 500),
                )
        except Exception as e:
            # Non-retriable error (auth, bad request, etc.) — fail immediately
            logger.error(
                "[LLM] Non-retriable error on attempt %d: %s: %s",
                attempt, type(e).__name__, _truncate_error_msg(str(e), 500),
            )
            raise

    # All retries exhausted
    raise last_error


# Error types that warrant a retry (transient / rate-limit / timeout / server)
_RETRIABLE_ERRORS = (
    TimeoutError,
    ConnectionError,
    RateLimitError,
    APIConnectionError,
    APIStatusError,  # covers 5xx server errors
)


def _extract_retry_after(err: RateLimitError) -> Optional[int]:
    """Extract Retry-After from a RateLimitError, if the server provides it."""
    headers = getattr(err, 'headers', None) or {}
    val = headers.get('retry-after')
    if val:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    return None


def _truncate_error_msg(msg: str, max_len: int) -> str:
    """Truncate error message for readable log lines."""
    if len(msg) <= max_len:
        return msg
    return msg[:max_len - 3] + "..."


def _sleep(seconds: int):
    """Sleep for the given number of seconds."""
    import time as _time
    _time.sleep(seconds)


def _call_llm_single(
    prompt: str,
    config: Config,
    model: str,
    temperature: float = 0.0,
    api_key: Optional[str] = None,
) -> str:
    """Single LLM call attempt (no retry logic — called by call_llm)."""
    provider = getattr(config, "provider", "openai-compatible")

    if provider in ("bedrock", "anthropic"):
        return _call_llm_via_litellm(prompt, config, model, temperature, api_key=api_key)

    if provider == "azure-openai":
        return _call_llm_via_azure(prompt, config, model, temperature, api_key=api_key)

    # openai-compatible provider
    llm_timeout = getattr(config, 'llm_timeout', DEFAULT_LLM_TIMEOUT)

    with ProxyDisabledContext():
        client = create_openai_client(config, api_key=api_key, timeout=llm_timeout)

        use_completion_tokens = _should_use_max_completion_tokens(model, config.llm_base_url)
        primary_key = "max_completion_tokens" if use_completion_tokens else "max_tokens"
        fallback_key = "max_tokens" if use_completion_tokens else "max_completion_tokens"

        base_kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }

        try:
            response = client.chat.completions.create(
                **base_kwargs,
                **{primary_key: config.max_tokens},
                timeout=float(llm_timeout),
            )
        except BadRequestError as e:
            if _is_unsupported_token_param_error(e, primary_key):
                logger.info(
                    "Provider rejected %s for model %s; retrying with %s.",
                    primary_key, model, fallback_key,
                )
                response = client.chat.completions.create(
                    **base_kwargs,
                    **{fallback_key: config.max_tokens},
                    timeout=float(llm_timeout),
                )
            else:
                raise
        return response.choices[0].message.content


def _is_unsupported_token_param_error(err: BadRequestError, param: str) -> bool:
    """Return True if *err* is the OpenAI "unsupported_parameter" error for *param*."""
    body = getattr(err, "body", None) or {}
    if isinstance(body, dict):
        error = body.get("error") or {}
        if isinstance(error, dict):
            if error.get("param") == param and error.get("code") == "unsupported_parameter":
                return True
    # Fallback: message-based sniff for proxies that don't preserve structure
    msg = str(err).lower()
    return "unsupported parameter" in msg and param in msg


def _call_llm_via_litellm(
    prompt: str,
    config: Config,
    model: str,
    temperature: float = 0.0,
    api_key: Optional[str] = None,
) -> str:
    """
    Call LLM via litellm for Bedrock/Anthropic providers.

    litellm handles the provider-specific API translation automatically.
    """
    import litellm

    litellm_model = _get_litellm_model_name(model, config.provider)

    with ProxyDisabledContext():
        if config.provider == "bedrock":
            os.environ.setdefault("AWS_DEFAULT_REGION", config.aws_region)
            os.environ.setdefault("AWS_REGION_NAME", config.aws_region)
            logger.debug("Calling Bedrock model %s in region %s", litellm_model, config.aws_region)
        elif config.provider == "anthropic":
            logger.debug("Calling Anthropic model %s via litellm", litellm_model)

        effective_key = api_key or config.llm_api_key
        llm_timeout = getattr(config, 'llm_timeout', DEFAULT_LLM_TIMEOUT)
        response = litellm.completion(
            model=litellm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=config.max_tokens,
            api_key=effective_key if config.provider != "bedrock" else None,
            timeout=float(llm_timeout),
        )
    return response.choices[0].message.content


def _call_llm_via_azure(
    prompt: str,
    config: Config,
    model: str,
    temperature: float = 0.0,
    api_key: Optional[str] = None,
) -> str:
    """
    Call LLM via Azure OpenAI.

    Uses the AzureOpenAI client from the openai package with
    azure_endpoint, api_version, and deployment name.
    """
    from openai import AzureOpenAI

    with ProxyDisabledContext():
        llm_timeout = getattr(config, 'llm_timeout', DEFAULT_LLM_TIMEOUT)
        client = AzureOpenAI(
            api_key=api_key or config.llm_api_key,
            api_version=config.api_version,
            azure_endpoint=config.llm_base_url,
            http_client=_build_proxyless_httpx_client(timeout=llm_timeout),
        )

        deployment = config.azure_deployment or model
        logger.debug("Calling Azure OpenAI deployment %s (api_version=%s)", deployment, config.api_version)

        llm_timeout = getattr(config, 'llm_timeout', DEFAULT_LLM_TIMEOUT)
        response = client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=config.max_tokens,
            timeout=float(llm_timeout),
        )
    return response.choices[0].message.content
