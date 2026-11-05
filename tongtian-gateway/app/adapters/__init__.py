"""适配层：协议无关的内部结构，以及可替换的上游适配器。"""

from . import base as _base
from .base import *  # noqa: F401,F403
from .fake import (
    FakeBehavior,
    FakeChatAdapter,
    FakeEmbeddingAdapter,
    FakeEmbeddingBehavior,
    fake_adapters,
)
from .litellm_adapter import (
    LiteLLMChatAdapter,
    LiteLLMEmbeddingAdapter,
    litellm_adapters,
    warm_litellm,
)

__all__ = [
    *_base.__all__,
    "FakeBehavior",
    "FakeChatAdapter",
    "FakeEmbeddingAdapter",
    "FakeEmbeddingBehavior",
    "LiteLLMChatAdapter",
    "LiteLLMEmbeddingAdapter",
    "fake_adapters",
    "litellm_adapters",
    "warm_litellm",
]
