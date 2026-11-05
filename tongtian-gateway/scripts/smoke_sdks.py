"""G2 替代证据：用官方 Anthropic / Google GenAI SDK 以同协议请求打网关。

Claude Code、Gemini CLI 未安装时，用它俩的官方 Python SDK 作为替代证据，
覆盖非流式、流式与一次工具往返，并检查 `usage_events.protocol` 分别是
`anthropic` / `gemini`。

运行：python scripts/smoke_sdks.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests.support as support  # noqa: E402


def anthropic_smoke(token: str) -> None:
    from anthropic import Anthropic

    client = Anthropic(base_url=support.BASE, auth_token=token)
    tools = [
        {
            "name": "get_weather",
            "description": "查询城市天气",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]

    first = client.messages.create(
        model="auto",
        max_tokens=64,
        tools=tools,
        messages=[{"role": "user", "content": "北京天气"}],
    )
    block = next(
        (item for item in first.content if getattr(item, "type", None) == "tool_use"),
        None,
    )
    support.check(
        "SDK Anthropic 工具回合",
        first.stop_reason == "tool_use"
        and block is not None
        and block.name == "get_weather"
        and block.input == {"city": "Beijing"},
        str(first.content),
    )
    if block is None:
        return

    second = client.messages.create(
        model="auto",
        max_tokens=64,
        messages=[
            {"role": "user", "content": "北京天气"},
            {"role": "assistant", "content": first.content},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "晴 20 度",
                    }
                ],
            },
        ],
    )
    text = "".join(
        item.text for item in second.content if getattr(item, "type", None) == "text"
    )
    support.check("SDK Anthropic 工具多轮", text == "你好", text)

    with client.messages.stream(
        model="auto",
        max_tokens=64,
        messages=[{"role": "user", "content": "hi"}],
    ) as stream:
        streamed = "".join(stream.text_stream)
    support.check("SDK Anthropic 流式", streamed == "你好", streamed)


def gemini_smoke(token: str) -> None:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=token,
        http_options=types.HttpOptions(base_url=support.BASE),
    )
    config = types.GenerateContentConfig(max_output_tokens=64)

    response = client.models.generate_content(
        model="auto", contents="hi", config=config
    )
    support.check("SDK Gemini 非流式", response.text == "你好", str(response.text))

    chunks = list(
        client.models.generate_content_stream(
            model="auto", contents="hi", config=config
        )
    )
    streamed = "".join(
        part.text
        for chunk in chunks
        for candidate in (chunk.candidates or [])
        for part in (candidate.content.parts or [])
        if getattr(part, "text", None)
    )
    support.check("SDK Gemini 流式", streamed == "你好", streamed)


def usage_smoke() -> None:
    from app.state import usage_store

    rows = usage_store.recent(limit=20)
    protocols = {row.protocol for row in rows}
    support.check(
        "SDK 用量落账本 anthropic/gemini",
        {"anthropic", "gemini"} <= protocols,
        str(protocols),
    )


def main() -> int:
    try:
        import anthropic  # noqa: F401
        from google import genai  # noqa: F401
    except ImportError as exc:
        print(f"SKIP 官方 SDK 未安装：{exc}")
        return 0

    server = support.start_gateway()
    upstreams = support.start_fake_upstreams(("openai",))
    try:
        support.configure_openai_provider(upstreams["openai"])
        token = support.make_token()
        anthropic_smoke(token)
        gemini_smoke(token)
        usage_smoke()
    finally:
        support.stop_fake_upstreams(upstreams)
        support.stop_gateway(server)

    return support.report("官方 SDK 协议冒烟通过")


if __name__ == "__main__":
    raise SystemExit(main())
