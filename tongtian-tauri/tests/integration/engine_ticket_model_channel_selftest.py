#!/usr/bin/env python3
"""
工单执行体的真实模型通道（缺陷 D-48.7）自测

`ticket_worker_adapter` 把 `model_call` 留成注入点，之前所有自测喂的都是脚本化的假通道。
本文件测的是真那一半：`ticket_model_channel` 把适配器的契约翻成一次 OpenAI 兼容的
`POST {baseUrl}/chat/completions`，再把回话翻回契约。钉住的口径（每条都问过「什么缺陷会让它红」）：

* **工具面只有一个来源**：声明给模型看的 6 个工具由 `tool_specs()` 生成，`kind` 的枚举取自
  `MODEL_SEND_KINDS`——模型能看见的名字必须等于执行体会执行的名字，两处各写一份迟早漂移。
* **通道不替模型编话**：参数不是合法 JSON（上游把 write_file 正文截断了）就标
  `rejectReason` 原样带回去，**不补全、不改判成别的工具**；正文像 JSON 却解析不了就抛，
  不比着一句正文把回合当成「仍在进行」拖过去。
* **密钥不出现在任何出口**：`describe()`、快照、异常文本里都不该有它——假传输层专门断言这一点。
* **重试有边界**：429/5xx/连不上算可重试，400/401/403 当场停；网关令牌**每次现取**（会到期）。
* **`role="tool"` 要改写**：适配器把一轮里所有工具结果并成一条，OpenAI 的 tool 角色必须逐条
  对上 tool_call_id——硬发会被上游 400 或把回执丢掉，丢掉的那条正是「你刚才那条命令没跑成」。

网络这一半用注入的假传输层测，`urllib` 真调用那几条用 monkeypatch `urlopen` 测（不碰真端点）。
纯 stdlib，不碰 `engine/server.py` 与 Rust。

用法: python tests/integration/engine_ticket_model_channel_selftest.py
"""

import json
import os
import sys
import tempfile
import time
import urllib.error
from io import BytesIO
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-model-channel-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home-0")
os.environ.pop("TT_BASE_URL", None)
os.environ.pop("TT_API_KEY", None)
os.environ.pop("TT_MODEL", None)
os.environ.pop("TT_PROVIDER", None)

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES: list = []
CHECKS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


import ticket_model_channel as tmc  # noqa: E402
import ticket_worker_adapter as ta  # noqa: E402

SECRET = "sk-FAKE-KEY-8f2b"

# ── §1 配置解析：判定顺序与引擎 _model_kind 一致 ──────────────────
os.environ["TT_API_KEY"] = SECRET
os.environ["TT_BASE_URL"] = "https://env.example.com/v1/"

got = tmc.resolve_model_config({"baseUrl": "https://cfg.example.com/v1", "apiKey": "cfg-key", "modelName": "m-cfg"}, env={})
check("cfg 里带 baseUrl 走 custom，密钥优先 cfg", got["mode"] == "custom" and got["apiKey"] == "cfg-key" and got["baseUrl"] == "https://cfg.example.com/v1", repr(got))
check("cfg.apiKey 缺失才落回 TT_API_KEY", tmc.resolve_model_config({"baseUrl": "https://x/v1"}, env={"TT_API_KEY": SECRET})["apiKey"] == SECRET)
check("只有 TT_BASE_URL 走 openai 兼容端点", tmc.resolve_model_config(None, env={"TT_BASE_URL": "https://env.example.com/v1"})["mode"] == "openai")
check("baseUrl 尾斜杠被收掉（拼出来不带双斜杠）", tmc.resolve_model_config(None, env={"TT_BASE_URL": "https://e/v1//", "TT_API_KEY": SECRET})["baseUrl"] == "https://e/v1")
check("模型名优先 cfg.modelName > TT_MODEL > 默认", tmc.resolve_model_config({"modelName": "a"}, env={"TT_MODEL": "b"})["modelName"] == "a" and tmc.resolve_model_config(None, env={"TT_MODEL": "b"})["modelName"] == "b" and tmc.resolve_model_config(None, env={})["modelName"] == tmc.DEFAULT_MODEL_NAME)

gw = {"gateway": {"baseUrl": "https://gw.example.com/v1", "controlPlaneUrl": "https://cp.example.com", "dept": "d1"}}
parsed = tmc.resolve_model_config(gw, env={})
check("网关模式取 gateway.baseUrl，令牌位留给换票件", parsed["mode"] == "gateway" and parsed["baseUrl"] == "https://gw.example.com/v1" and parsed["apiKey"] == "" and parsed["dept"] == "d1", repr(parsed))
broken = tmc.resolve_model_config({"gateway": {"baseUrl": "https://gw/v1"}}, env={})
check("网关配置不完整不静默退回直连（mode=none + 中文理由）", broken["mode"] == "none" and "controlPlaneUrl" in broken["reason"], repr(broken))

ds = tmc.resolve_model_config(None, env={"TT_API_KEY": SECRET, "TT_PROVIDER": "dashscope"})
check("缺 TT_PROVIDER 按 dashscope：补兼容模式地址", ds["mode"] == "dashscope" and ds["baseUrl"] == tmc.DASHSCOPE_COMPATIBLE_BASE_URL, repr(ds))
no_key = tmc.resolve_model_config(None, env={"TT_PROVIDER": "dashscope"})
check("dashscope 且没有密钥 = 现在还不能跑（none，不是猜个端点跑了必失败）", no_key["mode"] == "none" and "模型" in no_key["reason"], repr(no_key))
weird = tmc.resolve_model_config(None, env={"TT_PROVIDER": "other"})
check("TT_PROVIDER 既不是 dashscope 也没有 base_url → none", weird["mode"] == "none" and weird["reason"] != "", repr(weird))

# ── §2 工具面：与适配器同一把源 ───────────────────────────────────
schemas = tmc.worker_tool_schemas()
names = [s["function"]["name"] for s in schemas]
check("声明给模型的工具名 = 执行体会执行的 6 个", sorted(names) == sorted(ta.WORKER_TOOLS), repr(names))
send_schema = next(s for s in schemas if s["function"]["name"] == "send_to_session")["function"]["parameters"]
check("kind 的枚举就是模型面那三种（launch/deliver_input 不在模型面上）", send_schema["properties"]["kind"]["enum"] == sorted(ta.MODEL_SEND_KINDS.keys()), repr(send_schema["properties"]["kind"]))
run_schema = next(s for s in schemas if s["function"]["name"] == "run_shell")["function"]["parameters"]
check("background 声明成 boolean、units 声明成 integer", run_schema["properties"]["background"]["type"] == "boolean" and next(s for s in schemas if s["function"]["name"] == "read_file")["function"]["parameters"]["properties"]["units"]["type"] == "integer", repr(run_schema))
check("分页参数与幂等键不是必填（start/dedupeKey/background）", set(run_schema["required"]) == {"command"} and "start" not in next(s for s in schemas if s["function"]["name"] == "read_file")["function"]["parameters"]["required"], repr(run_schema["required"]))
check("每个工具声明都带上预算原话（模型看见的是同一份口径）", all("预算" in s["function"]["description"] for s in schemas) and "4000" in next(s for s in schemas if s["function"]["name"] == "read_file")["function"]["description"])

# ── §3 请求侧：messages 翻译 ─────────────────────────────────────
msgs = tmc.to_openai_messages("系统话", [{"role": "user", "content": "唤醒"}, {"role": "assistant", "content": "上一轮"}, {"role": "tool", "content": "被拒：命令里有 sleep"}, {"role": "user", "content": {"nested": 1}}])
check("system 在最前", msgs[0] == {"role": "system", "content": "系统话"})
check("没有 role=tool 硬发（会被上游 400 或丢掉回执）", all(m["role"] != "tool" for m in msgs), repr(msgs))
tool_msg = next(m for m in msgs if "被拒" in m["content"])
check("工具回执改成用户消息并留标记", tool_msg["role"] == "user" and tool_msg["content"].startswith("【工具回执】"), repr(tool_msg))
check("非字符串 content 收成 JSON 文本不抛", msgs[-1]["content"] == '{"nested": 1}', repr(msgs[-1]))
check("字典形状不对也不抛（缺 role 当 user）", tmc.to_openai_messages("", [{"content": "x"}])[0]["role"] == "user")

# ── §4 响应侧：normalize_reply ────────────────────────────────────
def openai_payload(content=None, tool_calls=None, usage=None):
    message = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body = {"choices": [{"message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}]}
    if usage is not None:
        body["usage"] = usage
    return body

reply = tmc.normalize_reply(openai_payload("先看一眼", [{"id": "c1", "function": {"name": "read_file", "arguments": json.dumps({"path": "a.txt", "units": 200})}}], {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}))
check("原生 tool_calls 翻成契约的 calls（args 解析成字典）", reply["text"] == "先看一眼" and reply["calls"] == [{"tool": "read_file", "args": {"path": "a.txt", "units": 200}, "callId": "c1"}], repr(reply))
check("usage 原样透出（接线侧要拿去计量）", reply["usage"]["total_tokens"] == 16)

mixed = tmc.normalize_reply(openai_payload(None, [
    {"id": "c1", "function": {"name": "write_file", "arguments": '{"path": "b.txt", "content": "写到一半被'}},
    {"id": "c2", "function": {"name": "finish_work", "arguments": json.dumps({"summary": "done"})}},
    {"id": "c3", "function": {"name": "", "arguments": "{}"}},
    {"id": "c4", "function": {"name": "list_dir", "arguments": "[1,2]"}},
    {"id": "c5", "function": {"name": "run_shell", "arguments": None}},
]))
check("被截断的参数不补全、不改判：原话标 rejectReason 带回下一轮", "不是合法 JSON" in mixed["calls"][0]["rejectReason"] and mixed["calls"][0]["tool"] == "write_file", repr(mixed["calls"][0]))
check("同批里合法的调用不受牵连", mixed["calls"][1] == {"tool": "finish_work", "args": {"summary": "done"}, "callId": "c2"}, repr(mixed["calls"][1]))
check("没有工具名的调用如实拒", "没有工具名" in mixed["calls"][2]["rejectReason"])
check("参数解析成非对象也拒（不猜成 {} 执行）", "不是对象" in mixed["calls"][3]["rejectReason"], repr(mixed["calls"][3]))
check("arguments 为空按无参调用（不是拒）", mixed["calls"][4]["args"] == {} and "rejectReason" not in mixed["calls"][4])

proto = tmc.normalize_reply(openai_payload('{"text": "读完了", "calls": [{"tool": "list_dir", "args": {"path": "."}}], "outcome": "running", "reason": "先看目录"}'))
check("正文里的协议 JSON 也能解（不支持工具调用的端点）", proto["text"] == "读完了" and proto["calls"][0]["tool"] == "list_dir" and proto["outcome"] == "running" and proto["reason"] == "先看目录", repr(proto))
fenced = tmc.normalize_reply(openai_payload("```json\n{\"text\": \"围栏里的\", \"calls\": []}\n```"))
check("代码围栏里的 JSON 一样解", fenced["text"] == "围栏里的" and fenced["calls"] == [], repr(fenced))
try:
    tmc.normalize_reply(openai_payload('{"text": "这段没写完", "calls": ['))
    check("像 JSON 却解析不了 = 抛，不比着正文把回合当仍在进行", False, "没抛")
except tmc.ModelChannelError as exc:
    check("像 JSON 却解析不了 = 抛，不比着正文把回合当仍在进行", "解析不了" in str(exc), str(exc))
prose = tmc.normalize_reply(openai_payload("我先看看目录里有什么。"))
check("模型就是说了句人话：按正文处理，不硬造成调用", prose["text"].startswith("我先看看") and prose["calls"] == [])
try:
    tmc.normalize_reply({"ok": True})
    check("空 choices 抛中文理由", False, "没抛")
except tmc.ModelChannelError as exc:
    check("空 choices 抛中文理由", "choices" in str(exc), str(exc))
parts = tmc.normalize_reply({"choices": [{"message": {"content": [{"text": "第一段"}, {"ignore": 1}, {"text": "第二段"}]}}]})
check("分片式 content 拼回正文", parts["text"] == "第一段\n第二段", repr(parts))
long_reply = tmc.normalize_reply(openai_payload("x" * (tmc.MAX_REPLY_CHARS + 10)))
check("超长正文截断并留痕（不整段塞进检查点）", len(long_reply["text"]) < tmc.MAX_REPLY_CHARS + 100 and "截断" in long_reply["text"], str(len(long_reply["text"])))

# ── §5 传输：注入假 chat + 真 urllib 的错误映射 ───────────────────
seen: list = []


def fake_chat(body, *, url, api_key, timeout):
    seen.append({"body": body, "url": url, "apiKey": api_key, "timeout": timeout})
    return openai_payload("回话", [{"id": "c1", "function": {"name": "finish_work", "arguments": json.dumps({"summary": "做完了"})}}])


channel = tmc.build_model_channel({"baseUrl": "https://cfg.example.com/v1", "apiKey": SECRET, "modelName": "m-cfg"}, chat=fake_chat)
out = channel({"system": "工单提示词", "messages": [{"role": "user", "content": "唤醒"}], "tools": tmc.worker_tool_schemas()})
sent = seen[0]
check("URL 拼到 chat/completions（baseUrl 带 /v1 也不重复）", sent["url"] == "https://cfg.example.com/v1/chat/completions", sent["url"])
check("请求体：模型名 + 非流式 + tool_choice=auto", sent["body"]["model"] == "m-cfg" and sent["body"]["stream"] is False and sent["body"]["tool_choice"] == "auto")
check("请求体带 6 个工具声明", len(sent["body"]["tools"]) == len(ta.WORKER_TOOLS))
check("协议说明自动追加一次（端点不支持工具调用时才有退路）", tmc.PROTOCOL_NOTE in sent["body"]["messages"][0]["content"])
out2 = channel({"system": sent["body"]["messages"][0]["content"], "messages": [], "tools": []})
check("第二次调用不把协议说明重复追加", seen[1]["body"]["messages"][0]["content"].count("【通道约定】") == 1)
check("契约形状：text + calls + 结局交回适配器", out["calls"][0]["tool"] == "finish_work" and out2 is not None)

url_variants = tmc.completions_url("https://a.example/v1/chat/completions")
check("baseUrl 已经指到 chat/completions 就不再拼", url_variants == "https://a.example/v1/chat/completions", url_variants)
try:
    tmc.completions_url("")
    check("空 baseUrl 当场拒（不拼出相对地址去撞本地）", False, "没抛")
except tmc.ModelChannelError as exc:
    check("空 baseUrl 当场拒（不拼出相对地址去撞本地）", "baseUrl" in str(exc), str(exc))

# 重试面
slept: list = []
attempts = {"n": 0}


def flaky(body, *, url, api_key, timeout):
    attempts["n"] += 1
    if attempts["n"] == 1:
        raise tmc.ModelChannelError("模型调用被上游拒绝（HTTP 429）：限流", retryable=True, status_code=429)
    return openai_payload("第二次成了")


retry_channel = tmc.TicketModelChannel({"baseUrl": "https://x/v1", "apiKey": SECRET, "modelName": "m"}, chat=flaky, sleep=slept.append)
check("429 之后重试一次并成功", retry_channel({"system": "s", "messages": []})["text"] == "第二次成了" and attempts["n"] == 2 and len(slept) == 1, repr({"n": attempts["n"], "slept": slept}))
check("退避真的等待过（不是原地重试）", slept[0] > 0)

hard = {"n": 0}


def always_500(body, **kw):
    hard["n"] += 1
    raise tmc.ModelChannelError("模型调用被上游拒绝（HTTP 503）", retryable=True, status_code=503)


down = tmc.TicketModelChannel({"baseUrl": "https://x/v1", "apiKey": SECRET, "modelName": "m"}, chat=always_500, sleep=lambda s: None, max_retries=2)
try:
    down({"system": "s", "messages": []})
    check("连续 5xx 到上限就抛（不无限重试烧额度）", False, "没抛")
except tmc.ModelChannelError as exc:
    check("连续 5xx 到上限就抛（不无限重试烧额度）", hard["n"] == 3 and "503" in str(exc) and down.retries == 2, repr({"n": hard["n"], "retries": down.retries}))
check("失败原因留在通道自述里（状态端点看得见）", "503" in down.describe()["lastError"], repr(down.describe()))

bad_req = {"n": 0}


def always_400(body, **kw):
    bad_req["n"] += 1
    raise tmc.ModelChannelError("模型调用被上游拒绝（HTTP 400）", retryable=False, status_code=400)


four = tmc.TicketModelChannel({"baseUrl": "https://x/v1", "apiKey": SECRET, "modelName": "m"}, chat=always_400, sleep=lambda s: None, max_retries=2)
try:
    four({"system": "s", "messages": []})
    check("400/401 这类不重试（重试同样的请求只会同样失败）", False, "没抛")
except tmc.ModelChannelError:
    check("400/401 这类不重试（重试同样的请求只会同样失败）", bad_req["n"] == 1 and four.retries == 0, repr(bad_req))

# 网关令牌每次现取
tokens = iter(["t1", "t2"])
gateway_seen: list = []


def gateway_chat(body, *, url, api_key, timeout):
    gateway_seen.append(api_key)
    return openai_payload("ok")


gw_channel = tmc.TicketModelChannel({"gateway": {"baseUrl": "https://gw/v1", "controlPlaneUrl": "https://cp", "modelName": "m"}}, chat=gateway_chat, token_provider=lambda: next(tokens))
gw_channel({"system": "s", "messages": []})
gw_channel({"system": "s", "messages": []})
check("网关模式每次现取令牌（短期令牌会到期，不能缓存进请求）", gateway_seen == ["t1", "t2"], repr(gateway_seen))
no_provider = tmc.TicketModelChannel({"gateway": {"baseUrl": "https://gw/v1", "controlPlaneUrl": "https://cp"}}, chat=gateway_chat)
try:
    no_provider({"system": "s", "messages": []})
    check("网关模式没有换票件 = 如实拒，不拿空令牌去撞", False, "没抛")
except tmc.ModelChannelError as exc:
    check("网关模式没有换票件 = 如实拒，不拿空令牌去撞", "token_provider" in str(exc) or "令牌" in str(exc), str(exc))
empty_token = tmc.TicketModelChannel({"gateway": {"baseUrl": "https://gw/v1", "controlPlaneUrl": "https://cp"}}, chat=gateway_chat, token_provider=lambda: "")
try:
    empty_token({"system": "s", "messages": []})
    check("换回来的令牌是空 = 拒（登录失效要说得出）", False, "没抛")
except tmc.ModelChannelError as exc:
    check("换回来的令牌是空 = 拒（登录失效要说得出）", "为空" in str(exc), str(exc))

unready = tmc.TicketModelChannel(None, env={"TT_PROVIDER": "dashscope"}, chat=gateway_chat)
check("没配置就是没配置：ready() False，不猜端点", unready.ready() is False and unready.describe()["mode"] == "none")
try:
    unready({"system": "s", "messages": []})
    check("未配置的通道不起请求", False, "没抛")
except tmc.ModelChannelError as exc:
    check("未配置的通道不起请求", "模型" in str(exc), str(exc))

# ── §6 urllib 真调用那一半：monkeypatch urlopen ──────────────────
class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, _limit=-1):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


requests_seen: list = []


def fake_urlopen(request, timeout=None):
    requests_seen.append({"url": request.full_url, "headers": dict(request.header_items()), "body": json.loads(request.data.decode("utf-8")), "timeout": timeout})
    return FakeResponse(json.dumps(openai_payload("真 urllib 这条路")).encode("utf-8"))


real_urlopen = tmc.urllib.request.urlopen
tmc.urllib.request.urlopen = fake_urlopen
try:
    live = tmc.TicketModelChannel({"baseUrl": "https://live.example/v1", "apiKey": SECRET, "modelName": "m"})
    live_reply = live({"system": "s", "messages": [{"role": "user", "content": "唤醒"}], "tools": []})
    check("默认传输真走 urllib 并回契约字典", live_reply["text"] == "真 urllib 这条路" and live_reply["calls"] == [], repr(live_reply))
    sent_headers = requests_seen[0]["headers"]
    check("密钥只进请求头，正文里没有", sent_headers.get("Authorization") == f"Bearer {SECRET}" and SECRET not in json.dumps(requests_seen[0]["body"], ensure_ascii=False))
    check("超时随请求传给 urlopen（不无限等）", requests_seen[0]["timeout"] == tmc.DEFAULT_TIMEOUT_SECONDS, repr(requests_seen[0]["timeout"]))

    def http_500(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "服务器出错", {}, BytesIO("上游吐了栈".encode("utf-8")))

    tmc.urllib.request.urlopen = http_500
    tries = {"n": 0}
    live500 = tmc.TicketModelChannel({"baseUrl": "https://live/v1", "apiKey": SECRET, "modelName": "m"}, max_retries=1, sleep=lambda s: tries.__setitem__("n", tries["n"] + 1))
    try:
        live500({"system": "s", "messages": []})
        check("HTTP 5xx 可重试并把上游说明带进理由", False, "没抛")
    except tmc.ModelChannelError as exc:
        check("HTTP 5xx 可重试并把上游说明带进理由", tries["n"] == 1 and "500" in str(exc) and "上游吐了栈" in str(exc), str(exc))
        check("异常文本里没有密钥", SECRET not in str(exc), str(exc))

    def conn_refused(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    tmc.urllib.request.urlopen = conn_refused
    offline = tmc.TicketModelChannel({"baseUrl": "https://live/v1", "apiKey": SECRET, "modelName": "m"}, max_retries=0)
    try:
        offline({"system": "s", "messages": []})
        check("连不上算可重试那类（原因如实写）", False, "没抛")
    except tmc.ModelChannelError as exc:
        check("连不上算可重试那类（原因如实写）", exc.retryable is True and "连不上" in str(exc), str(exc))

    def garbage(request, timeout=None):
        return FakeResponse(b"<html>not json</html>")

    tmc.urllib.request.urlopen = garbage
    junk = tmc.TicketModelChannel({"baseUrl": "https://live/v1", "apiKey": SECRET, "modelName": "m"}, max_retries=0)
    try:
        junk({"system": "s", "messages": []})
        check("响应不是 JSON = 不采信（不当成空回合）", False, "没抛")
    except tmc.ModelChannelError as exc:
        check("响应不是 JSON = 不采信（不当成空回合）", "不是合法 JSON" in str(exc), str(exc))

    def too_big(request, timeout=None):
        return FakeResponse(b"x" * (tmc.MAX_RESPONSE_BYTES + 10))

    tmc.urllib.request.urlopen = too_big
    huge = tmc.TicketModelChannel({"baseUrl": "https://live/v1", "apiKey": SECRET, "modelName": "m"}, max_retries=0)
    try:
        huge({"system": "s", "messages": []})
        check("响应超上限 = 拒（多半不是模型回话）", False, "没抛")
    except tmc.ModelChannelError as exc:
        check("响应超上限 = 拒（多半不是模型回话）", "上限" in str(exc), str(exc))
finally:
    tmc.urllib.request.urlopen = real_urlopen

# ── §7 用量回调与自述 ────────────────────────────────────────────
usage_seen: list = []
cb = tmc.build_model_channel({"baseUrl": "https://u/v1", "apiKey": SECRET, "modelName": "m"}, chat=fake_chat, on_usage=usage_seen.append)
cb({"system": "s", "messages": [], "tools": []})
check("模型没回 usage 就不自造台账（回调不编数字）", usage_seen == [], repr(usage_seen))
check("通道自述里没有密钥字段", "apiKey" not in json.dumps(cb.describe(), ensure_ascii=False) and SECRET not in json.dumps(cb.describe(), ensure_ascii=False), repr(cb.describe()))
check("自述报模式/模型名/主机与调用计数", cb.describe()["mode"] == "custom" and cb.describe()["host"] == "u" and cb.describe()["calls"] >= 1, repr(cb.describe()))

# 模型真回了 usage 时回调必须有内容，而且耗时要盖住退避等待：接线侧（server.py 的用量台账）
# 只有这一条路能知道工单回合花了多久、花了多少 token。
usage_with_tokens: list = []
flaky_attempts = {"n": 0}


def flaky_chat(body, **kw):
    flaky_attempts["n"] += 1
    if flaky_attempts["n"] == 1:
        raise tmc.ModelChannelError("上游暂时不可用", retryable=True)
    return openai_payload("好了", None, {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})


cb_slow = tmc.build_model_channel(
    {"baseUrl": "https://u2/v1", "apiKey": SECRET, "modelName": "m2"},
    chat=flaky_chat,
    on_usage=usage_with_tokens.append,
    max_retries=1,
    # 退避照走、把等待压到 200ms：断言「耗时含退避」要靠这段时间撑住，
    # 只测最后一次往返的话这里会是 0ms，看起来像「一次调用很快」。
    # 阈值留 50ms 而不是贴着睡眠值：vendor/python 的 time.monotonic 是 GetTickCount64
    # （粒度 15.6ms），睡 50ms 会量出 47ms——贴着的断言红在时钟上，不是红在实现上。
    sleep=lambda seconds: time.sleep(0.2),
)
cb_slow({"system": "s", "messages": []})
check("模型回了 usage 就报回调（origin/模型名/tokens 都在）", len(usage_with_tokens) == 1
      and usage_with_tokens[0]["origin"] == tmc.USAGE_ORIGIN
      and usage_with_tokens[0]["model"] == "m2"
      and usage_with_tokens[0]["promptTokens"] == 7
      and usage_with_tokens[0]["completionTokens"] == 3
      and usage_with_tokens[0]["totalTokens"] == 10, repr(usage_with_tokens))
check("用量回调带端到端耗时且含退避等待（槽位占用是真成本）",
      int(usage_with_tokens[0].get("latencyMs") or 0) >= 50, repr(usage_with_tokens))
check("重试成功后仍只报一次用量（一次调用一行台账）", len(usage_with_tokens) == 1 and flaky_attempts["n"] == 2, repr({"usage": len(usage_with_tokens), "attempts": flaky_attempts}))


# ── §8 与适配器对接：一轮真回合从通道走到收口 ────────────────────
script = iter(
    [
        openai_payload(None, [{"id": "c1", "function": {"name": "send_to_session", "arguments": json.dumps({"kind": "reply", "sessionKey": "slack:C1", "text": "第一版做好了"})}}]),
        openai_payload(None, [{"id": "c2", "function": {"name": "finish_work", "arguments": json.dumps({"summary": "已投递回话"})}}]),
    ]
)
turn_seen: list = []


def scripted_chat(body, **kw):
    turn_seen.append(body)
    return next(script)


rig_model = tmc.build_model_channel({"baseUrl": "https://r/v1", "apiKey": SECRET, "modelName": "m"}, chat=scripted_chat)
execute = ta.make_execute_turn(rig_model, goal="打包", default_session_key="slack:C1")
turn = execute({"ticketId": "tk-r", "dirName": "worker-r", "machine": "m1"}, "唤醒：把第一版发出去", [])
check("真通道驱动的一轮：外发交出去、结局收口", turn["ok"] is True and turn["outcome"] == "finished" and [s["kind"] for s in turn["sends"]] == ["reply"] and turn["reason"] == "已投递回话", repr(turn))
check("两往返都在同一回合里（第二轮看见第一轮的反馈）", len(turn_seen) == 2 and any("副作用账本" in m["content"] for m in turn_seen[1]["messages"][1:]), repr([m["role"] for m in turn_seen[1]["messages"]]))

broken_script = iter([openai_payload(None, [{"id": "c1", "function": {"name": "write_file", "arguments": '{"path": "x", "content": "截了'}}]), openai_payload(None, [{"id": "c2", "function": {"name": "finish_work", "arguments": json.dumps({"summary": "s"})}}])])
broken_seen: list = []
write_calls: list = []


def fake_write(path, content):
    write_calls.append((path, content))
    return {"reason": f"写入 {path}"}


def broken_chat(body, **kw):
    broken_seen.append(body)
    return next(broken_script)


broken_turn = ta.make_execute_turn(tmc.build_model_channel({"baseUrl": "https://r/v1", "apiKey": SECRET, "modelName": "m"}, chat=broken_chat), goal="g", write_file=fake_write, max_tool_rounds=3)(
    {"ticketId": "tk-b", "dirName": "worker-b"}, "唤醒", []
)
check("通道标了 rejectReason 的调用不进账本：被截断的那条没变成一次真写入", write_calls == [] and broken_turn["ok"] is True, repr({"turn": broken_turn, "calls": write_calls}))
check("拒绝原话回给下一轮的模型（不是只在通道里碎掉）", "不是合法 JSON" in json.dumps(broken_seen[1], ensure_ascii=False), json.dumps(broken_seen[1], ensure_ascii=False)[:400])

print()
print(f"合计 {CHECKS} 项，失败 {len(FAILURES)} 项")
if FAILURES:
    for name in FAILURES:
        print(f"FAIL: {name}")
    sys.exit(1)
print("engine_ticket_model_channel_selftest: 全绿")
sys.exit(0)
