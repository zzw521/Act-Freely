#!/usr/bin/env python3
"""
工单执行体的 HTTP 端点自测（缺陷 D-48.7 接线半边）

`engine/ticket_worker_service.py` 那一层的自测（190 项）证明的是「装配在一个进程里能一起
活着」；本文件证明的是另一半：**真的从 HTTP 打到引擎，外面的人拿得到、也改得动**。路由是薄
包装，但薄包装会出的错正好是服务层自测看不见的三类，所以每条断言都问「什么缺陷会让它红」：

* **接错线**（§1/§8）：循环没在 lifespan 里捕获、周期泵没起、`TT_TICKET_OUTBOX_POLL_SECONDS=0`
  时确实没起。装配层看不见这些——它连 uvicorn 的循环是什么都不知道。周期泵那半给的是「一条
  退避中的外发，没人调任何端点也自己发出去了」，这是 D-48.2 耐久队列在重启后唯一的出路。
* **请求面漏裁决**（§2/§5/§7）：必填没给却投进了一条带空 ticketId 的唤醒；无效 JSON 被当成空
  对象往下走；远程身份能投唤醒（= 远程起 shell）。查询面不设门槛、写面设，两半都要有证据。
* **结果传不出去**（§6）：服务层的字典过了 `json_ready` 才回，端点不自己再拼一份字段表——
  自己拼的那份会在下一次服务层加字段时悄悄少给。

离线运行：模型是脚本化的假实现（经 `tws.configure` 注入，走的是服务层留好的口子），IM 投递
由记账的假通道接住，一次真实 HTTP 也不发。回合真的动盘（工单目录、耐久收件箱、外发账本），
`TONGTIANWORK_HOME` 一节一根。fastapi/httpx 缺失时 SKIP 并以 0 退出。

用法: python tests/integration/engine_ticket_worker_routes_selftest.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-routes-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home-boot")
# 通道解析要确定性地落到「没配模型」：本机 .env 里可能真有 TT_API_KEY
for _key in ("TT_API_KEY", "TT_BASE_URL"):
    os.environ[_key] = ""
os.environ["TT_PROVIDER"] = "openai"

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    from fastapi.testclient import TestClient
    import server as engine_server
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: 端点自测依赖缺失（{exc}）")
    sys.exit(0)

import ticket_worker_adapter as ta  # noqa: E402
import ticket_worker_service as tws  # noqa: E402

FAILURES: list = []
SECTION = {"n": 0}

T0 = 1_760_000_000_000
REPLY_TEXT = "第一版做好了，产物在目录里"
SUMMARY = "已投递回话并收口"


def section(title: str) -> None:
    SECTION["n"] += 1
    print(f"\n── {SECTION['n']}. {title} " + "─" * max(0, 56 - len(title)))


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def use_home(label: str) -> Path:
    """一节一根：外发账本、已读账本、耐久收件箱都是跨工单共享的单文件账本。"""
    home = _TMP / f"home-{label}"
    os.environ["TONGTIANWORK_HOME"] = str(home)
    return home


def seed(**kwargs) -> None:
    """换数据根 + 注入假模型/假通道：先拆单例，再把装配参数交给服务层。

    lifespan 已经建过一套默认装配（真通道、按环境变量解析的模型），本节要的是注入件，
    所以这里显式拆掉重建——`configure` 只记参数，下一次端点调用 `_ticket_runtime()` 才建。
    """
    tws.reset_runtime(timeout_seconds=2.0)
    tws.configure(**kwargs)


class ScriptedModel:
    """按脚本回话的假模型：记下每次请求，超出脚本长度就重复最后一条。"""

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.requests: list = []
        self.lock = threading.Lock()

    def __call__(self, request: dict) -> dict:
        with self.lock:
            self.requests.append(dict(request))
            index = len(self.requests)
        reply = self.replies[min(index, len(self.replies)) - 1]
        return {
            "text": str(reply.get("text") or ""),
            "calls": [dict(c) for c in (reply.get("calls") or [])],
            "outcome": reply.get("outcome", ""),
            "reason": reply.get("reason", ""),
        }

    def blobs(self) -> list:
        out = []
        for request in self.requests:
            parts = [str(request.get("system") or "")]
            parts += [str(m.get("content") or "") for m in (request.get("messages") or [])]
            out.append("\n".join(parts))
        return out


class Channel:
    """假 IM 通道：按到达顺序记 (通道, 会话, 正文)，可让指定正文投递失败。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sent: list = []
        self.fail: set = set()

    def send(self, channel: str, target: str, text: str) -> None:
        if text in self.fail:
            raise ta.DeliveryError(f"通道暂时拒收「{text}」（用例设定）")
        with self.lock:
            self.sent.append((channel, target, text))

    def allow(self, text: str) -> None:
        with self.lock:
            self.fail.discard(text)


def wake_script(reply_text: str = REPLY_TEXT, summary: str = SUMMARY) -> list:
    return [
        {"text": "先做这一步", "calls": [{"tool": "send_to_session", "args": {"kind": "reply", "sessionKey": "slack:C9", "text": reply_text}}]},
        {"text": summary, "calls": [{"tool": "finish_work", "args": {"summary": summary}}], "outcome": "finished"},
    ]


def wait_until(fn, *, timeout: float = 20.0, poll: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(poll)
    return False


def wait_turn_done(timeout: float = 20.0) -> bool:
    """等这一轮的回合跑完：等的是**服务层的空闲判定**（没有工单持有单飞位），不是睡一觉。"""
    runtime = tws.peek_runtime()
    if runtime is None:
        return False
    return runtime.wait_idle(timeout_seconds=timeout).get("ok") is True


def wait_done(client: TestClient, *, timeout: float = 20.0) -> dict:
    """轮询查询面直到外发账里出现一条 done（周期泵的证据就在这个「没人推也好了」）。"""
    box: dict = {}

    def hit() -> bool:
        nonlocal box
        box = client.get("/api/tickets/outbox").json()
        return (box.get("outbox") or {}).get("statusCounts", {}).get("done", 0) >= 1

    wait_until(hit, timeout=timeout)
    return box


# ── §1 启动面：lifespan 把循环与泵接上 ──────────────────────────
section("lifespan 启动面：循环、泵、状态面板")
use_home("boot")
os.environ["TT_TICKET_OUTBOX_POLL_SECONDS"] = "1"
with TestClient(engine_server.app) as client:
    check("lifespan 捕获到了事件循环（跨循环投递要有它）", engine_server._TICKET_LOOP is not None, repr(engine_server._TICKET_LOOP))
    check("外发周期泵按 TT_TICKET_OUTBOX_POLL_SECONDS=1 起了任务", engine_server._TICKET_PUMP_TASK is not None, repr(engine_server._TICKET_PUMP_TASK))
    os.environ["TT_TICKET_OUTBOX_POLL_SECONDS"] = "0.1"
    check("泵间隔有下界：0.1 秒也按 1 秒（0.1 秒一次的泵会把通道打成排队）", engine_server._ticket_outbox_poll_seconds() == 1.0, repr(engine_server._ticket_outbox_poll_seconds()))
    os.environ["TT_TICKET_OUTBOX_POLL_SECONDS"] = "乱填"
    check("环境变量填错不炸引擎：退回默认 20 秒", engine_server._ticket_outbox_poll_seconds() == 20.0, repr(engine_server._ticket_outbox_poll_seconds()))
    os.environ["TT_TICKET_OUTBOX_POLL_SECONDS"] = "1"
    for k in ("TT_ENGINE_TOKEN", "TT_ENGINE_LOCAL_TOKEN", "TT_ENGINE_JWT_SECRET"):
        os.environ.pop(k, None)
    resp = client.get("/api/tickets/status")
    status_body = resp.json() if resp.status_code == 200 else {}
    check("状态面板 200 且 ok", resp.status_code == 200 and status_body.get("ok") is True, f"{resp.status_code} {status_body}")
    check("面板报的是本节数据根（不是引擎 cwd 或别人的根）", status_body.get("home") == str(_TMP / "home-boot"), repr(status_body.get("home")))
    check("没配模型时面板如实说没配好（不假装能起回合）", status_body.get("modelReady") is False, repr(status_body.get("model")))
    check("面板自述里没有密钥字段", "sk-" not in json.dumps(status_body, ensure_ascii=False) and "apiKey" not in json.dumps(status_body, ensure_ascii=False), json.dumps(status_body)[:400])
    check("面板给出的槽位/调度/队列/收件箱四块都在", {"slots", "scheduler", "outbox", "inbox"} <= set(status_body), repr(sorted(status_body)))

    # ── §2 请求面：校验在投递之前 ────────────────────────────
    section("请求面校验：缺字段/坏 JSON 都在投递之前被挡下")
    r = client.post("/api/tickets/wakes", json={"message": "没有工单号"})
    b = r.json()
    check("缺 ticketId 的唤醒被 400 挡下并点名缺什么", r.status_code == 400 and b.get("code") == "TICKET_FIELD_MISSING" and "ticketId" in str(b.get("message")), f"{r.status_code} {b}")
    check("被挡下的唤醒没有进任何账（不留下半条投递）", client.get("/api/tickets/inbox").json().get("inbox", {}).get("records", 0) == 0, repr(client.get("/api/tickets/inbox").json()))
    r = client.post("/api/tickets/wakes", content=b"{not json", headers={"Content-Type": "application/json"})
    check("无效 JSON 不当成空对象往下走", r.status_code == 400 and r.json().get("code") == "VALIDATION_ERROR", f"{r.status_code} {r.text[:200]}")
    r = client.post("/api/tickets/wakes", content=b"[1,2]", headers={"Content-Type": "application/json"})
    check("请求体顶层是数组也被拒并说要对象（不猜成空对象往下投）", r.status_code == 400 and r.json().get("code") == "VALIDATION_ERROR" and "对象" in r.json().get("message", ""), f"{r.status_code} {r.text[:200]}")
    r = client.post("/api/tickets/wakes", json={"ticketId": "t", "message": "m", "assignmentId": "a", "arrivedAtMs": "待会儿再说"})
    check("arrivedAtMs 不是数字就拒（到达时刻是重投的凭据，不能猜）", r.status_code == 400 and r.json().get("code") == "TICKET_FIELD_INVALID", f"{r.status_code} {r.text[:200]}")
    r = client.get("/api/tickets/state")
    check("状态查询不给工单号被拒（不默认查第一单）", r.status_code == 400 and r.json().get("code") == "TICKET_FIELD_MISSING", f"{r.status_code} {r.text[:120]}")
    r = client.get("/api/tickets/history", params={"limit": 0})
    check("history limit=0 被拒（0 条不是「全部」）", r.status_code == 400 and r.json().get("code") == "TICKET_FIELD_INVALID", f"{r.status_code} {r.text[:120]}")
    r = client.post("/api/tickets/config", json={})
    check("空 config 不改任何东西也不重建装配", r.status_code == 400 and r.json().get("code") == "TICKET_CONFIG_EMPTY", f"{r.status_code} {r.text[:120]}")
    r = client.post("/api/tickets/config", json={"agentSlots": 99})
    check("槽位越界被拒（槽位数是并发授权，不是随便填的数字）", r.status_code == 400 and r.json().get("code") == "TICKET_FIELD_INVALID", f"{r.status_code} {r.text[:120]}")
    r = client.post("/api/tickets/outbox/run", json={"limit": 999})
    check("手动推队列的上限被挡（一次推 999 条等于放开洪泛）", r.status_code == 400 and r.json().get("code") == "TICKET_FIELD_INVALID", f"{r.status_code} {r.text[:120]}")
    r = client.post("/api/tickets/outbox/retry", json={})
    check("重试不指名 recordId 被拒（重试哪条不能猜）", r.status_code == 400 and r.json().get("code") == "TICKET_FIELD_MISSING", f"{r.status_code} {r.text[:120]}")
    r = client.post("/api/tickets/resume", json={"ticketId": "t", "prevAssignmentId": "p"})
    check("补查不给新领取被拒（投给谁都不能猜）", r.status_code == 400 and r.json().get("code") == "TICKET_FIELD_MISSING" and "newAssignmentId" in r.text, f"{r.status_code} {r.text[:160]}")
    r = client.get("/api/tickets/resume/plan", params={"ticketId": "t"})
    check("plan 缺 prevAssignmentId 被拒", r.status_code == 400 and r.json().get("code") == "TICKET_FIELD_MISSING", f"{r.status_code} {r.text[:120]}")
    r = client.get("/api/tickets/resume/plan", params={"ticketId": "t", "prevAssignmentId": "p", "candidateWakes": "{不是 JSON]"})
    check("plan 的候选不是合法 JSON 就拒并说原因", r.status_code == 400 and r.json().get("code") == "TICKET_FIELD_INVALID" and "candidateWakes" in r.text, f"{r.status_code} {r.text[:160]}")
    r = client.get("/api/tickets/resume/plan", params={"ticketId": "t", "prevAssignmentId": "p", "candidateWakes": json.dumps([{"text": "一条"}])})
    check("候选给对了形状就过校验（后面由服务层裁决语义）", r.status_code == 200 and r.json().get("ok") is True, f"{r.status_code} {r.text[:200]}")

    # ── §3 投递面：一次真回合从 HTTP 走到收口 ────────────────
    section("投递面：HTTP 投一条唤醒，一轮真回合跑到收口")
    use_home("round")
    model3 = ScriptedModel(wake_script())
    channel3 = Channel()
    seed(model_call=model3, send_fn=channel3.send, goal="把这条工单做完", default_session_key="slack:C9")
    r = client.post("/api/tickets/wakes", json={"ticketId": "tk-3", "message": "麻烦把首页改一版", "assignmentId": "as-3", "title": "改首页", "arrivedAtMs": T0})
    body3 = r.json() if r.status_code == 200 else {}
    check("投递 200 且拿到起了轮的口径", r.status_code == 200 and body3.get("ok") is True and body3.get("action") == "turn-scheduled", f"{r.status_code} {body3}")
    check("这一轮真的跑完了（等服务层自己的空闲判定）", wait_turn_done(), repr(client.get("/api/tickets/status").json().get("scheduler")))
    st3 = client.get("/api/tickets/state", params={"ticketId": "tk-3"}).json()
    check("状态面说这一领取已 finished 收口", st3.get("ok") is True and (st3.get("worker") or {}).get("endedStatus") == "finished", repr(st3)[:400])
    his3 = client.get("/api/tickets/history", params={"ticketId": "tk-3"}).json()
    check("历史里有这一轮且消费了一条唤醒", his3.get("count") == 1 and (his3.get("items") or [{}])[0].get("wakesConsumed") == 1 and (his3.get("items") or [{}])[0].get("ended") == "finished", repr(his3)[:400])
    check("模型确实收到了唤醒正文", any("麻烦把首页改一版" in blob for blob in model3.blobs()), repr(model3.blobs())[:300])
    check("回话真经通道发出（不是只在账本里标了个 done）", ("slack", "C9", REPLY_TEXT) in channel3.sent, repr(channel3.sent))
    ob3 = client.get("/api/tickets/outbox").json()
    check("外发账里有 done 且没有 in-flight 残留", ob3.get("ok") is True and (ob3.get("outbox") or {}).get("statusCounts", {}).get("done", 0) >= 1 and (ob3.get("outbox") or {}).get("statusCounts", {}).get("running", 0) == 0, repr(ob3)[:400])
    inbox3 = client.get("/api/tickets/inbox", params={"ticketId": "tk-3"}).json()
    check("喂进会话后耐久箱把这一单销账", inbox3.get("enabled") is True and (inbox3.get("inbox") or {}).get("forTicket") == [], repr(inbox3)[:400])
    check("回合跑完后槽位归还（人用预留槽不饿死 Agent 额度）", client.get("/api/tickets/status").json().get("scheduler", {}).get("inFlight") == [], repr(client.get("/api/tickets/status").json().get("scheduler")))

    # ── §4 模型没配好就在投递之前拒 ──────────────────────────
    section("模型没配好：投递之前裁决，哪儿也没去")
    use_home("no-model")
    seed()  # 不注入模型，也不给配置：通道解析出 mode=none
    r = client.post("/api/tickets/wakes", json={"ticketId": "tk-4", "message": "有模型再投", "assignmentId": "as-4"})
    b4 = r.json() if r.status_code == 200 else {}
    check("没配模型的唤醒被当场拒（不是投进去压着）", r.status_code == 200 and b4.get("ok") is False and b4.get("stage") == "model-channel" and "模型通道还没配置好" in str(b4.get("reason")), f"{r.status_code} {b4}")
    check("被拒的原话说清「这条没落盘、请配好后重投」", "没有可用模型" in str(b4.get("reason")) or "配好模型" in str(b4.get("reason")), repr(b4)[:300])
    st4 = client.get("/api/tickets/inbox", params={"ticketId": "tk-4"}).json()
    check("拒掉的唤醒没进耐久箱（箱里只放真投出去的）", (st4.get("inbox") or {}).get("records", 0) == 0, repr(st4)[:300])
    check("拒掉的唤醒也没占槽位（注册表里没有它的 worker）", client.get("/api/tickets/status").json().get("workers", {}).get("total", 0) == 0, repr(client.get("/api/tickets/status").json().get("workers")))

    # ── §5 配置推送：改模型不重建装配 ────────────────────────
    section("配置面：改模型是「推」，装配不重建")
    use_home("config")
    seen_chat: list = []

    def fake_transport(body, *, url, api_key, timeout):
        seen_chat.append({"url": url, "apiKey": api_key})
        return {"choices": [{"message": {"role": "assistant", "content": "好了"}, "finish_reason": "stop"}]}

    seed(
        model_config={"baseUrl": "https://first.invalid/v1", "apiKey": "sk-first", "modelName": "m-first"},
        chat=fake_transport,
        send_fn=False,
    )
    s5 = client.get("/api/tickets/status").json()
    check("建起来时通道用的是真实现（不是注入的 model_call）", s5.get("model", {}).get("host") == "first.invalid" and s5.get("modelReady") is True, repr(s5.get("model")))
    r = client.post("/api/tickets/config", json={"modelConfig": {"baseUrl": "https://second.invalid/v1", "apiKey": "sk-second", "modelName": "m-second", "prices": {"inputPerM": 1.0, "outputPerM": 2.0, "currency": "CNY"}}})
    c5 = r.json() if r.status_code == 200 else {}
    check("推模型配置不重建装配（rebuilt=False 且没有收尾报告）", r.status_code == 200 and c5.get("rebuilt") is False and c5.get("shutdown") == {}, f"{r.status_code} {c5}")
    check("面板说应用成功且通道就绪", (c5.get("modelApplied") or {}).get("applied") is True and (c5.get("modelApplied") or {}).get("modelReady") is True, repr(c5.get("modelApplied")))
    s5b = client.get("/api/tickets/status").json()
    check("改完下一轮打的就是新端点（面板/通道两处一致）", s5b.get("model", {}).get("host") == "second.invalid" and s5b.get("signature", {}).get("modelName") == "m-second", repr(s5b.get("model")))
    check("密钥不回显在响应里", "sk-second" not in json.dumps(s5b, ensure_ascii=False) and "sk-second" not in r.text, json.dumps(s5b)[:300])
    seen_chat.clear()
    client.get("/api/tickets/status")
    r = client.post("/api/tickets/config", json={"goal": "只做面板口径校验用"})
    c5c = r.json() if r.status_code == 200 else {}
    check("不带 modelConfig 的改口径不假装推了模型（modelApplied 空）", r.status_code == 200 and c5c.get("modelApplied") == {}, f"{r.status_code} {c5c}")
    check("并发口径改坏的值在请求面就拒（不等到重建才发现）", client.post("/api/tickets/config", json={"maxParallelTurns": 0}).status_code == 400, "")
    # 换模式必须连换票件一起换：只换配置的话，下一轮撞的是「网关模式没有换票件」
    chan5 = tws.peek_runtime().channel
    check("直连模式下通道没有换票件（也不需要）", chan5 is not None and chan5._token_provider is None, repr(getattr(chan5, "_token_provider", "?")))
    gw_resp = client.post(
        "/api/tickets/config",
        json={"modelConfig": {"gateway": {"baseUrl": "https://gw.invalid/v1", "controlPlaneUrl": "https://cp.invalid"}, "modelName": "m-gw"}},
    )
    gw_body = gw_resp.json() if gw_resp.status_code == 200 else {}
    check("切成网关模式：面板说就绪且模式真是网关", gw_body.get("modelApplied", {}).get("applied") is True and chan5.describe()["mode"] == "gateway", f"{gw_resp.status_code} {gw_body}")
    check("换票件随配置一起进了活通道（不换就会在下一轮当场抛）", chan5._token_provider is not None, repr(chan5._token_provider))
    status_gw = json.dumps(client.get("/api/tickets/status").json(), ensure_ascii=False)
    check("网关模式下面板不自报凭据字段（令牌只在内存里）", "accessToken" not in status_gw and "apiKey" not in status_gw and "Bearer" not in status_gw, status_gw[:400])
    client.post("/api/tickets/config", json={"modelConfig": {"baseUrl": "https://third.invalid/v1", "apiKey": "sk-third", "modelName": "m-third"}})
    check("从网关切回直连：换票件跟着被换掉（不给残留的旧主体留换票路）", chan5._token_provider is None and chan5.describe()["mode"] == "custom", repr(chan5.describe()))

    # ── §6 补查面：plan / resume 走 HTTP ─────────────────────
    section("补查面：重启补查经 HTTP 分类与重投")
    use_home("resume")
    model6 = ScriptedModel(wake_script(reply_text="补查这一轮的回话", summary="补查后收口"))
    channel6 = Channel()
    seed(model_call=model6, send_fn=channel6.send, goal="补查场景")
    r6a = client.post("/api/tickets/wakes", json={"ticketId": "tk-6", "message": "第一条：先做起来", "assignmentId": "as-6a", "arrivedAtMs": T0})
    check("补查场景的第一轮起了（后面拿它当 prev 领取）", r6a.json().get("action") == "turn-scheduled" and wait_turn_done(), repr(r6a.json()))
    # 第一轮读过的那条，故意再当候选递回去：补查的全部意义就是「读过的别再喂一遍」
    once_read = [{"text": "第一条：先做起来", "arrivedAtMs": T0}]
    p6 = client.get(
        "/api/tickets/resume/plan",
        params={"ticketId": "tk-6", "prevAssignmentId": "as-6a", "candidateWakes": json.dumps(once_read)},
    ).json()
    ev6 = p6.get("evidence") or {}
    check(
        "plan 认得出已读：第一轮喂进过会话的那条进 alreadyRead、没被当成未读要重投",
        p6.get("ok") is True and len(p6.get("alreadyRead") or []) == 1 and (p6.get("unread") or []) == [] and p6.get("resumable") is False,
        repr(p6)[:500],
    )
    check(
        "plan 分不出可读的就不起空转轮：action 说 nothing-to-resume、携带条数为 0",
        p6.get("action") == "nothing-to-resume" and (p6.get("carriedWakes") or 0) == 0,
        repr(p6)[:300],
    )
    check(
        "plan 的证据不说谎：已读落账还在盘上，收口那轮的旧检查点已按 finished 删掉（上下文借不到）",
        int(ev6.get("marksForPrev") or 0) >= 1 and ev6.get("prevCheckpointExists") is False,
        repr(ev6)[:400],
    )
    turns_after_plan = client.get("/api/tickets/history", params={"ticketId": "tk-6"}).json().get("count")
    check("plan 真的只读：查这一眼没起轮、没多烧模型、也没动箱里的条数", turns_after_plan == 1, repr(turns_after_plan))
    r6b = client.post("/api/tickets/resume", json={"ticketId": "tk-6", "prevAssignmentId": "as-6a", "newAssignmentId": "as-6b", "candidateWakes": once_read})
    b6b = r6b.json() if r6b.status_code == 200 else {}
    check("全是已读时补查回 nothing-to-resume（不起空转轮）", b6b.get("action") == "nothing-to-resume" and b6b.get("redelivered") == 0, f"{r6b.status_code} {b6b}")
    check("已读那条被明确记成跳过，不是悄悄丢掉", b6b.get("skippedAlreadyRead") == 1, repr(b6b)[:300])
    turns_after_nothing = client.get("/api/tickets/history", params={"ticketId": "tk-6"}).json().get("count")
    check("nothing-to-resume 确实没多烧一轮模型", turns_after_nothing == 1, repr(turns_after_nothing))
    unread_text = "停机期间到的那条：没人喂进过会话"
    r6c = client.post(
        "/api/tickets/resume",
        json={
            "ticketId": "tk-6",
            "prevAssignmentId": "as-6a",
            "newAssignmentId": "as-6c",
            "candidateWakes": [{"text": unread_text, "arrivedAtMs": T0 + 60_000}],
        },
    )
    b6c = r6c.json() if r6c.status_code == 200 else {}
    check("候选里那条没读过的被重投并起了新领取的一轮", b6c.get("action") == "resumed" and b6c.get("redelivered") == 1 and b6c.get("assignmentId") == "as-6c", f"{r6c.status_code} {b6c}")
    check("重投的这一轮真跑了（等到收口）", wait_turn_done() and client.get("/api/tickets/history", params={"ticketId": "tk-6"}).json().get("count") == 2, repr(client.get("/api/tickets/history", params={"ticketId": "tk-6"}).json()))
    check("停机期间那条确实进了新一轮的提示词（不是只记在计划里）", any(unread_text in blob for blob in model6.blobs()), repr(model6.blobs())[:400])
    check("恢复注记点名了旧领取与携带条数（不是只回个 id 糊弄）", b6c.get("carriedWakes") == 1 and "as-6a" in str(b6c.get("recoveryNote") or "") and str(b6c.get("recoveryNote") or "").strip() != "", repr(b6c)[:400])

    # ── §7 鉴权面：写面要本机，查询面不要 ────────────────────
    section("鉴权面：远程身份能查不能投")
    os.environ["TT_ENGINE_TOKEN"] = "service-token-for-selftest"
    try:
        r = client.get("/api/tickets/status")
        check("服务令牌开着时不带凭据一律 401（工单端点不例外）", r.status_code == 401 and r.json().get("code") == "ENGINE_UNAUTHORIZED", f"{r.status_code} {r.text[:160]}")
        headers = {"Authorization": "Bearer service-token-for-selftest"}
        r = client.get("/api/tickets/status", headers=headers)
        check("服务主体能读状态（查询面不起进程）", r.status_code == 200 and r.json().get("ok") is True, f"{r.status_code} {r.text[:160]}")
        r = client.get("/api/tickets/resume/plan", params={"ticketId": "tk-7", "prevAssignmentId": "p"}, headers=headers)
        check("服务主体能读补查计划（plan 只读不动盘）", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
        r = client.post("/api/tickets/wakes", json={"ticketId": "tk-7", "message": "远程投一条", "assignmentId": "as-7"}, headers=headers)
        b7 = r.json() if r.status_code == 403 else {}
        check("远程服务主体投唤醒被 403（worker 会起真实进程）", r.status_code == 403 and b7.get("code") == "TICKET_WORKER_DESKTOP_ONLY", f"{r.status_code} {r.text[:200]}")
        check("被 403 挡下的唤醒没有落进任何账", client.get("/api/tickets/inbox", params={"ticketId": "tk-7"}, headers=headers).json().get("inbox", {}).get("records", 0) == 0, repr(client.get("/api/tickets/inbox", headers=headers).json())[:300])
        for path in ("/api/tickets/kick", "/api/tickets/resume", "/api/tickets/config", "/api/tickets/outbox/run"):
            rr = client.post(path, json={"ticketId": "tk-7", "recordId": "x", "action": "skip", "modelConfig": {"baseUrl": "https://remote.invalid/v1"}}, headers=headers)
            check(f"写面 {path} 同样只对本机开放", rr.status_code == 403, f"{rr.status_code} {rr.text[:160]}")
        os.environ["TT_ENGINE_LOCAL_TOKEN"] = "local-token-for-selftest"
        local_headers = {"x-tt-engine-token": "local-token-for-selftest"}
        r = client.post("/api/tickets/wakes", json={"ticketId": "tk-7", "message": "桌面壳投的一条", "assignmentId": "as-7l"}, headers=local_headers)
        check("本机令牌来的请求能投（桌面壳是唯一写面入口）", r.status_code == 200 and r.json().get("ok") is True, f"{r.status_code} {r.text[:200]}")
        check("本机身份下这一轮也真跑完了", wait_turn_done(), repr(client.get("/api/tickets/status", headers=local_headers).json().get("scheduler")))
    finally:
        os.environ.pop("TT_ENGINE_TOKEN", None)
        os.environ.pop("TT_ENGINE_LOCAL_TOKEN", None)

    # ── §8 周期泵：没人推也发得出去 ──────────────────────────
    section("周期泵：退避中的外发不等端点也发出去")
    use_home("pump")
    model8 = ScriptedModel(wake_script(reply_text="这条要先失败一次", summary="失败重试后收口"))
    channel8 = Channel()
    channel8.fail.add("这条要先失败一次")
    seed(model_call=model8, send_fn=channel8.send, goal="周期泵场景")
    r8 = client.post("/api/tickets/wakes", json={"ticketId": "tk-8", "message": "发一条会被通道拒的", "assignmentId": "as-8"})
    check("回合跑完（外发第一次投递失败）", r8.json().get("action") == "turn-scheduled" and wait_turn_done(), repr(r8.json()))
    ob8 = client.get("/api/tickets/outbox").json()
    counts8 = (ob8.get("outbox") or {}).get("statusCounts") or {}
    check("失败的那条回到 pending 等退避（不是悄悄丢掉）", counts8.get("pending", 0) == 1 and counts8.get("done", 0) == 0, repr(ob8)[:400])
    check("第一次失败的原文由留在账上可查", "用例设定" in json.dumps(client.get("/api/tickets/outbox").json(), ensure_ascii=False), json.dumps(client.get("/api/tickets/outbox").json())[:400])
    channel8.allow("这条要先失败一次")
    box8 = wait_done(client, timeout=25.0)
    check("没人调任何写端点，周期泵到点把它发出去了", (box8.get("outbox") or {}).get("statusCounts", {}).get("done", 0) == 1, repr(box8)[:400])
    check("通道确实收到了那条正文（泵走的是同一个 dispatch）", ("slack", "C9", "这条要先失败一次") in channel8.sent, repr(channel8.sent))

# ── §9 泵关掉时不起任务（手动推仍然可用）───────────────────────
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home-pumpoff")
os.environ["TT_TICKET_OUTBOX_POLL_SECONDS"] = "0"
section("TT_TICKET_OUTBOX_POLL_SECONDS=0：泵不起，端点仍能推")
with TestClient(engine_server.app) as client_off:
    check("泵关闭时没有周期任务（引擎起来不背这个）", engine_server._TICKET_PUMP_TASK is None, repr(engine_server._TICKET_PUMP_TASK))
    check("泵关闭也照常捕获循环（端点写面仍要跨循环投递）", engine_server._TICKET_LOOP is not None, repr(engine_server._TICKET_LOOP))
    seed()
    r = client_off.get("/api/tickets/status")
    check("泵关了状态面照样回", r.status_code == 200 and r.json().get("ok") is True, f"{r.status_code} {r.text[:160]}")

# ── 收尾：单例与临时目录 ────────────────────────────────────────
tws.reset_runtime(timeout_seconds=2.0)
check("自测收尾把单例收回（不留活线程给下一份用例）", tws.peek_runtime() is None, repr(tws.peek_runtime()))
shutil.rmtree(_TMP, ignore_errors=True)

print("\n" + "-" * 52)
print(f"合计 {SECTION['n']} 节，失败 {len(FAILURES)} 项")
if FAILURES:
    for name in FAILURES:
        print(f"  FAIL: {name}")
    print("engine_ticket_worker_routes_selftest: 有失败")
    sys.exit(1)
print("engine_ticket_worker_routes_selftest: 全绿")
