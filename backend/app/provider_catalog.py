"""Built-in provider catalog for the compatible API providers supported here.

The catalog is intentionally a small, checked-in snapshot.  It keeps the
Xiangqi app independent from any external runtime while keeping provider
routes, API protocols, endpoints, credential environment variables, and model
catalogs in one place.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BuiltinProviderSpec:
    id: str
    name: str
    api_type: str | None
    protocol: str | None
    base_url: str | None
    api_key_env: str | None
    auth_mode: str
    models: tuple[str, ...]
    supported: bool
    discovery: str
    note: str
    aliases: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("api_type", None)
        value["models"] = list(self.models)
        value["aliases"] = list(self.aliases)
        value["model_count"] = len(self.models)
        return value


def _api_key(
    id: str,
    name: str,
    api_type: str,
    protocol: str,
    base_url: str,
    api_key_env: str,
    models: tuple[str, ...],
    note: str = "",
    aliases: tuple[str, ...] = (),
) -> BuiltinProviderSpec:
    return BuiltinProviderSpec(
        id=id,
        name=name,
        api_type=api_type,
        protocol=protocol,
        base_url=base_url,
        api_key_env=api_key_env,
        auth_mode="api_key",
        models=models,
        supported=True,
        discovery="catalog",
        note=note or "使用内置模型目录；可在模型目录中手动补充未列出的模型 ID。",
        aliases=aliases,
    )


# Protocol names are the Xiangqi client's names.  api_type is retained only
# as a compatibility reference while the public catalog stays provider-facing.
BUILTIN_PROVIDER_SPECS: tuple[BuiltinProviderSpec, ...] = (
    _api_key("ant-ling", "Ant Ling", "openai-completions", "openai_chat",
             "https://api.ant-ling.com/v1", "ANT_LING_API_KEY",
             ("Ling-2.6-flash", "Ling-2.6-1T", "Ring-2.6-1T")),
    _api_key("anthropic", "Anthropic", "anthropic-messages", "anthropic_messages",
             "https://api.anthropic.com", "ANTHROPIC_API_KEY",
             ("claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"),
             "支持 API Key；该提供方也支持 Claude Pro/Max OAuth，本项目先使用 API Key。",
             ("claude-default", "claude")),
    _api_key("baseten", "Baseten", "openai-completions", "openai_chat",
             "https://inference.baseten.co/v1", "BASETEN_API_KEY",
             ("deepseek-ai/DeepSeek-V4-Flash-0731", "deepseek-ai/DeepSeek-V4-Pro",
              "moonshotai/Kimi-K2.5")),
    _api_key("cerebras", "Cerebras", "openai-completions", "openai_chat",
             "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY",
             ("gpt-oss-120b", "gemma-4-31b")),
    _api_key("deepseek", "DeepSeek", "openai-completions", "openai_chat",
             "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY",
             ("deepseek-flash", "deepseek-v4-pro"),
             "当前官方模型目录：deepseek-flash（V4.1 Flash）和 deepseek-v4-pro；旧 V4 Flash 名称仍可手动填写。提供方默认地址是 https://api.deepseek.com；本项目使用其 OpenAI 兼容的 /v1 路径。"),
    _api_key("groq", "Groq", "openai-completions", "openai_chat",
             "https://api.groq.com/openai/v1", "GROQ_API_KEY",
             ("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-120b")),
    _api_key("huggingface", "Hugging Face", "openai-completions", "openai_chat",
             "https://router.huggingface.co/v1", "HF_TOKEN",
             ("MiniMaxAI/MiniMax-M2.5", "Qwen/Qwen3-235B-A22B", "Qwen/Qwen3-Coder-30B-A3B-Instruct")),
    _api_key("kimi-coding", "Kimi For Coding", "anthropic-messages", "anthropic_messages",
             "https://api.kimi.com/coding", "KIMI_API_KEY",
             ("kimi-for-coding", "kimi-for-coding-highspeed", "k3"),
             "该提供方还提供 Kimi Code 订阅 OAuth；本项目使用 API Key。"),
    _api_key("minimax", "MiniMax", "anthropic-messages", "anthropic_messages",
             "https://api.minimax.io/anthropic", "MINIMAX_API_KEY",
             ("MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M3")),
    _api_key("minimax-cn", "MiniMax CN", "anthropic-messages", "anthropic_messages",
             "https://api.minimaxi.com/anthropic", "MINIMAX_CN_API_KEY",
             ("MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M3")),
    _api_key("moonshotai", "Moonshot AI", "openai-completions", "openai_chat",
             "https://api.moonshot.ai/v1", "MOONSHOT_API_KEY",
             ("kimi-k2.5", "kimi-k2-thinking", "kimi-k3")),
    _api_key("moonshotai-cn", "Moonshot AI CN", "openai-completions", "openai_chat",
             "https://api.moonshot.cn/v1", "MOONSHOT_API_KEY",
             ("kimi-k2.5", "kimi-k2-thinking", "kimi-k3")),
    _api_key("nvidia", "NVIDIA", "openai-completions", "openai_chat",
             "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
             ("deepseek-ai/deepseek-v4-flash-0731", "deepseek-ai/deepseek-v4-pro-0813",
              "meta/llama-3.2-90b-vision-instruct")),
    _api_key("openai", "OpenAI", "openai-responses", "openai_responses",
             "https://api.openai.com/v1", "OPENAI_API_KEY",
             ("gpt-5", "gpt-5-chat-latest", "gpt-4.1-mini"),
             "使用 OpenAI Responses 路由；旧配置 openai-default 作为兼容别名。",
             ("openai-default",)),
    _api_key("openrouter", "OpenRouter", "openai-completions", "openai_chat",
             "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
             ("deepseek/deepseek-v3.2", "anthropic/claude-sonnet-4.5", "openai/gpt-5"),
             "该提供方还支持 OAuth；本项目使用 API Key。"),
    _api_key("qwen-token-plan", "Qwen Token Plan", "openai-completions", "openai_chat",
             "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
             "QWEN_TOKEN_PLAN_API_KEY",
             ("qwen3.6-flash", "deepseek-v4-flash", "qwen3.6-plus")),
    _api_key("qwen-token-plan-cn", "Qwen Token Plan CN", "openai-completions", "openai_chat",
             "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
             "QWEN_TOKEN_PLAN_CN_API_KEY",
             ("qwen3.6-flash", "deepseek-v4-flash", "qwen3.6-plus")),
    _api_key("qwen-token-plan-individual", "Qwen Token Plan Individual", "openai-completions", "openai_chat",
             "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
             "QWEN_TOKEN_PLAN_API_KEY",
             ("qwen3.6-flash", "deepseek-v4-pro", "qwen3.7-max")),
    _api_key("together", "Together", "openai-completions", "openai_chat",
             "https://api.together.ai/v1", "TOGETHER_API_KEY",
             ("Qwen/Qwen3.6-Plus", "deepseek-ai/DeepSeek-V4-Pro-0813", "moonshotai/Kimi-K2.6")),
    _api_key("vercel-ai-gateway", "Vercel AI Gateway", "anthropic-messages", "anthropic_messages",
             "https://ai-gateway.vercel.sh", "AI_GATEWAY_API_KEY",
             ("anthropic/claude-sonnet-4.5", "openai/gpt-5", "google/gemini-2.5-flash")),
    _api_key("xai", "xAI", "openai-responses", "openai_responses",
             "https://api.x.ai/v1", "XAI_API_KEY",
             ("grok-4.6", "grok-4.5", "grok-4.3"),
             "该提供方还支持 SuperGrok/X 订阅 OAuth；本项目使用 API Key。"),
    _api_key("xiaomi", "Xiaomi", "openai-completions", "openai_chat",
             "https://api.xiaomimimo.com/v1", "XIAOMI_API_KEY",
             ("mimo-v2.5", "mimo-v2.5-pro", "mimo-v2.5-pro-ultraspeed")),
    _api_key("xiaomi-token-plan-ams", "Xiaomi Token Plan AMS", "openai-completions", "openai_chat",
             "https://token-plan-ams.xiaomimimo.com/v1", "XIAOMI_TOKEN_PLAN_AMS_API_KEY",
             ("mimo-v2.5", "mimo-v2.5-pro")),
    _api_key("xiaomi-token-plan-cn", "Xiaomi Token Plan CN", "openai-completions", "openai_chat",
             "https://token-plan-cn.xiaomimimo.com/v1", "XIAOMI_TOKEN_PLAN_CN_API_KEY",
             ("mimo-v2.5", "mimo-v2.5-pro")),
    _api_key("xiaomi-token-plan-sgp", "Xiaomi Token Plan SGP", "openai-completions", "openai_chat",
             "https://token-plan-sgp.xiaomimimo.com/v1", "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
             ("mimo-v2.5", "mimo-v2.5-pro")),
    _api_key("zai", "Z.AI", "openai-completions", "openai_chat",
             "https://api.z.ai/api/coding/paas/v4", "ZAI_API_KEY",
             ("glm-5.3", "glm-5.2", "glm-4.7")),
    _api_key("zai-coding-cn", "Z.AI Coding CN", "openai-completions", "openai_chat",
             "https://open.bigmodel.cn/api/coding/paas/v4", "ZAI_CODING_CN_API_KEY",
             ("glm-5.3", "glm-5.2", "glm-4.7")),
)


_BY_ID = {spec.id: spec for spec in BUILTIN_PROVIDER_SPECS}
_BY_ALIAS = {alias: spec for spec in BUILTIN_PROVIDER_SPECS for alias in (spec.id, *spec.aliases)}


def builtin_provider_catalog() -> list[dict[str, Any]]:
    return [spec.public() for spec in BUILTIN_PROVIDER_SPECS]


def builtin_provider(provider_id: str) -> BuiltinProviderSpec | None:
    return _BY_ID.get(provider_id) or _BY_ALIAS.get(provider_id)


def provider_for_connection(connection: Any) -> BuiltinProviderSpec | None:
    explicit = getattr(connection, "builtin_provider_id", None)
    if explicit:
        spec = builtin_provider(explicit)
        if (spec and spec.base_url and connection.base_url.rstrip("/") == spec.base_url.rstrip("/")
                and connection.protocol == spec.protocol):
            return spec
        return None
    spec = _BY_ALIAS.get(getattr(connection, "id", ""))
    if not spec or not spec.base_url or connection.protocol != spec.protocol:
        return None
    # Only infer legacy aliases when the endpoint still matches the built-in
    # template.  A custom proxy named "openai" must remain a custom route.
    return spec if connection.base_url.rstrip("/") == spec.base_url.rstrip("/") else None
