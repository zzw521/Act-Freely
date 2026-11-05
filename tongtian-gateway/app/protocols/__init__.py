"""三个客户端协议面：OpenAI 已接通，Anthropic/Gemini 在 M1.2 接入。"""

from __future__ import annotations

from .anthropic import router as anthropic_router
from .gemini import router as gemini_router
from .openai import router as openai_router

__all__ = ["anthropic_router", "gemini_router", "openai_router"]
