"""T0.1：LiteLLM 版本探针。

只打本机假上游（tests/fake_upstreams.py），不访问任何真实 provider。
验证 Python 3.13 + pydantic 2.14 下 LiteLLM 的安装、调用、流式、工具、视觉、
嵌入与 usage 字段，作为 M1 锁定版本的依据。

用法：
    .venv/Scripts/python.exe scripts/spike_litellm.py [--json docs/spike-litellm.json]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import traceback
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 探针期间禁止 LiteLLM 拉远端成本表 / 发遥测
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_LOG", "ERROR")
os.environ.setdefault("LITELLM_TELEMETRY", "False")
os.environ.setdefault("LITELLM_DONT_SHOW_FEEDBACK_BOX", "True")

from tests.fake_upstreams import FakeUpstream  # noqa: E402

PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAusB9Wl2nT8AAAAASUVORK5CYII="
)

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询城市天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def _as_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return {
        key: getattr(value, key)
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
        )
        if hasattr(value, key)
    }


def _usage_of(response: Any) -> dict:
    if isinstance(response, dict):
        return _as_dict(response.get("usage"))
    return _as_dict(getattr(response, "usage", None))


def _content_of(response: Any) -> str:
    try:
        return str(response.choices[0].message.content or "")
    except (AttributeError, IndexError, TypeError):
        return ""


def _tool_calls_of(response: Any) -> list[dict]:
    try:
        calls = response.choices[0].message.tool_calls or []
    except (AttributeError, IndexError, TypeError):
        return []
    out = []
    for call in calls:
        function = getattr(call, "function", None)
        out.append(
            {
                "id": getattr(call, "id", ""),
                "name": getattr(function, "name", ""),
                "arguments": getattr(function, "arguments", ""),
            }
        )
    return out


def _stream_result(chunks: list[Any], litellm: Any = None) -> dict:
    text = ""
    tool_arguments = ""
    usage: dict = {}
    finish_reason = ""
    for chunk in chunks:
        usage = _usage_of(chunk) or usage
        try:
            choice = chunk.choices[0]
        except (AttributeError, IndexError, TypeError):
            continue
        if choice.finish_reason:
            finish_reason = str(choice.finish_reason)
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        if getattr(delta, "content", None):
            text += str(delta.content)
        for call in getattr(delta, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            if function is not None and getattr(function, "arguments", None):
                tool_arguments += str(function.arguments)
    usage_source = "chunks" if usage else "none"
    if not usage and litellm is not None:
        # Anthropic / Gemini 的流式 chunk 不带 usage，LiteLLM 只在
        # stream_chunk_builder 与最后一个 chunk 的 hidden_params 里给。
        try:
            built = litellm.stream_chunk_builder(chunks)
            built_usage = _usage_of(built)
            if built_usage:
                usage, usage_source = built_usage, "stream_chunk_builder"
        except Exception as exc:  # noqa: BLE001
            usage_source = f"builder_error: {type(exc).__name__}"
    return {
        "text": text,
        "tool_arguments": tool_arguments,
        "finish_reason": finish_reason,
        "usage": usage,
        "usage_source": usage_source,
        "chunks": len(chunks),
    }


def _body_shape(kind: str, body: dict) -> dict:
    """只记录结构，不记录正文，用来验证协议翻译是否真的发生了。"""
    if kind == "openai":
        types = []
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, list):
                types.extend(
                    part.get("type") for part in content if isinstance(part, dict)
                )
        return {
            "message_content_types": types,
            "tools": bool(body.get("tools")),
            "tool_choice": body.get("tool_choice"),
            "stream_options": body.get("stream_options"),
        }
    if kind == "anthropic":
        types = []
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, list):
                types.extend(
                    part.get("type") for part in content if isinstance(part, dict)
                )
        return {
            "content_types": types,
            "tools": bool(body.get("tools")),
            "max_tokens": body.get("max_tokens"),
        }
    parts = []
    for content in body.get("contents") or []:
        for part in content.get("parts") or []:
            if "functionCall" in part:
                parts.append("functionCall")
            elif "inlineData" in part:
                parts.append("inlineData")
            else:
                parts.append("text")
    return {"part_types": parts, "tools": bool(body.get("tools"))}


class Spike:
    def __init__(self) -> None:
        self.upstreams = {
            "openai": FakeUpstream("openai").start(),
            "anthropic": FakeUpstream("anthropic").start(),
            "gemini": FakeUpstream("gemini").start(),
        }
        self.results: list[dict] = []
        self.litellm = None
        self.import_seconds = None

    def close(self) -> None:
        for upstream in self.upstreams.values():
            upstream.stop()

    def _new_records(self, before: dict[str, int]) -> list[dict]:
        records = []
        for kind, upstream in self.upstreams.items():
            fresh = upstream.records[before[kind] :]
            for item in fresh:
                body = item["body"]
                records.append(
                    {
                        "upstream": kind,
                        "method": item["method"],
                        "path": item["path"],
                        "auth": item["headers"].get("authorization")
                        or item["headers"].get("x-api-key")
                        or item["headers"].get("x-goog-api-key")
                        or "",
                        "body_keys": sorted(body.keys()),
                        "shape": _body_shape(kind, body),
                    }
                )
        return records

    def run(
        self,
        name: str,
        fn: Callable[[Any], Any],
        *,
        required: bool = True,
        note: str = "",
    ) -> None:
        before = {kind: len(up.records) for kind, up in self.upstreams.items()}
        started = time.perf_counter()
        result: dict[str, Any] = {"case": name, "required": required, "note": note}
        try:
            value = fn(self.litellm)
            result.update({"ok": True, "result": value})
        except Exception as exc:  # noqa: BLE001 - 探针要保留原始异常
            result.update(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=6),
                }
            )
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        result["upstream_calls"] = self._new_records(before)
        self.results.append(result)
        mark = "PASS" if result["ok"] else ("FAIL" if required else "SKIP/FAIL")
        print(f"[{mark}] {name} ({result['elapsed_ms']}ms)")
        if not result["ok"]:
            print(f"       {result['error']}")

    @property
    def required_failures(self) -> list[dict]:
        return [r for r in self.results if r["required"] and not r["ok"]]


def _openai_cases(spike: Spike) -> None:
    base = spike.upstreams["openai"].base_url + "/v1"
    messages = [{"role": "user", "content": "你好"}]

    def complete(litellm):
        response = litellm.completion(
            model="openai/fake-openai-model",
            api_base=base,
            api_key="fake-key",
            messages=messages,
            timeout=15,
        )
        return {
            "content": _content_of(response),
            "usage": _usage_of(response),
            "model": getattr(response, "model", ""),
        }

    def stream(litellm):
        chunks = list(
            litellm.completion(
                model="openai/fake-openai-model",
                api_base=base,
                api_key="fake-key",
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                timeout=15,
            )
        )
        return _stream_result(chunks, litellm)

    def tools(litellm):
        response = litellm.completion(
            model="openai/fake-openai-model",
            api_base=base,
            api_key="fake-key",
            messages=[{"role": "user", "content": "北京天气"}],
            tools=[WEATHER_TOOL],
            tool_choice="auto",
            timeout=15,
        )
        return {
            "tool_calls": _tool_calls_of(response),
            "finish_reason": str(response.choices[0].finish_reason),
            "usage": _usage_of(response),
        }

    def tools_stream(litellm):
        chunks = list(
            litellm.completion(
                model="openai/fake-openai-model",
                api_base=base,
                api_key="fake-key",
                messages=[{"role": "user", "content": "北京天气"}],
                tools=[WEATHER_TOOL],
                stream=True,
                stream_options={"include_usage": True},
                timeout=15,
            )
        )
        return _stream_result(chunks, litellm)

    def vision(litellm):
        response = litellm.completion(
            model="openai/fake-openai-model",
            api_base=base,
            api_key="fake-key",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "图里是什么"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{PNG_1X1}"},
                        },
                    ],
                }
            ],
            timeout=15,
        )
        return {"content": _content_of(response), "usage": _usage_of(response)}

    def embedding(litellm):
        response = litellm.embedding(
            model="openai/fake-embedding",
            api_base=base,
            api_key="fake-key",
            input=["hello", "world"],
            timeout=15,
        )
        data = response.data if not isinstance(response, dict) else response.get("data")
        return {
            "vectors": len(data or []),
            "dimensions": len(data[0]["embedding"]) if data else 0,
            "usage": _usage_of(response),
        }

    def token_count(litellm):
        count = litellm.token_counter(
            model="openai/fake-openai-model", messages=messages
        )
        return {"tokens": count}

    spike.run("openai.chat.nonstream", complete)
    spike.run("openai.chat.stream", stream)
    spike.run("openai.chat.tools", tools)
    spike.run("openai.chat.tools.stream", tools_stream)
    spike.run("openai.chat.vision", vision)
    spike.run("openai.embeddings", embedding)
    spike.run(
        "openai.token_counter",
        token_count,
        required=False,
        note="count_tokens 的 M1 依赖项；失败可退化为上游估算",
    )


def _anthropic_cases(spike: Spike) -> None:
    base = spike.upstreams["anthropic"].base_url
    messages = [{"role": "user", "content": "你好"}]

    def complete(litellm):
        response = litellm.completion(
            model="anthropic/claude-fake",
            api_base=base,
            api_key="fake-anthropic-key",
            messages=messages,
            timeout=15,
        )
        return {
            "content": _content_of(response),
            "usage": _usage_of(response),
            "model": getattr(response, "model", ""),
        }

    def stream(litellm):
        chunks = list(
            litellm.completion(
                model="anthropic/claude-fake",
                api_base=base,
                api_key="fake-anthropic-key",
                messages=messages,
                stream=True,
                timeout=15,
            )
        )
        return _stream_result(chunks, litellm)

    def tools(litellm):
        response = litellm.completion(
            model="anthropic/claude-fake",
            api_base=base,
            api_key="fake-anthropic-key",
            messages=[{"role": "user", "content": "北京天气"}],
            tools=[WEATHER_TOOL],
            timeout=15,
        )
        return {
            "tool_calls": _tool_calls_of(response),
            "finish_reason": str(response.choices[0].finish_reason),
            "usage": _usage_of(response),
        }

    spike.run("anthropic.chat.nonstream", complete)
    spike.run("anthropic.chat.stream", stream)
    spike.run("anthropic.chat.tools", tools)


def _gemini_cases(spike: Spike) -> None:
    base = spike.upstreams["gemini"].base_url + "/v1beta"
    messages = [{"role": "user", "content": "你好"}]

    def complete(litellm):
        response = litellm.completion(
            model="gemini/gemini-fake",
            api_base=base,
            api_key="fake-gemini-key",
            messages=messages,
            timeout=15,
        )
        return {
            "content": _content_of(response),
            "usage": _usage_of(response),
            "model": getattr(response, "model", ""),
        }

    def stream(litellm):
        chunks = list(
            litellm.completion(
                model="gemini/gemini-fake",
                api_base=base,
                api_key="fake-gemini-key",
                messages=messages,
                stream=True,
                timeout=15,
            )
        )
        return _stream_result(chunks, litellm)

    def tools(litellm):
        response = litellm.completion(
            model="gemini/gemini-fake",
            api_base=base,
            api_key="fake-gemini-key",
            messages=[{"role": "user", "content": "北京天气"}],
            tools=[WEATHER_TOOL],
            timeout=15,
        )
        return {
            "tool_calls": _tool_calls_of(response),
            "finish_reason": str(response.choices[0].finish_reason),
            "usage": _usage_of(response),
        }

    spike.run("gemini.chat.nonstream", complete)
    spike.run("gemini.chat.stream", stream)
    spike.run("gemini.chat.tools", tools)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    started = time.perf_counter()
    import litellm  # noqa: PLC0415 - 必须在环境变量之后导入

    import pydantic  # noqa: PLC0415

    litellm.suppress_debug_info = True
    litellm.set_verbose = False
    import_seconds = round(time.perf_counter() - started, 3)

    spike = Spike()
    spike.litellm = litellm
    spike.import_seconds = import_seconds
    try:
        print(
            f"python={platform.python_version()} litellm={_version_of('litellm')} "
            f"pydantic={pydantic.VERSION} import={import_seconds}s"
        )
        _openai_cases(spike)
        _anthropic_cases(spike)
        _gemini_cases(spike)
    finally:
        spike.close()

    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "litellm": _version_of("litellm"),
        "pydantic": pydantic.VERSION,
        "fastapi": _version_of("fastapi"),
        "import_seconds": import_seconds,
        "results": spike.results,
        "required_failures": [r["case"] for r in spike.required_failures],
    }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"report -> {args.json}")

    print(
        f"\n{len(spike.results) - len(spike.required_failures)}/{len(spike.results)} "
        f"cases passed; required failures: {report['required_failures'] or 'none'}"
    )
    return 1 if spike.required_failures else 0


def _version_of(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "not-installed"


if __name__ == "__main__":
    raise SystemExit(main())
