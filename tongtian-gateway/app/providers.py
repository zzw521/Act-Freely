"""Provider 能力矩阵：只声明真实可用的能力。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    chat: bool
    tools: str
    vision: str
    embeddings: str
    note: str
    local: bool = False


# tools/vision/embeddings 取值：yes / untested / no
PROVIDERS: dict[str, ProviderSpec] = {
    "azure_openai": ProviderSpec(
        id="azure_openai",
        label="Azure OpenAI",
        chat=True,
        tools="yes",
        vision="yes",
        embeddings="yes",
        note="deployment 名需通过别名映射",
    ),
    "bailian": ProviderSpec(
        id="bailian",
        label="阿里百炼",
        chat=True,
        tools="untested",
        vision="yes",
        embeddings="yes",
        note="兼容模式与原生模式存在差异，需实测",
    ),
    "volcengine_ark": ProviderSpec(
        id="volcengine_ark",
        label="火山方舟",
        chat=True,
        tools="untested",
        vision="yes",
        embeddings="untested",
        note="模型为 endpoint id",
    ),
    "tencent_hunyuan": ProviderSpec(
        id="tencent_hunyuan",
        label="腾讯云混元",
        chat=True,
        tools="untested",
        vision="untested",
        embeddings="untested",
        note="OpenAI 兼容入口需确认",
    ),
    "anthropic": ProviderSpec(
        id="anthropic",
        label="Anthropic",
        chat=True,
        tools="yes",
        vision="yes",
        embeddings="no",
        note="不提供嵌入，需独立嵌入 provider",
    ),
    "gemini": ProviderSpec(
        id="gemini",
        label="Google Gemini",
        chat=True,
        tools="yes",
        vision="yes",
        embeddings="yes",
        note="原生协议面由网关翻译",
    ),
    "openai_compatible": ProviderSpec(
        id="openai_compatible",
        label="OpenAI 兼容端点",
        chat=True,
        tools="untested",
        vision="untested",
        embeddings="untested",
        note="按策略声明能力",
    ),
    "vllm": ProviderSpec(
        id="vllm",
        label="本地 vLLM",
        chat=True,
        tools="untested",
        vision="untested",
        embeddings="untested",
        note="需要 allowPrivate=true",
        local=True,
    ),
    "ollama": ProviderSpec(
        id="ollama",
        label="本地 Ollama",
        chat=True,
        tools="untested",
        vision="untested",
        embeddings="untested",
        note="需要 allowPrivate=true",
        local=True,
    ),
}


def provider_ids() -> list[str]:
    return list(PROVIDERS)


def get_provider(provider_id: str) -> ProviderSpec | None:
    return PROVIDERS.get(provider_id)
