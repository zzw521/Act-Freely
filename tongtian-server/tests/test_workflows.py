#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺陷 D-10 服务端半边：共享定义仓库 + 每次运行留证 + 人工卡点结论的存储与边界。

覆盖（与桌面 `engine_workflow_runtime_selftest.py` 分工：那边验「怎么跑」，这边验「共享与记账」）：
- 定义校验：拒掉共享不出去的那几本（四种节点 / 三种动作 / 九个算子 / 恰好一个触发 / 分支指向 /
  重复 id / 点号 id / attempts 与 onError 打架 / 体积与字段上限），算子大小写按规范收下；
- 升版与写冲突：`expectedVersion` 缺失或对不上都拒（跨设备没人静默盖别人），内容一字未改的
  重复提交按重试处理（不升版、不发事件），历史版只读且滚出保留窗口后如实 404；
- 权限与隔离：非成员统一 404（不探测项目是否存在）、viewer 不能写、归档要创建者或管理员；
- 运行留证：钉版本（digest 对不上就拒、允许钉在保留的历史版上）、重复上报不重复发事件、
  已收口的运行不许改回未收口、归档护栏（未收口运行存在时要 force，且 force 也不删历史）；
- 人工卡点：只认人（自动放行不进这张表）、同一步只能一次、结论不推进运行、
  结论必须落在真卡点上、**钉的那版取不到时 blocked 真落盘**（这一条是本批次修的回滚 bug 的护栏）；
- 事件与清单：`workflow.*` 进 project_events 且负载不带步骤正文，跨项目「等我」清单不含 blocked；
- 路由形状：`/api/v1/agent/workflow-runs` 不被市场的 `/api/v1/{plural}/{item_id}` 通配吞掉。

用法: python tests/test_workflows.py
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-workflows-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "workflows-test-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "测试管理员"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app import workflows  # noqa: E402
from app.main import app  # noqa: E402

_passed = 0
BRIEF = "交回三段：昨天做了什么 / 今天打算做什么 / 卡在哪"


def check(name: str, cond: bool, detail: str = ""):
    global _passed
    if not cond:
        print(f"  ✗ {name} {detail}")
        raise AssertionError(name)
    _passed += 1
    print(f"  ✓ {name}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
_opener = urllib.request.build_opener()


def http(method: str, path: str, *, json_body: dict | None = None, token: str | None = None):
    send = {"Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        send["Content-Type"] = "application/json"
    if token:
        send["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=send, method=method)
    try:
        response = _opener.open(request, timeout=20)
        return response.status, _lower(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, _lower(exc.headers), exc.read()


def _lower(message) -> dict:
    return {str(key).lower(): value for key, value in message.items()}


def body_json(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def ok_call(method: str, path: str, *, json_body: dict | None = None, token: str = "",
            want: int = 200) -> dict:
    """成功路径的简写：状态不对就把响应体原样摔出来，免得失败时只剩一句断言。"""
    status, _, raw = http(method, path, json_body=json_body, token=token or None)
    if status != want:
        raise AssertionError(f"{method} {path} 期望 {want}，得到 {status}: {raw[:400]}")
    return body_json(raw)


def register(email: str, nickname: str) -> dict:
    status, _, raw = http("POST", "/api/v1/auth/register",
                          json_body={"email": email, "password": "secret123", "nickname": nickname})
    if status != 201:
        raise AssertionError(f"注册失败 {status}: {raw[:200]}")
    return body_json(raw)


def create_project(token: str, name: str) -> str:
    data = ok_call("POST", "/api/v1/projects", json_body={"name": name, "description": "自测"},
                   token=token, want=201)
    return data["project"]["id"]


def invite_member(project_id: str, owner_token: str, email: str, role: str, token: str) -> str:
    data = ok_call("POST", f"/api/v1/projects/{project_id}/invites",
                   json_body={"email": email, "role": role}, token=owner_token, want=201)
    ok_call("POST", "/api/v1/projects/invites/accept",
            json_body={"token": data["inviteCode"]}, token=token)
    return token


def start_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(120):
        try:
            if http("GET", "/health")[0] == 200:
                return server
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端启动超时")


# ── 定义样本：一条直线 + 一个条件分支 + 一个人工卡点 ──────────────


def steps_full() -> list[dict]:
    return [
        {"id": "start", "kind": "trigger", "name": "手动触发"},
        {"id": "ask", "kind": "action", "name": "派工给成员", "next": "check",
         "spec": {"action": "team_dispatch", "memberIds": ["member-1"], "brief": BRIEF}},
        {"id": "check", "kind": "condition", "name": "有没有卡住",
         "nextTrue": "gate", "nextFalse": "done",
         "spec": {"op": "nonEmpty", "source": "step:ask.output"}},
        {"id": "gate", "kind": "approval", "name": "组长确认", "next": "done", "onDeny": "ask",
         "question": "这条要不要发给全组？"},
        {"id": "done", "kind": "action", "name": "通知宿主",
         "spec": {"action": "notify_host", "text": "站会已归档"}},
    ]


def steps_linear() -> list[dict]:
    return [
        {"id": "start", "kind": "trigger", "name": "手动触发"},
        {"id": "wake", "kind": "action", "name": "唤醒工单", "next": "end",
         "spec": {"action": "ticket_wake", "text": "把这轮的三段汇总贴回工单"}},
        {"id": "end", "kind": "action", "name": "收口",
         "spec": {"action": "notify_host", "text": "本轮收工"}},
    ]


def save(project_id: str, token: str, *, name: str = "每日站会流", steps: list | None = None,
         key: str = "daily-standup", expected_version: int = 0, description: str = "") -> tuple:
    return http("POST", f"/api/v1/projects/{project_id}/workflows", token=token,
                json_body={"key": key, "name": name, "description": description,
                           "steps": steps if steps is not None else steps_full(),
                           "expectedVersion": expected_version})


def start_run(project_id: str, token: str, workflow_id: str, *, state: str = "running",
              digest: str = "", steps: list | None = None, title: str = "站会第 1 轮",
              room: str = "s-standup", ticket: str = "s-standup~wf-abc", executed: int = 0,
              auto_passed: int = 0, note: str = "") -> dict:
    return ok_call("POST", f"/api/v1/projects/{project_id}/workflows/{workflow_id}/runs",
                   token=token, want=201,
                   json_body={"title": title, "roomRef": room, "ticketRef": ticket,
                              "state": state, "note": note, "executed": executed,
                              "autoPassed": auto_passed, "definitionDigest": digest,
                              "steps": steps if steps is not None else [
                                  {"id": "start", "kind": "trigger", "state": "succeeded"}]})


def report_run(project_id: str, token: str, run_id: str, *, state: str, steps: list,
               note: str = "", executed: int = 0, auto_passed: int = 0) -> tuple:
    return http("POST", f"/api/v1/projects/{project_id}/workflow-runs/{run_id}/report",
                token=token,
                json_body={"state": state, "note": note, "steps": steps,
                           "executed": executed, "autoPassed": auto_passed})


def approve(project_id: str, token: str, run_id: str, *, step_id: str, decision: str,
            note: str = "", question: str = "") -> tuple:
    return http("POST", f"/api/v1/projects/{project_id}/workflow-runs/{run_id}/approve",
                token=token,
                json_body={"stepId": step_id, "decision": decision, "note": note,
                           "question": question})


def events(project_id: str, token: str) -> list[dict]:
    return ok_call("GET", f"/api/v1/projects/{project_id}/events?limit=200",
                   token=token)["events"]


def main() -> None:
    server = start_server()
    try:
        stamp = int(time.time())
        owner = register(f"wf-owner-{stamp}@test.local", "流程作者")
        member = register(f"wf-member-{stamp}@test.local", "组员")
        viewer = register(f"wf-viewer-{stamp}@test.local", "旁观者")
        keeper = register(f"wf-keeper-{stamp}@test.local", "项目管理员")
        stranger = register(f"wf-outside-{stamp}@test.local", "项目外的人")

        project = create_project(owner["accessToken"], "工作流自测项目")
        invite_member(project, owner["accessToken"], f"wf-member-{stamp}@test.local", "member",
                      member["accessToken"])
        invite_member(project, owner["accessToken"], f"wf-viewer-{stamp}@test.local", "viewer",
                      viewer["accessToken"])
        invite_member(project, owner["accessToken"], f"wf-keeper-{stamp}@test.local", "admin",
                      keeper["accessToken"])
        other_project = create_project(stranger["accessToken"], "别人的项目")

        print("\n[定义校验：拒的是共享不出去的那几本]")
        bad_cases = [
            ("空步骤", {"key": "k1", "name": "n", "steps": []}, "非空数组"),
            ("未知节点类型", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "loop", "name": "循环"}]}, "不在四种节点里"),
            ("动作不在三种里", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "b", "kind": "action", "spec": {"action": "send_email"}}]},
             "notify_host"),
            ("派工没有成员", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "b", "kind": "action", "spec": {"action": "team_dispatch",
                                                        "brief": BRIEF}}]}, "memberIds"),
            ("派工没有 brief", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "b", "kind": "action",
                 "spec": {"action": "team_dispatch", "memberIds": ["m-1"]}}]}, "brief"),
            ("唤醒没有正文", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "b", "kind": "action", "spec": {"action": "ticket_wake"}}]}, "正文"),
            ("条件又给 next 又给两分支", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "c", "kind": "condition", "next": "a", "nextTrue": "a", "nextFalse": "a",
                 "spec": {"op": "nonEmpty", "source": "payload"}}]}, "永远不会被用到"),
            ("条件缺一条分支", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "c", "kind": "condition", "nextTrue": "a",
                 "spec": {"op": "nonEmpty", "source": "payload"}}]}, "nextFalse"),
            ("算子不在九个里", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "c", "kind": "condition", "nextTrue": "a", "nextFalse": "a",
                 "spec": {"op": "startsWith", "source": "payload"}}]}, "不在九个里"),
            ("条件的 source 写表达式", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "c", "kind": "condition", "nextTrue": "a", "nextFalse": "a",
                 "spec": {"op": "equals", "source": "expr: 1 + 1"}}]}, "payload 或 step:"),
            ("开了 retry 却没给次数", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "b", "kind": "action", "onError": "retry",
                 "spec": {"action": "notify_host", "text": "x"}}]}, "attempts"),
            ("attempts 写成文字", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "b", "kind": "action", "onError": "retry", "attempts": "很多次",
                 "spec": {"action": "notify_host", "text": "x"}}]}, "不是整数"),
            ("attempts 超上限", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "b", "kind": "action", "onError": "retry",
                 "attempts": workflows.MAX_ATTEMPTS + 1,
                 "spec": {"action": "notify_host", "text": "x"}}]}, "超过上限"),
            ("单字段正文超桌面存得下的长度", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "b", "kind": "action", "spec": {"action": "notify_host",
                                                       "text": "长" * (workflows.MAX_TEXT + 1)}}]},
             "不能超过"),
            ("步 id 含点号", {"key": "k1", "name": "n", "steps": [
                {"id": "a.b", "kind": "trigger"}]}, "归一成连字符"),
            ("步 id 重复", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"}, {"id": "a", "kind": "action",
                 "spec": {"action": "notify_host", "text": "x"}}]}, "重复"),
            ("两个触发", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"}, {"id": "b", "kind": "trigger"}]}, "恰好一个"),
            ("没有触发", {"key": "k1", "name": "n", "steps": [
                {"id": "b", "kind": "action", "spec": {"action": "notify_host", "text": "x"}}]},
             "恰好一个"),
            ("next 指向不存在的步", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger", "next": "ghost"}]}, "定义里没有这一步"),
            ("步数超上限", {"key": "k1", "name": "n", "steps": (
                [{"id": "start", "kind": "trigger"}]
                + [{"id": f"s{i}", "kind": "action", "spec": {"action": "notify_host", "text": "x"}}
                   for i in range(workflows.MAX_STEPS)])}, "超过上限"),
            ("key 大写", {"key": "Daily-Case", "name": "n", "steps": steps_linear()}, "小写字母"),
            ("整本体积超上限", {"key": "k1", "name": "n", "steps": [
                {"id": "a", "kind": "trigger"},
                {"id": "b", "kind": "action", "spec": {"action": "notify_host", "text": "ok",
                                                       "junk": "长" * 200_000}}]},
             "一本流程不该靠往步骤里塞正文来长大"),
        ]
        for label, payload, needle in bad_cases:
            status, _, raw = http("POST", f"/api/v1/projects/{project}/workflows",
                                  token=owner["accessToken"], json_body={**payload, "name": label})
            data = body_json(raw)
            check(f"定义校验：{label} 被拒且说明原因",
                  status == 400 and data.get("code") == "WORKFLOW_INVALID"
                  and needle in str(data.get("message")),
                  f"{status} {raw[:200]}")

        status, _, raw = http("POST", f"/api/v1/projects/{project}/workflows",
                              token=owner["accessToken"],
                              json_body={"key": "k1", "name": "多余字段", "steps": steps_linear(),
                                         "executor": "server"})
        check("定义校验：未声明的字段直接拒（extra=forbid），不给人「服务端也许认这个」的错觉",
              status == 422, f"{status} {raw[:200]}")

        # 名字与问题原文这两条上限**照桌面取值**（`MAX_NAME` = 桌面 `MAX_NAME_CHARS`，
        # `MAX_QUESTION` = 桌面 `MAX_QUESTION_CHARS`，由引擎自测 group_crosswire 钉住）。
        # 服务端收 120 字的话，会出现「云端存得下一本桌面存不下的定义」，共享当场断。
        status, _, raw = http("POST", f"/api/v1/projects/{project}/workflows",
                              token=owner["accessToken"],
                              json_body={"key": "long-name", "name": "长" * (workflows.MAX_NAME + 1),
                                         "steps": steps_linear()})
        check(f"定义校验：名字超 {workflows.MAX_NAME} 字拒（与桌面同源，服务端不替你截短）",
              status == 400 and body_json(raw).get("code") == "WORKFLOW_INVALID",
              f"{status} {raw[:160]}")

        status, _, raw = http("POST", f"/api/v1/projects/{project}/workflows",
                              token=owner["accessToken"],
                              json_body={"key": "long-question", "name": "n", "steps": [
                                  {"id": "a", "kind": "trigger", "next": "r"},
                                  {"id": "r", "kind": "approval",
                                   "question": "话" * (workflows.MAX_QUESTION + 1)}]})
        check("定义校验：卡点问题原文超上限拒——不让人在没读完的话上签字，也不静默截",
              status == 400 and body_json(raw).get("code") == "WORKFLOW_INVALID",
              f"{status} {raw[:160]}")

        status, _, raw = save(project, owner["accessToken"], name="算子大小写", key="case-op",
                              steps=[{"id": "a", "kind": "trigger"},
                                     {"id": "c", "kind": "condition", "nextTrue": "a",
                                      "nextFalse": "a",
                                      "spec": {"op": "NOTEQUALS", "source": "payload"}}])
        check("定义校验：算子按大小写不敏感收下、落库存的是规范写法（与桌面同一口径）",
              status == 201 and body_json(raw)["workflow"]["steps"][1]["spec"]["op"] == "notEquals",
              f"{status} {raw[:200]}")

        print("\n[存本与升版：跨设备写冲突]")
        first = ok_call("POST", f"/api/v1/projects/{project}/workflows",
                        token=owner["accessToken"], want=201,
                        json_body={"key": "daily-standup", "name": "每日站会流",
                                   "description": "早会三轮", "steps": steps_full(),
                                   "expectedVersion": 0})
        wf_id = first["workflow"]["id"]
        check("新建：第一版 version=1、含卡点标记与步数，digest 是内容摘要",
              first["workflow"]["version"] == 1 and first["created"] is True
              and first["workflow"]["hasApproval"] is True
              and first["workflow"]["stepCount"] == 5
              and len(first["digest"]) == 64, json.dumps(first, ensure_ascii=False)[:260])
        check("新建：归档位与运行计数从实（还没跑过）",
              first["workflow"]["archived"] is False and first["workflow"]["runCount"] == 0,
              json.dumps(first["workflow"], ensure_ascii=False)[:200])

        again = ok_call("POST", f"/api/v1/projects/{project}/workflows",
                        token=owner["accessToken"], want=201,
                        json_body={"key": "daily-standup", "name": "每日站会流",
                                   "description": "早会三轮", "steps": steps_full(),
                                   "expectedVersion": 0})
        check("同内容重复提交按重试处理：不升版、不发事件、如实标 unchanged",
              again["workflow"]["version"] == 1 and again.get("unchanged") is True
              and again["seq"] == 0, json.dumps(again, ensure_ascii=False)[:220])

        status, _, raw = save(project, owner["accessToken"], name="每日站会流 v2 盲写",
                              steps=steps_full(), expected_version=0)
        check("改一本已存在的流程而不带 expectedVersion：拒（留空的语义是「只在新建时写」）",
              status == 409 and body_json(raw).get("code") == "WORKFLOW_VERSION_REQUIRED",
              f"{status} {raw[:200]}")

        steps2 = steps_full()
        steps2[1]["spec"]["brief"] = BRIEF + "（再加一条：明天想学什么）"
        status, _, raw = http("POST", f"/api/v1/projects/{project}/workflows",
                              token=owner["accessToken"],
                              json_body={"key": "daily-standup", "name": "每日站会流",
                                         "steps": steps2, "expectedVersion": 9})
        check("expectedVersion 对不上：409 且不覆盖任何人的版本",
              status == 409 and body_json(raw).get("code") == "WORKFLOW_VERSION_CONFLICT",
              f"{status} {raw[:200]}")
        detail = ok_call("GET", f"/api/v1/projects/{project}/workflows/{wf_id}",
                         token=member["accessToken"])
        check("被拒的那次写没留下痕迹：当前版仍是第 1 版",
              detail["workflow"]["version"] == 1
              and detail["workflow"]["steps"][1]["spec"]["brief"] == BRIEF,
              json.dumps(detail["workflow"]["steps"][1], ensure_ascii=False)[:220])

        second = ok_call("POST", f"/api/v1/projects/{project}/workflows",
                         token=owner["accessToken"], want=201,
                         json_body={"key": "daily-standup", "name": "每日站会流",
                                    "steps": steps2, "expectedVersion": 1})
        check("带对版本号：升到第 2 版，旧版进了历史",
              second["workflow"]["version"] == 2 and second["created"] is False
              and second["digest"] != first["digest"],
              json.dumps(second, ensure_ascii=False)[:220])
        listed = ok_call("GET", f"/api/v1/projects/{project}/workflows/{wf_id}",
                         token=owner["accessToken"])
        check("历史清单只列被替换下来的旧版（当前版不在里面重复出现，与桌面 versions 同一口径）",
              [v["version"] for v in listed["versions"]] == [1]
              and listed["versions"][0]["digest"] == first["digest"]
              and listed["workflow"]["version"] == 2,
              json.dumps(listed["versions"], ensure_ascii=False)[:260])

        historic = ok_call("GET", f"/api/v1/projects/{project}/workflows/{wf_id}?version=1",
                           token=member["accessToken"])
        check("按版本号取历史版：只读且如实标 historic，正文与当时一致",
              historic["historic"] is True and historic["revision"]["version"] == 1
              and historic["revision"]["historic"] is True
              and historic["revision"]["steps"][1]["spec"]["brief"] == BRIEF,
              json.dumps(historic, ensure_ascii=False)[:240])
        status, _, raw = http("GET", f"/api/v1/projects/{project}/workflows/{wf_id}?version=77",
                              token=member["accessToken"])
        check("取滚出保留窗口/从未存在的版本：404 并说明只保留最近 N 版（不退回当前版糊人）",
              status == 404 and body_json(raw).get("code") == "WORKFLOW_VERSION_UNAVAILABLE"
              and str(workflows.MAX_VERSIONS) in str(body_json(raw).get("message")),
              f"{status} {raw[:200]}")

        print("\n[权限与隔离]")
        status, _, raw = save(other_project, owner["accessToken"], key="outside-project",
                              name="别人的项目里建一本", steps=steps_linear())
        check("不是项目成员：统一按 404 处理，不探测项目是否存在",
              status == 404 and body_json(raw).get("code") == "PROJECT_NOT_FOUND",
              f"{status} {raw[:200]}")
        status, _, raw = save(project, viewer["accessToken"], key="viewer-tries",
                              name="旁观者想改", steps=steps_linear())
        check("viewer 只读：写定义 403，理由说的是角色不够",
              status == 403 and body_json(raw).get("code") == "PROJECT_FORBIDDEN",
              f"{status} {raw[:200]}")
        member_save = ok_call("POST", f"/api/v1/projects/{project}/workflows",
                              token=member["accessToken"], want=201,
                              json_body={"key": "member-wf", "name": "组员起的流程",
                                         "steps": steps_linear(), "expectedVersion": 0})
        check("member 能共享自己的流程：新 key 建第一版，创建者是本人",
              member_save["workflow"]["version"] == 1
              and member_save["workflow"]["createdBy"] == member["profile"]["uid"],
              json.dumps(member_save["workflow"], ensure_ascii=False)[:220])
        member_wf = member_save["workflow"]["id"]
        status, _, raw = http("GET", f"/api/v1/projects/{project}/workflows/{wf_id}",
                              token=stranger["accessToken"])
        check("项目外的人读不到共享定义（连「存在」都不确认）",
              status == 404, f"{status} {raw[:160]}")
        status, _, raw = http("GET", f"/api/v1/projects/{project}/workflows/awf-0000000000000000",
                              token=owner["accessToken"])
        check("合法形状但不存在的 id：404 WORKFLOW_NOT_FOUND",
              status == 404 and body_json(raw).get("code") == "WORKFLOW_NOT_FOUND",
              f"{status} {raw[:160]}")
        status, _, raw = http("GET", f"/api/v1/projects/{project}/workflows/not-an-id",
                              token=owner["accessToken"])
        check("形状就不对的 id 也按 404（不给人「参数错了 vs 不存在」的探测差）",
              status == 404, f"{status} {raw[:160]}")

        print("\n[运行留证：钉哪一版 / 重复上报 / 收口不可逆]")
        run = start_run(project, member["accessToken"], wf_id, state="running",
                        digest=second["digest"], executed=1)
        run_id = run["run"]["id"]
        check("起运行：钉住摘要对应的第 2 版，房间与工单引用按上报原样存",
              run["run"]["workflowVersion"] == 2 and run["run"]["workflowDigest"] == second["digest"]
              and run["run"]["roomRef"] == "s-standup" and run["run"]["ticketRef"] == "s-standup~wf-abc",
              json.dumps(run["run"], ensure_ascii=False)[:260])
        check("起运行：open/needsHuman 由状态算出，不是客户端报什么就信什么",
              run["run"]["open"] is True and run["run"]["needsHuman"] is False
              and run["run"]["stateLabel"] == "在跑", json.dumps(run["run"], ensure_ascii=False)[:200])

        status, _, raw = http("POST", f"/api/v1/projects/{project}/workflows/{wf_id}/runs",
                              token=member["accessToken"],
                              json_body={"title": "幽灵版", "definitionDigest": "f" * 64})
        check("摘要在本项目里查不到（既不是当前版也不在历史里）：拒起运行",
              status == 409 and body_json(raw).get("code") == "WORKFLOW_DIGEST_UNKNOWN",
              f"{status} {raw[:220]}")
        pinned = start_run(project, member["accessToken"], wf_id, state="running",
                           digest=first["digest"], title="钉在历史版")
        check("允许把运行钉在保留的历史版上（跑一半才升版是常态），版本按那一版记",
              pinned["run"]["workflowVersion"] == 1
              and pinned["run"]["workflowDigest"] == first["digest"],
              json.dumps(pinned["run"], ensure_ascii=False)[:220])

        view_steps = [{"id": "start", "kind": "trigger", "state": "succeeded"},
                      {"id": "ask", "kind": "action", "state": "succeeded", "attempt": 1,
                       "output": "三段齐了"}]
        rep = ok_call("POST", f"/api/v1/projects/{project}/workflow-runs/{run_id}/report",
                      token=member["accessToken"],
                      json_body={"state": "awaiting_step", "steps": view_steps, "executed": 2})
        check("上报视图：状态与步视图进账，changed=true",
              rep["changed"] is True and rep["run"]["state"] == "awaiting_step"
              and rep["run"]["executed"] == 2 and len(rep["run"]["steps"]) == 2,
              json.dumps(rep, ensure_ascii=False)[:240])
        before_events = len(events(project, member["accessToken"]))
        dup = ok_call("POST", f"/api/v1/projects/{project}/workflow-runs/{run_id}/report",
                      token=member["accessToken"],
                      json_body={"state": "awaiting_step", "steps": view_steps, "executed": 2})
        after_events = len(events(project, member["accessToken"]))
        check("同一份内容重复上报：changed=false、不重发事件（重试不该长出一条新时间线）",
              dup["changed"] is False and dup["seq"] == 0 and after_events == before_events,
              f"{after_events} vs {before_events}")
        ok_call("POST", f"/api/v1/projects/{project}/workflow-runs/{run_id}/report",
                token=member["accessToken"],
                json_body={"state": "succeeded", "steps": view_steps, "executed": 2})
        status, _, raw = report_run(project, member["accessToken"], run_id, state="running",
                                    steps=view_steps, executed=2)
        check("已收口的运行不许改回未收口：跑完就是跑完，事后改账的口子不能留",
              status == 409 and body_json(raw).get("code") == "WORKFLOW_RUN_CLOSED",
              f"{status} {raw[:220]}")
        closed = ok_call("GET", f"/api/v1/projects/{project}/workflow-runs/{run_id}",
                         token=member["accessToken"])["run"]
        check("收口时 endedAtMs 落下去，open 变假",
              closed["state"] == "succeeded" and int(closed["endedAtMs"]) > 0
              and closed["open"] is False, json.dumps(closed, ensure_ascii=False)[:220])
        status, _, raw = http("POST", f"/api/v1/projects/{project}/workflow-runs/awr-1/report",
                              token=member["accessToken"], json_body={"state": "running"})
        check("上报一条不存在的运行：404，不新建",
              status == 404 and body_json(raw).get("code") == "WORKFLOW_RUN_NOT_FOUND",
              f"{status} {raw[:180]}")
        status, _, raw = report_run(project, viewer["accessToken"], pinned["run"]["id"],
                                    state="running", steps=view_steps)
        check("viewer 不能替运行上报状态（写的是项目内的账）",
              status == 403, f"{status} {raw[:180]}")

        print("\n[人工卡点：只认人 / 同一步一次 / 不推进运行]")
        gate_run = start_run(project, member["accessToken"], wf_id, state="awaiting_approval",
                             digest=second["digest"], title="等人确认那轮", executed=4,
                             auto_passed=2)
        gate_id = gate_run["run"]["id"]
        check("等人确认的运行：needsHuman 为真，自动放行只记在 autoPassed 计数里",
              gate_run["run"]["needsHuman"] is True and gate_run["run"]["autoPassed"] == 2
              and gate_run["run"]["approvals"] == [],
              json.dumps(gate_run["run"], ensure_ascii=False)[:240])

        status, _, raw = approve(project, member["accessToken"], pinned["run"]["id"],
                                 step_id="gate", decision="allow")
        check("运行不在「等人」状态时给结论：409（重复点确认不能再推一步）",
              status == 409 and body_json(raw).get("code") == "WORKFLOW_RUN_NOT_AWAITING",
              f"{status} {raw[:220]}")
        status, _, raw = approve(project, member["accessToken"], gate_id,
                                 step_id="check", decision="allow")
        check("结论落在非卡点步骤：404，不能替别的步给结论",
              status == 404 and body_json(raw).get("code") == "WORKFLOW_STEP_NOT_APPROVAL",
              f"{status} {raw[:220]}")
        status, _, raw = approve(project, member["accessToken"], gate_id,
                                 step_id="gate", decision="skip")
        check("结论第三种答案（skip）：400，这里只有 allow / deny",
              status == 400 and body_json(raw).get("code") == "WORKFLOW_INVALID",
              f"{status} {raw[:200]}")
        yes = ok_call("POST", f"/api/v1/projects/{project}/workflow-runs/{gate_id}/approve",
                      token=member["accessToken"], want=201,
                      json_body={"stepId": "gate", "decision": "allow",
                                 "note": "看完三段，可以发全组",
                                 "question": "这条要不要发给全组？"})
        check("人给的结论独立成行：谁点的、说了什么、问题原文都在",
              yes["approval"]["decision"] == "allow"
              and yes["approval"]["decidedBy"] == member["profile"]["uid"]
              and "可以发全组" in yes["approval"]["note"]
              and yes["approval"]["human"] is True,
              json.dumps(yes["approval"], ensure_ascii=False)[:240])
        check("记账不改运行状态：本层不判定下一步往哪儿走（判定权在引擎）",
              yes["run"]["state"] == "awaiting_approval"
              and yes["run"]["humanPassed"] == 1 and len(yes["run"]["approvals"]) == 1,
              json.dumps(yes["run"], ensure_ascii=False)[:240])
        status, _, raw = approve(project, owner["accessToken"], gate_id,
                                 step_id="gate", decision="deny", note="改口")
        check("同一步第二次给结论：409，不应用也不许改口（要改只能重跑）",
              status == 409 and body_json(raw).get("code") == "WORKFLOW_ALREADY_DECIDED",
              f"{status} {raw[:220]}")
        detail_after = ok_call("GET", f"/api/v1/projects/{project}/workflow-runs/{gate_id}",
                               token=owner["accessToken"])["run"]
        check("被拒的第二次结论没进账：这一步仍只有一条 allow",
              len(detail_after["approvals"]) == 1
              and detail_after["approvals"][0]["decision"] == "allow",
              json.dumps(detail_after["approvals"], ensure_ascii=False)[:220])
        check("自动放行绝不进这张表：approvals 只有一条人给的，autoPassed 仍是 2",
              detail_after["autoPassed"] == 2 and len(detail_after["approvals"]) == 1,
              json.dumps(detail_after, ensure_ascii=False)[:200])

        deny_run = start_run(project, member["accessToken"], wf_id, state="awaiting_approval",
                             digest=second["digest"], title="被拒的那轮")
        denied = ok_call("POST", f"/api/v1/projects/{project}/workflow-runs/{deny_run['run']['id']}/approve",
                         token=owner["accessToken"], want=201,
                         json_body={"stepId": "gate", "decision": "deny", "note": "缺一条来源"})
        check("deny 也是人给的结论：进表、运行仍停在等人",
              denied["approval"]["decision"] == "deny"
              and denied["run"]["state"] == "awaiting_approval",
              json.dumps(denied, ensure_ascii=False)[:220])

        print("\n[定义滚出保留窗口：blocked 必须真落盘]")
        orphan_wf = ok_call("POST", f"/api/v1/projects/{project}/workflows",
                            token=owner["accessToken"], want=201,
                            json_body={"key": "orphan", "name": "会被滚出去的一本",
                                       "steps": steps_full(), "expectedVersion": 0})
        orphan_digest = orphan_wf["digest"]
        orphan_run = start_run(project, member["accessToken"], orphan_wf["workflow"]["id"],
                               state="awaiting_approval", digest=orphan_digest)
        for i in range(workflows.MAX_VERSIONS + 1):
            bumped = steps_full()
            bumped[4]["spec"]["text"] = f"收工通知 {i}"
            status, _, raw = http("POST", f"/api/v1/projects/{project}/workflows",
                                  token=owner["accessToken"],
                                  json_body={"key": "orphan", "name": f"会被滚出去的一本 {i}",
                                             "steps": bumped, "expectedVersion": 1 + i})
            if status != 201:
                raise AssertionError(f"升版第 {i} 次失败：{status} {raw[:200]}")
        status, resp_headers, raw = approve(project, member["accessToken"],
                                            orphan_run["run"]["id"], step_id="gate",
                                            decision="allow")
        check("钉的那一版已滚出窗口时给结论：409 且说明「结论没有应用」",
              status == 409 and body_json(raw).get("code") == "WORKFLOW_DEFINITION_UNAVAILABLE",
              f"{status} {raw[:240]}")
        check("409 的响应头把运行新状态带回去：客户端不用靠再读一次才知道它停成了 blocked",
              resp_headers.get("x-run-state") == "blocked",
              json.dumps(resp_headers, ensure_ascii=False)[:220])
        orphan_after = ok_call("GET", f"/api/v1/projects/{project}/workflow-runs/{orphan_run['run']['id']}",
                               token=member["accessToken"])["run"]
        check("blocked 真落盘（这一条钉住「raise 在事务里会把写回滚掉」那个坑）",
              orphan_after["state"] == "blocked" and orphan_after["open"] is True
              and "不猜一份定义把它推完" in orphan_after["note"],
              json.dumps(orphan_after, ensure_ascii=False)[:260])
        check("停在 blocked 的运行没有一条人给的结论：结论确实没应用",
              orphan_after["approvals"] == [],
              json.dumps(orphan_after["approvals"], ensure_ascii=False)[:160])
        blocked_events = [e for e in events(project, member["accessToken"])
                          if e["kind"] == "workflow.run.updated"
                          and e["payload"].get("reason") == "definition_unavailable"]
        check("blocked 也进了事件流：换一台设备也能看到它为什么停",
              len(blocked_events) >= 1 and blocked_events[-1]["payload"]["runId"] == orphan_run["run"]["id"],
              json.dumps(blocked_events[-1] if blocked_events else {}, ensure_ascii=False)[:220])

        print("\n[归档与无硬删]")
        status, _, raw = http("POST", f"/api/v1/projects/{project}/workflows/{wf_id}/archive",
                              token=owner["accessToken"], json_body={"reason": "停用"})
        check("还挂着未收口运行的本子直接归档：409 并数出几条没收口",
              status == 409 and body_json(raw).get("code") == "WORKFLOW_RUNS_OPEN"
              and "未收口" in str(body_json(raw).get("message")), f"{status} {raw[:240]}")
        archived = ok_call("POST", f"/api/v1/projects/{project}/workflows/{wf_id}/archive",
                           token=owner["accessToken"], want=201,
                           json_body={"reason": "停用", "force": True})
        check("带 force 也只是归档：archived=true、运行记录一条没删",
              archived["workflow"]["archived"] is True
              and archived["workflow"]["runCount"] >= 4 and archived["openRuns"] >= 1,
              json.dumps(archived, ensure_ascii=False)[:260])
        status, _, raw = http("GET", f"/api/v1/projects/{project}/workflow-runs/{gate_id}",
                              token=owner["accessToken"])
        check("归档后仍能按 id 读到那几次运行（「查看每次运行」还在）",
              status == 200 and body_json(raw)["run"]["id"] == gate_id, f"{status} {raw[:180]}")
        status, _, raw = http("POST", f"/api/v1/projects/{project}/workflows/{wf_id}/runs",
                              token=member["accessToken"], json_body={"title": "归档后想起新跑"})
        check("归档的本子不再起新运行：409（已在跑的那些照常记账）",
              status == 409 and body_json(raw).get("code") == "WORKFLOW_ARCHIVED", f"{status} {raw[:200]}")
        plain = ok_call("GET", f"/api/v1/projects/{project}/workflows", token=owner["accessToken"])
        with_arch = ok_call("GET", f"/api/v1/projects/{project}/workflows?includeArchived=true",
                            token=owner["accessToken"])
        check("清单默认不含归档本，includeArchived 才含（历史还在，只是不再被复用）",
              wf_id not in [w["id"] for w in plain["workflows"]]
              and wf_id in [w["id"] for w in with_arch["workflows"]],
              f"{len(plain['workflows'])} vs {len(with_arch['workflows'])}")
        status, _, raw = http("POST", f"/api/v1/projects/{project}/workflows/{orphan_wf['workflow']['id']}/archive",
                              token=member["accessToken"], json_body={"reason": "不是我建的"})
        check("归档别人建的流程：普通成员 403（只有创建者或项目管理员点头）",
              status == 403 and body_json(raw).get("code") == "WORKFLOW_FORBIDDEN", f"{status} {raw[:180]}")
        by_admin = ok_call("POST",
                           f"/api/v1/projects/{project}/workflows/{orphan_wf['workflow']['id']}/archive",
                           token=keeper["accessToken"], want=201, json_body={"reason": "清理", "force": True})
        check("项目管理员（不是创建者）能归档：force 也只是归档，不删运行",
              by_admin["workflow"]["archived"] is True
              and by_admin["workflow"]["runCount"] == 1,
              json.dumps(by_admin, ensure_ascii=False)[:220])
        by_creator = ok_call("POST", f"/api/v1/projects/{project}/workflows/{member_wf}/archive",
                             token=member["accessToken"], want=201, json_body={"reason": "我自己收掉"})
        check("创建者能归档自己那本（不需要管理员身份）",
              by_creator["workflow"]["archived"] is True
              and by_creator["workflow"]["createdBy"] == member["profile"]["uid"],
              json.dumps(by_creator["workflow"], ensure_ascii=False)[:200])

        print("\n[事件流与跨项目「等我」清单]")
        kinds = [e["kind"] for e in events(project, member["accessToken"])]
        for kind in ("workflow.created", "workflow.versioned", "workflow.run.started",
                     "workflow.run.updated", "workflow.approval.decided", "workflow.archived"):
            check(f"事件流里有 {kind}（复用 D-06 的 SSE 与游标，不新开一条流）",
                  kind in kinds, f"{kinds[-12:]}")
        created_event = next(e for e in events(project, member["accessToken"])
                             if e["kind"] == "workflow.created"
                             and e["payload"]["workflow"]["id"] == wf_id)
        check("事件负载只带视图与摘要：步骤正文（brief 全文）不广播",
              BRIEF not in json.dumps(created_event, ensure_ascii=False)
              and created_event["payload"]["workflow"]["stepCount"] == 5
              and "digest" in created_event["payload"]["workflow"],
              json.dumps(created_event, ensure_ascii=False)[:240])
        mine = ok_call("GET", "/api/v1/agent/workflow-runs", token=member["accessToken"])
        check("跨项目「等我」清单：只列需要人动手那一种",
              mine["count"] >= 1 and all(r["state"] == "awaiting_approval" for r in mine["runs"]),
              json.dumps(mine, ensure_ascii=False)[:200])
        check("blocked 不进「等你」那一屏：它等人补的是装配，不是点确认",
              orphan_run["run"]["id"] not in [r["id"] for r in mine["runs"]],
              json.dumps([r["id"] for r in mine["runs"]])[:200])
        all_open = ok_call("GET", "/api/v1/agent/workflow-runs?needsHuman=false",
                           token=owner["accessToken"])
        check("needsHuman=false 时未收口的都在（含 blocked）",
              orphan_run["run"]["id"] in [r["id"] for r in all_open["runs"]]
              and any(r["state"] == "blocked" for r in all_open["runs"]),
              json.dumps([r["state"] for r in all_open["runs"]])[:220])
        outsider = ok_call("GET", "/api/v1/agent/workflow-runs?needsHuman=false",
                           token=stranger["accessToken"])
        check("项目外的人在这张清单里什么都看不见（按成员关系过滤，不是按状态）",
              outsider["count"] == 0, json.dumps(outsider, ensure_ascii=False)[:200])

        print("\n[路由形状：不被市场通配吞掉]")
        status, headers, raw = http("GET", "/api/v1/agent/workflow-runs", token=member["accessToken"])
        check("/api/v1/agent/workflow-runs 是本层的路由（不是被 /api/v1/{plural}/{item_id} 当成未知条目）",
              status == 200 and body_json(raw).get("ok") is True and "runs" in body_json(raw),
              f"{status} {raw[:200]}")
        paths = [getattr(r, "path", "") for r in app.routes]
        market_wildcard = min(i for i, p in enumerate(paths) if p == "/api/v1/{plural}/{item_id}")
        check("注册顺序：本层路由注在市场通配路由之前（顺序一翻，这条两段路径就被吞）",
              paths.index("/api/v1/agent/workflow-runs") < market_wildcard,
              f"{paths.index('/api/v1/agent/workflow-runs')} vs {market_wildcard}")
        check("本层十条路由都在应用里（定义 4 + 运行 5 + 跨项目 1）",
              len([p for p in paths if "workflow" in p]) == 10,
              str([p for p in paths if "workflow" in p]))

        print("\n[上限：靠数据说话，不靠把旧的挤掉]")
        limit_project = create_project(owner["accessToken"], "上限自测项目")
        original_per_project = workflows.MAX_PER_PROJECT
        original_runs = workflows.MAX_RUNS_PER_PROJECT
        try:
            workflows.MAX_PER_PROJECT = 3
            workflows.MAX_RUNS_PER_PROJECT = 2
            for i in range(3):
                status, _, raw = save(limit_project, owner["accessToken"], key=f"cap-{i}",
                                      name=f"一本 {i}", steps=steps_linear())
                if status != 201:
                    raise AssertionError(f"第 {i} 本应该建得起来：{status} {raw[:200]}")
            status, _, raw = save(limit_project, owner["accessToken"], key="cap-overflow",
                                  name="第四本", steps=steps_linear())
            check("定义数到上限：409 且明确「这一本没有落盘」，不挤掉旧的",
                  status == 409 and body_json(raw).get("code") == "WORKFLOW_LIMIT"
                  and "没有落盘" in str(body_json(raw).get("message")), f"{status} {raw[:220]}")
            cap_wf = ok_call("GET", f"/api/v1/projects/{limit_project}/workflows",
                             token=owner["accessToken"])["workflows"][0]["id"]
            for i in range(2):
                start_run(limit_project, owner["accessToken"], cap_wf, title=f"第 {i} 轮")
            status, _, raw = http("POST", f"/api/v1/projects/{limit_project}/workflows/{cap_wf}/runs",
                                  token=owner["accessToken"], json_body={"title": "第三条"})
            check("运行记录到上限：409（「查看每次运行」不能靠丢掉旧几次腾地方）",
                  status == 409 and body_json(raw).get("code") == "WORKFLOW_RUN_LIMIT",
                  f"{status} {raw[:220]}")
        finally:
            workflows.MAX_PER_PROJECT = original_per_project
            workflows.MAX_RUNS_PER_PROJECT = original_runs
        status, _, raw = save(limit_project, owner["accessToken"], key="cap-after",
                              name="恢复上限后的一本", steps=steps_linear())
        check("上限是运行期读的常数：改回去以后同一项目还能建（自测没把状态留在进程里）",
              status == 201, f"{status} {raw[:180]}")
    finally:
        server.should_exit = True
    print(f"\n通过 {_passed} 项")


if __name__ == "__main__":
    main()
