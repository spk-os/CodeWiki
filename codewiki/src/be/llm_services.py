"""
LLM service factory for creating configured LLM clients.

Includes a compatibility layer for OpenAI-compatible API proxies that may
return slightly non-standard responses (e.g. choices[].index = None).

Supports multiple providers: openai-compatible, anthropic, bedrock, azure-openai.
"""
import asyncio
import logging
import os
import time
from typing import Optional

import httpx
from openai.types import chat

from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.providers.openai import OpenAIProvider
from openai import OpenAI, AsyncOpenAI, BadRequestError

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


class _RetryingHTTPTransport(httpx.BaseTransport):
    """httpx transport that retries transient LLM-client failures uniformly.

    Retries on ANY transport exception (timeouts, connection resets) and on
    retriable HTTP statuses — 408 (Request Timeout), 429 (Too Many Requests),
    and any 5xx.  Other 4xx responses (400 Bad Request, 413 Payload Too Large,
    401/403/404 …) are **deterministic**: the identical request will fail the
    same way every time, so retrying only burns wall-clock (a 400 context-
    overflow would otherwise waste max_retries × retry_interval seconds before
    surfacing).  Such responses are returned immediately so the openai SDK can
    raise a proper, fast status error.

    Installing the retry policy at the httpx transport (client) layer — rather
    than per call site — guarantees the SAME policy covers every code path
    that builds a client via ``_build_proxyless_httpx_client``: direct
    ``call_llm`` calls, pydantic-ai agent runs, and azure clients alike.
    Each failed attempt and the final exhaustion are logged clearly.
    """

    # HTTP statuses worth retrying (the request may yet succeed unchanged):
    # 408 timeout, 429 rate-limit, and all 5xx (transient server faults).
    _RETRIABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

    def __init__(self, max_retries: int, retry_interval: int) -> None:
        self._max_retries = max(1, int(max_retries))
        self._retry_interval = max(0, int(retry_interval))
        # Inner transport owns the actual HTTP I/O; trust_env=False keeps it
        # proxy-free (the outer Client's trust_env is ignored once a custom
        # transport is supplied).
        self._real = httpx.HTTPTransport(trust_env=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        response: Optional[httpx.Response] = None
        last_error = "unknown error"
        for attempt in range(1, self._max_retries + 1):
            response = None
            try:
                response = self._real.handle_request(request)
                if response.status_code < 400:
                    return response
                # Non-2xx: read body once for a readable log line, then free
                # the connection before deciding whether to retry.
                err_desc = f"HTTP {response.status_code}"
                try:
                    response.read()
                    body = response.text[:300]
                    if body:
                        err_desc = f"{err_desc}: {body}"
                except Exception:
                    pass
                last_error = err_desc
                # Deterministic 4xx (not 408/429): the same request will fail
                # identically every time — surface it now instead of retrying.
                if response.status_code not in self._RETRIABLE_STATUSES:
                    logger.warning(
                        "[LLM Client] %s failed (%s). Not retried "
                        "(deterministic 4xx).",
                        url, _truncate_error_msg(last_error, 300),
                    )
                    return response
            except Exception as e:  # noqa: BLE001 — retry on ANY failure
                response = None
                last_error = f"{type(e).__name__}: {e}"

            if attempt < self._max_retries:
                logger.warning(
                    "[LLM Client] %s attempt %d/%d failed (%s). Retry in %ds...",
                    url, attempt, self._max_retries,
                    _truncate_error_msg(last_error, 300), self._retry_interval,
                )
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                if self._retry_interval:
                    time.sleep(self._retry_interval)
            else:
                logger.error(
                    "[LLM Client] %s all %d attempts exhausted. Last failure: %s",
                    url, self._max_retries, _truncate_error_msg(last_error, 500),
                )

        # Exhausted: return the last response so the openai SDK can raise a
        # proper status error; if the last attempt threw, surface that.
        if response is not None:
            return response
        raise httpx.TransportError(f"LLM client retries exhausted: {last_error}")


class _RetryingAsyncHTTPTransport(httpx.AsyncBaseTransport):
    """Async twin of :class:`_RetryingHTTPTransport` (see its docstring)."""

    _RETRIABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

    def __init__(self, max_retries: int, retry_interval: int) -> None:
        self._max_retries = max(1, int(max_retries))
        self._retry_interval = max(0, int(retry_interval))
        self._real = httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        response: Optional[httpx.Response] = None
        last_error = "unknown error"
        for attempt in range(1, self._max_retries + 1):
            response = None
            try:
                response = await self._real.handle_async_request(request)
                if response.status_code < 400:
                    return response
                err_desc = f"HTTP {response.status_code}"
                try:
                    await response.aread()
                    body = response.text[:300]
                    if body:
                        err_desc = f"{err_desc}: {body}"
                except Exception:
                    pass
                last_error = err_desc
                # Deterministic 4xx (not 408/429): surface now, do not retry.
                if response.status_code not in self._RETRIABLE_STATUSES:
                    logger.warning(
                        "[LLM Client] %s failed (%s). Not retried "
                        "(deterministic 4xx).",
                        url, _truncate_error_msg(last_error, 300),
                    )
                    return response
            except Exception as e:  # noqa: BLE001 — retry on ANY failure
                response = None
                last_error = f"{type(e).__name__}: {e}"

            if attempt < self._max_retries:
                logger.warning(
                    "[LLM Client] %s attempt %d/%d failed (%s). Retry in %ds...",
                    url, attempt, self._max_retries,
                    _truncate_error_msg(last_error, 300), self._retry_interval,
                )
                if response is not None:
                    try:
                        await response.aclose()
                    except Exception:
                        pass
                if self._retry_interval:
                    await asyncio.sleep(self._retry_interval)
            else:
                logger.error(
                    "[LLM Client] %s all %d attempts exhausted. Last failure: %s",
                    url, self._max_retries, _truncate_error_msg(last_error, 500),
                )

        if response is not None:
            return response
        raise httpx.TransportError(f"LLM client retries exhausted: {last_error}")


def _retry_params(config: Config):
    """Read the uniform retry policy (max attempts, fixed interval) from config."""
    max_retries = getattr(config, 'llm_max_retries', DEFAULT_LLM_MAX_RETRIES)
    retry_interval = getattr(config, 'llm_retry_interval', DEFAULT_LLM_RETRY_INTERVAL)
    return max_retries, retry_interval


def _build_proxyless_httpx_client(config: Config, timeout: Optional[int] = None) -> httpx.Client:
    """Create an httpx.Client that ignores env proxies and retries uniformly.

    The retry policy (``config.llm_max_retries`` / ``config.llm_retry_interval``)
    is installed at the httpx transport layer so that every request through this
    client is retried identically, regardless of which call path created it.
    """
    effective_timeout = timeout if timeout is not None else getattr(config, 'llm_timeout', DEFAULT_LLM_TIMEOUT)
    max_retries, retry_interval = _retry_params(config)
    return httpx.Client(
        transport=_RetryingHTTPTransport(max_retries, retry_interval),
        trust_env=False,
        timeout=httpx.Timeout(float(effective_timeout)),
    )


def _build_proxyless_async_httpx_client(config: Config, timeout: Optional[int] = None) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient that ignores env proxies and retries uniformly.

    Used for pydantic-ai OpenAIProvider (async clients), so sub-agent /
    FallbackModel calls also connect directly instead of honoring
    HTTP_PROXY / HTTPS_PROXY / ALL_PROXY, and benefit from the same
    transport-layer retry as the sync path.
    """
    effective_timeout = timeout if timeout is not None else getattr(config, 'llm_timeout', DEFAULT_LLM_TIMEOUT)
    max_retries, retry_interval = _retry_params(config)
    return httpx.AsyncClient(
        transport=_RetryingAsyncHTTPTransport(max_retries, retry_interval),
        trust_env=False,
        timeout=httpx.Timeout(float(effective_timeout)),
    )


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


def _build_model_settings(config: Config, model_name: str) -> OpenAIChatModelSettings:
    """Build model settings with the correct token parameter."""
    if _should_use_max_completion_tokens(model_name, config.llm_base_url):
        return OpenAIChatModelSettings(
            temperature=0.0,
            max_completion_tokens=config.max_tokens
        )
    return OpenAIChatModelSettings(
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


class CompatibleOpenAIModel(OpenAIChatModel):
    """OpenAIChatModel subclass that patches non-standard API proxy responses.

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
    # Configure litellm for the provider
    if config.provider == "bedrock":
        os.environ.setdefault("AWS_DEFAULT_REGION", config.aws_region)
        os.environ.setdefault("AWS_REGION_NAME", config.aws_region)

    with ProxyDisabledContext():
        llm_timeout = getattr(config, 'llm_timeout', DEFAULT_LLM_TIMEOUT)
        return OpenAI(
            api_key=config.llm_api_key or "not-needed-for-bedrock",
            base_url=config.llm_base_url or "https://api.openai.com/v1",
            http_client=_build_proxyless_httpx_client(config, timeout=llm_timeout),
            max_retries=0,  # retry handled uniformly at the httpx transport layer
        )


def create_main_model(config: Config, api_key: Optional[str] = None) -> CompatibleOpenAIModel:
    """Create the main LLM model from configuration."""
    return CompatibleOpenAIModel(
        model_name=config.main_model,
        provider=OpenAIProvider(
            # Pass a pre-built AsyncOpenAI so we can disable the SDK's own
            # retries (max_retries=0) — the uniform retry lives at the httpx
            # transport layer to avoid double-retry stacking.
            openai_client=AsyncOpenAI(
                base_url=config.llm_base_url,
                api_key=api_key or config.llm_api_key or "api-key-not-set",
                http_client=_build_proxyless_async_httpx_client(config),
                max_retries=0,
            ),
        ),
        settings=_build_model_settings(config, config.main_model)
    )


def create_fallback_model(config: Config, api_key: Optional[str] = None) -> CompatibleOpenAIModel:
    """Create the fallback LLM model from configuration."""
    return CompatibleOpenAIModel(
        model_name=config.fallback_model,
        provider=OpenAIProvider(
            openai_client=AsyncOpenAI(
                base_url=config.llm_base_url,
                api_key=api_key or config.llm_api_key or "api-key-not-set",
                http_client=_build_proxyless_async_httpx_client(config),
                max_retries=0,
            ),
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
            http_client=_build_proxyless_httpx_client(config, timeout=effective_timeout),
            max_retries=0,  # retry handled uniformly at the httpx transport layer
        )


def call_llm(
    prompt: str,
    config: Config,
    model: str = None,
    temperature: float = 0.0,
    api_key: Optional[str] = None,
) -> str:
    """Call LLM; retries are handled uniformly at the httpx client layer.

    The underlying OpenAI/httpx client (see ``_build_proxyless_httpx_client``)
    retries on ANY failure up to ``config.llm_max_retries`` times with a fixed
    ``config.llm_retry_interval`` second interval, with clear per-attempt
    logging. There is NO business-layer retry loop here on purpose: keeping
    the retry policy in one place (the client transport) means it covers every
    call path — direct ``call_llm``, pydantic-ai agent runs, azure, and litellm
    — with no per-branch gaps or double-retry stacking.

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

    return _call_llm_single(prompt, config, model, temperature, api_key=api_key)


def _truncate_error_msg(msg: str, max_len: int) -> str:
    """Truncate error message for readable log lines."""
    if len(msg) <= max_len:
        return msg
    return msg[:max_len - 3] + "..."


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
    litellm owns its own HTTP stack (no injectable httpx transport), so the
    uniform retry policy is applied at this call boundary instead — same
    ``llm_max_retries`` / ``llm_retry_interval`` policy, retrying on ANY
    error, with clear per-attempt logging. litellm's own retries are
    disabled (``num_retries=0``) to avoid double-retry stacking.
    """
    import litellm

    litellm_model = _get_litellm_model_name(model, config.provider)
    max_retries, retry_interval = _retry_params(config)

    with ProxyDisabledContext():
        if config.provider == "bedrock":
            os.environ.setdefault("AWS_DEFAULT_REGION", config.aws_region)
            os.environ.setdefault("AWS_REGION_NAME", config.aws_region)
            logger.debug("Calling Bedrock model %s in region %s", litellm_model, config.aws_region)
        elif config.provider == "anthropic":
            logger.debug("Calling Anthropic model %s via litellm", litellm_model)

        effective_key = api_key or config.llm_api_key
        llm_timeout = getattr(config, 'llm_timeout', DEFAULT_LLM_TIMEOUT)
        call_kwargs = dict(
            model=litellm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=config.max_tokens,
            api_key=effective_key if config.provider != "bedrock" else None,
            timeout=float(llm_timeout),
            num_retries=0,
        )

        last_exc = None
        last_desc = "unknown error"
        for attempt in range(1, max_retries + 1):
            try:
                response = litellm.completion(**call_kwargs)
                return response.choices[0].message.content
            except Exception as e:  # noqa: BLE001 — retry on ANY failure
                last_exc = e
                last_desc = f"{type(e).__name__}: {e}"
                if attempt < max_retries:
                    logger.warning(
                        "[LLM Client] litellm %s attempt %d/%d failed (%s). Retry in %ds...",
                        litellm_model, attempt, max_retries,
                        _truncate_error_msg(last_desc, 300), retry_interval,
                    )
                    if retry_interval:
                        time.sleep(retry_interval)
                else:
                    logger.error(
                        "[LLM Client] litellm %s all %d attempts exhausted. Last failure: %s",
                        litellm_model, max_retries, _truncate_error_msg(last_desc, 500),
                    )
        raise last_exc


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
            http_client=_build_proxyless_httpx_client(config, timeout=llm_timeout),
            max_retries=0,  # retry handled uniformly at the httpx transport layer
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
