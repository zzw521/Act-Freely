#!/usr/bin/env python3
"""D-01 部署演练探针：对引擎服务化链路做一次 HTTP/SSE 检查并输出 JSON。

同一份脚本在两个位置运行：
  · 宿主机（Python 3.12）——经真实 nginx TLS 反代访问
  · 引擎容器内（`docker exec`）——走 bridge 网络直连引擎，不经过代理

计时口径：响应头读完为 t0，之后每收到一块数据打一个点，用来判断代理是
"边到边转"还是"攒到最后一次性给"。分帧交给 http.client 处理，不自己解析。

令牌只从环境变量读取（`--token-env` 指定变量名），不进命令行、不进输出。
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit


def _open(base: str, ca: str | None, insecure: bool, timeout: float):
    parts = urlsplit(base)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        ctx = ssl.create_default_context(cafile=ca if ca else None)
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _send(args, extra_headers: dict):
    parts = urlsplit(args.base)
    body = args.body.encode("utf-8") if args.body is not None else None
    headers = {
        "Host": args.host_header or parts.hostname or "",
        "Accept": args.accept or "*/*",
        "User-Agent": "tt-d01-probe/1.0",
    }
    token = os.environ.get(args.token_env) if args.token_env else None
    if token and args.token_name:
        headers[args.token_name] = (
            f"Bearer {token}" if args.token_name.lower() == "authorization" else token
        )
    if body is not None:
        headers["Content-Type"] = "application/json"
    headers.update(extra_headers)
    conn = _open(args.base, args.ca, args.insecure, args.timeout)
    verb = "POST" if body is not None else "GET"
    conn.putrequest(verb, args.path or "/", skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        conn.putheader(k, v)
    if body is not None:
        conn.putheader("Content-Length", str(len(body)))
    conn.endheaders(body)
    return conn


def _read_sse(resp, deadline: float, started: float) -> dict:
    """边收边打点；返回文本、每块到达时刻与异常说明"""
    arrivals: list = []
    chunks_text: list = []
    total = 0
    info = {}
    while True:
        if time.monotonic() > deadline:
            info["timeout"] = True
            break
        try:
            raw = resp.read1(65536)
        except (socket.timeout, TimeoutError) as exc:
            info["idleTimeout"] = str(exc) or type(exc).__name__
            break
        except (http.client.RemoteDisconnected, ConnectionResetError, ssl.SSLError, OSError) as exc:
            info["closedByError"] = f"{type(exc).__name__}: {exc}"
            break
        if not raw:
            break
        total += len(raw)
        arrivals.append({"tMs": round((time.monotonic() - started) * 1000, 1), "bytes": len(raw)})
        chunks_text.append(raw.decode("utf-8", "replace"))
    return {"text": "".join(chunks_text), "arrivals": arrivals, "bytes": total, **info}


def _parse_sse(raw_text: str) -> tuple:
    events, keepalives = [], 0
    for block in raw_text.split("\n\n"):
        if not block.strip():
            continue
        if block.lstrip().startswith(":"):
            keepalives += 1
            continue
        ev, data_lines = {}, []
        for line in block.split("\n"):
            if line.startswith("id:"):
                ev["id"] = line[3:].strip()
            elif line.startswith("event:"):
                ev["event"] = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not ev.get("event"):
            continue
        data = None
        if data_lines:
            try:
                data = json.loads("\n".join(data_lines))
            except Exception:  # noqa: BLE001
                data = {"raw": data_lines}
        ev["data"] = data or {}
        events.append(ev)
    return events, keepalives


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["health", "static", "auth", "chat", "review", "replay", "file"])
    parser.add_argument("--base", required=True)
    parser.add_argument("--path", default="/")
    parser.add_argument("--body", default=None)
    parser.add_argument("--accept", default=None)
    parser.add_argument("--token-env", default=None)
    parser.add_argument("--token-name", default="x-tt-engine-token")
    parser.add_argument("--host-header", default=None)
    parser.add_argument("--ca", default=None)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--extra-header", action="append", default=[])
    parser.add_argument("--save-to", default=None)
    args = parser.parse_args()

    extra = {}
    for item in args.extra_header:
        k, _, v = item.partition("=")
        extra[k] = v
    if args.mode in {"chat", "review"}:
        extra.setdefault("Accept", "text/event-stream")

    started = time.monotonic()
    conn = _send(args, extra)
    resp = conn.getresponse()
    result = {
        "mode": args.mode,
        "status": resp.status,
        "headers": {
            k.lower(): v
            for k, v in resp.getheaders()
            if k.lower() in {"content-type", "x-accel-buffering", "cache-control", "location", "content-length",
                             "content-encoding", "server", "transfer-encoding", "vary"}
        },
    }
    header_ms = round((time.monotonic() - started) * 1000, 1)
    result["headerMs"] = header_ms

    streaming = args.mode in {"chat", "review", "file"}
    if not streaming:
        body = resp.read()
        result["elapsedMs"] = round((time.monotonic() - started) * 1000, 1)
        text = body.decode("utf-8", "replace")
        if args.mode in {"health", "replay", "auth"}:
            try:
                payload = json.loads(text)
            except Exception:  # noqa: BLE001
                payload = None
            if args.mode == "replay" and isinstance(payload, dict):
                result["events"] = [e.get("seq") for e in payload.get("events", [])]
                result["eventTypes"] = [e.get("type") for e in payload.get("events", [])]
                result["windowExpired"] = payload.get("windowExpired")
                if payload.get("code"):
                    result["code"] = payload.get("code")
            else:
                result["json"] = payload
        else:
            result["bytes"] = len(body)
            result["looksLikeHtml"] = text.lstrip()[:200].lower().startswith("<!doctype html")
        _emit(result)
        return 0

    streamed = _read_sse(resp, started + args.timeout, started)
    arrivals = streamed["arrivals"]
    result["totalMs"] = round((time.monotonic() - started) * 1000, 1)
    result["firstByteMs"] = arrivals[0]["tMs"] if arrivals else None
    result["lastByteMs"] = arrivals[-1]["tMs"] if arrivals else None
    result["chunkCount"] = len(arrivals)
    result["bytes"] = streamed["bytes"]
    result["chunkStamps"] = [a["tMs"] for a in arrivals[:16]]
    for key in ("timeout", "idleTimeout", "closedByError"):
        if key in streamed:
            result[key] = streamed[key]

    if args.mode == "file":
        result["sha256"] = hashlib.sha256(streamed["text"].encode("utf-8", "replace")).hexdigest()[:16]
        if args.save_to:
            with open(args.save_to, "wb") as fh:
                fh.write(streamed["text"].encode("utf-8", "replace"))
        _emit(result)
        return 0

    events, keepalives = _parse_sse(streamed["text"])
    result["keepalives"] = keepalives
    result["eventTypes"] = [e["event"] for e in events]
    result["events"] = [
        {
            "event": e["event"],
            "id": e.get("id"),
            "seq": (e.get("data") or {}).get("seq"),
            "status": (e.get("data") or {}).get("status"),
            "code": (e.get("data") or {}).get("code"),
            "stepId": (e.get("data") or {}).get("step_id"),
            "artifact": (e.get("data") or {}).get("path") or (e.get("data") or {}).get("artifacts"),
        }
        for e in events
    ]
    if resp.status >= 400:
        # 非 2xx 的错误体是普通 JSON（401 ENGINE_UNAUTHORIZED / 409 CHECKPOINT_TASK_RUNNING），
        # 不解析就只剩一个 status=409，看不出是被哪条规则挡住的
        try:
            result["json"] = json.loads(streamed["text"])
        except Exception:  # noqa: BLE001
            result["json"] = None
    _emit(result)
    return 0


def _emit(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
