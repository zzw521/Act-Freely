#!/usr/bin/env python3
"""
多专家运行时与每专家模型绑定自测

锁住「专家注册表 → 负责人身份/模型 → 子 Agent 按专家派发 → 结果回到父 Agent」这条链，
防止出现「页面能建专家，任务里从不生效」的假承诺：

1. 注册表归一化：坏结构/缺字段/重复 id 显式报错，不返回空列表
2. 模型解析：绑定配置优先；模型不可用、密钥被剥掉、只有 modelId 时都失败关闭
3. 技能白名单过滤与缺失技能说明
4. /api/chat 端到端（假模型）：负责人提示词与模型真的生效；子 Agent 按 id/名称命中专家，
   各自使用自己的模型与技能白名单；未绑定的专家沿用本轮对话模型；
   未知专家整批拒绝；负责人不存在/无注册表/模型不可用直接拒绝提交
5. 检查点落盘剥掉专家模型密钥并留下标记，恢复时不会拿空密钥调用远端模型

离线运行：不联网、无需 API Key。

用法: python tests/integration/engine_experts_selftest.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import expert_runtime as er
except Exception as exc:
    print(f"SKIP: expert runtime selftest unavailable ({exc})")
    sys.exit(0)

try:
    from fastapi.testclient import TestClient
    import server as engine_server
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.formatter import DashScopeChatFormatter
except Exception as exc:
    print(f"SKIP: 端到端依赖缺失（{exc}）")
    engine_server = None
    ChatModelBase = ChatResponse = DashScopeChatFormatter = None

FAILURES: list = []
RECORDED: list = []
BUILDS: list = []

LEAD_MARK = "## 本轮负责人专家"
CHILD_MARK = "## 专家身份"
SUBAGENT_MARK = "只负责一个被明确指定的子任务"
SUMMARY_MARK = "子任务结果汇总"
S1_MARK = "技能一：财务口径核对"
S2_MARK = "技能二：代码质量审查"
GEN_MARK = "技能三：资料整理"
TURN_MODEL = "turn-model"
LEAD_MODEL = "lead-model"
CODE_MODEL = "code-model"

_SCENARIO = {"name": "routing"}


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _blob(messages) -> str:
    try:
        return json.dumps(messages, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(messages)


def _registry() -> dict:
    return {
        "persisted": True,
        "items": [
            {
                "id": "lead-fin",
                "name": "财务分析师",
                "description": "负责财务口径核对与成本结构拆解",
                "prompt": "先核对财报口径，再给出可复核的计算过程",
                "modelId": "m-lead",
                "modelName": LEAD_MODEL,
                "modelConfig": {"modelName": LEAD_MODEL, "apiKey": "lead-secret", "baseUrl": "http://127.0.0.1:1/v1"},
                "skills": ["s1"],
            },
            {
                "id": "child-code",
                "name": "代码审查员",
                "description": "负责代码质量与安全审查",
                "prompt": "按严重程度列出问题，并给出修复建议",
                "modelId": "m-code",
                "modelName": CODE_MODEL,
                "modelConfig": {"modelName": CODE_MODEL, "apiKey": "code-secret", "baseUrl": "http://127.0.0.1:1/v1"},
                "skills": ["s2"],
            },
            {
                "id": "child-general",
                "name": "资料整理员",
                "description": "负责把材料整理成结构化要点",
                "prompt": "只整理材料，不扩写",
                "modelId": "",
                "modelName": "",
                "skills": ["s1", "s-disabled"],
            },
            {
                "id": "bad-model",
                "name": "失联专家",
                "description": "绑定模型已被删除",
                "prompt": "按专业标准执行",
                "modelId": "m-gone",
                "modelName": "",
                "modelError": "绑定的模型已不存在，请重新选择",
                "skills": [],
            },
        ],
    }


def _runtime() -> dict:
    return {
        "skills": [
            {"id": "s1", "name": "Skill One", "description": S1_MARK, "content": "正文", "kind": "skill", "source": "installed"},
            {"id": "s2", "name": "Skill Two", "description": S2_MARK, "content": "正文", "kind": "skill", "source": "installed"},
            {"id": "s3", "name": "Skill Three", "description": GEN_MARK, "content": "正文", "kind": "skill", "source": "installed"},
        ],
        "experts": _registry(),
    }


# ── 1. 纯函数层 ────────────────────────────────────────────────

def pure_checks() -> None:
    print("--- 1. 注册表归一化与模型解析（纯函数层） ---")

    items, err = er.normalize_registry(_registry())
    check("合法注册表归一化成功", err is None and len(items) == 4, str(err))
    check("id 统一小写、技能去重", items[0]["id"] == "lead-fin" and items[0]["skills"] == ["s1"], str(items[0]))

    _, err = er.normalize_registry({"items": [{"id": "a"}]})
    check("缺 name/description/prompt 显式报错", err is not None and "缺少" in err, str(err))

    _, err = er.normalize_registry({"items": [_registry()["items"][0], _registry()["items"][0]]})
    check("重复 id 显式报错", err is not None and "重复 id" in err, str(err))

    _, err = er.normalize_registry({"error": "EXPERTS_CORRUPT: 坏 JSON"})
    check("注册表损坏原因透传，不假装没有专家", err is not None and "EXPERTS_CORRUPT" in err, str(err))

    _, err = er.normalize_registry({"items": "not-list"})
    check("items 不是数组时报错", err is not None and "items" in err, str(err))

    lead = er.find_expert(items, "LEAD-FIN")
    check("按 id 大小写不敏感命中", lead is not None and lead["name"] == "财务分析师")
    check("按名称命中专家", (er.find_expert(items, "资料整理员") or {}).get("id") == "child-general")
    check("未知专家解析为 None", er.find_expert(items, "expert-missing") is None)

    cfg, err = er.model_config_of(items[0])
    check("绑定的模型配置直接可用", err is None and cfg["modelName"] == LEAD_MODEL, str(err))

    cfg, err = er.model_config_of(items[3])
    check("modelError 专家失败关闭", cfg is None and "绑定的模型不可用" in (err or ""), str(err))

    _, err = er.model_config_of({
        "name": "磁盘恢复", "modelId": "m1", "modelConfig": {"modelName": "m1"},
        "modelSecretsStripped": True,
    })
    check("密钥被剥掉的专家失败关闭（不拿空密钥调用远端）",
          err is not None and "检查点" in err, str(err))

    _, err = er.model_config_of({
        "name": "只有 id", "modelId": "m1", "modelConfig": None, "modelError": "",
    })
    check("只有 modelId 没有配置时失败关闭", err is not None and "没有下发" in err, str(err))

    hint = er.lead_system_hint(items[0])
    check("负责人提示词包含身份、职责与安全优先约束",
          LEAD_MARK in hint and "财务口径" in hint and "安全边界" in hint, hint)
    child_hint = er.child_role_prompt(items[1])
    check("子 Agent 提示词包含专家身份且限定子任务范围",
          CHILD_MARK in child_hint and "只负责被明确指派的子任务" in child_hint, child_hint)

    skills = [
        {"id": "s1", "name": "a"}, {"id": "s2", "name": "b"}, {"id": "s3", "name": "c"},
    ]
    kept, missing = er.filter_skills(skills, items[2])
    check("技能白名单只保留配置项并摊开缺失项",
          [s["id"] for s in kept] == ["s1"] and missing == ["s-disabled"],
          f"kept={kept}, missing={missing}")
    kept, missing = er.filter_skills(skills, None)
    check("未配置白名单时不限制技能", len(kept) == 3 and missing == [], str(kept))

    notes = er.expert_notes(items[2], ["s-disabled"])
    check("专家结果说明包含模型来源与缺失技能",
          any("未绑定模型" in n for n in notes) and any("s-disabled" in n for n in notes), str(notes))

    # 检查点入库：密钥剥掉 + 标记；恢复时 model_config_of 必须报错
    stripped = engine_server._runtime_for_disk(_runtime())
    dumped = json.dumps(stripped, ensure_ascii=False)
    check("检查点不写专家模型密钥",
          "lead-secret" not in dumped and "code-secret" not in dumped, dumped[:300])
    restored, _ = er.normalize_registry(stripped["experts"])
    check("检查点保留了专家身份但标记密钥已剥离",
          restored[0]["name"] == "财务分析师" and restored[0]["modelSecretsStripped"] is True,
          str(restored[0]))
    _, err = er.model_config_of(restored[0])
    check("从检查点恢复的专家不能按旧配置调用模型", err is not None and "检查点" in err, str(err))

    session = engine_server.TaskSession("expert-checkpoint")
    session.expert_id = "lead-fin"
    check("检查点上下文记录负责人专家",
          engine_server._checkpoint_context(session).get("expertId") == "lead-fin")


# ── 2. /api/chat 端到端（假模型）───────────────────────────────

class ScriptedExpertModel(ChatModelBase):
    """负责人：先派发两个带专家的子任务，看到汇总后收尾；子 Agent：直接给结论。"""

    def __init__(self, index: int):
        super().__init__(model_name="scripted-expert-model", stream=False)
        self.index = index

    async def __call__(self, messages, *args, **kwargs):
        blob = _blob(messages)
        RECORDED.append({"index": self.index, "blob": blob})
        if LEAD_MARK in blob:
            return self._parent(blob)
        if CHILD_MARK in blob or SUBAGENT_MARK in blob:
            return ChatResponse(content=[{"type": "text", "text": f"子任务完成（call {self.index}）"}])
        return ChatResponse(content=[{"type": "text", "text": "未知身份，直接收尾。"}])

    def _parent(self, blob: str):
        if SUMMARY_MARK in blob or "错误：" in blob:
            return ChatResponse(content=[{"type": "text", "text": "负责人已核对子任务结果并整合。"}])
        if _SCENARIO["name"] == "unknown-expert":
            tasks = [{"title": "未知专家任务", "instruction": "交给不存在的专家", "expert": "expert-missing"}]
        elif _SCENARIO["name"] == "plain-child":
            tasks = [{"title": "普通子任务", "instruction": "只整理材料，不指定专家"}]
        else:
            tasks = [
                {"title": "代码审查", "instruction": "审查 src/main.rs 的错误处理", "expert": "child-code"},
                {"title": "资料整理", "instruction": "把会议记录整理成要点", "expert": "资料整理员"},
            ]
        return ChatResponse(content=[{
            "type": "tool_use",
            "id": "call-spawn-experts",
            "name": "spawn_subagents",
            "input": {"tasks_json": json.dumps(tasks, ensure_ascii=False)},
        }])


def _fake_build(model_override=None, model_config=None, **kwargs):
    BUILDS.append({"binding": model_override, "config": model_config})
    return ScriptedExpertModel(len(BUILDS) - 1), DashScopeChatFormatter()


def _builds_for(mark: str) -> list:
    """按模型实际收到的提示词，把构建调用与「是谁」对上号。"""
    out = []
    for rec in RECORDED:
        if mark in rec["blob"]:
            out.append(BUILDS[rec["index"]])
    return out


def _model_names(builds: list) -> list:
    return [str((b.get("config") or {}).get("modelName") or b.get("binding") or "") for b in builds]


def _reset(scenario: str = "routing") -> None:
    RECORDED.clear()
    BUILDS.clear()
    _SCENARIO["name"] = scenario


def e2e_checks() -> None:
    print("--- 2. /api/chat 端到端（负责人 → 专家子 Agent） ---")
    engine_server.build_model_and_formatter = _fake_build
    work_dir = Path(tempfile.mkdtemp(prefix="tt-experts-http-"))
    engine_server.WORK_DIR = work_dir
    client = TestClient(engine_server.app)
    try:
        _reset()
        turn_config = {"modelName": TURN_MODEL, "apiKey": "turn-secret", "baseUrl": "http://127.0.0.1:1/v1"}
        resp = client.post("/api/chat", json={
            "message": "核对本季度成本并审查相关代码",
            "task_id": "experts-routing",
            "expertId": "lead-fin",
            "modelConfig": turn_config,
            "runtime": _runtime(),
        })
        body = resp.text
        parents = [r for r in RECORDED if LEAD_MARK in r["blob"]]
        children = [r for r in RECORDED if CHILD_MARK in r["blob"] or SUBAGENT_MARK in r["blob"]]

        check("任务正常收尾", "event: done" in body and "event: error" not in body, body[:400])
        check("负责人提示词真的进入父 Agent（身份与职责）",
              any("财务分析师" in r["blob"] and "先核对财报口径" in r["blob"] for r in parents),
              str(len(parents)))
        check("负责人使用专家绑定模型，而不是对话里选的模型",
              set(_model_names(_builds_for(LEAD_MARK))) == {LEAD_MODEL},
              str(_model_names(_builds_for(LEAD_MARK))))
        check("没有把对话模型配置用在负责人身上", TURN_MODEL not in _model_names(_builds_for(LEAD_MARK)),
              str(_model_names(_builds_for(LEAD_MARK))))

        code_child = [r for r in children if "代码审查员" in r["blob"]]
        general_child = [r for r in children if "资料整理员" in r["blob"]]
        check("按 id 派发的子 Agent 带上了专家身份与角色提示词",
              len(code_child) >= 1 and "## 专家身份：代码审查员" in code_child[0]["blob"]
              and "按严重程度列出问题" in code_child[0]["blob"],
              str(code_child[:1]))
        check("按名称派发的子 Agent 也命中专家", len(general_child) >= 1, str(len(general_child)))
        check("代码审查子 Agent 使用自己的绑定模型",
              set(_model_names([BUILDS[r["index"]] for r in code_child])) == {CODE_MODEL},
              str(_model_names([BUILDS[r["index"]] for r in code_child])))
        check("未绑定模型的专家沿用本轮对话模型",
              set(_model_names([BUILDS[r["index"]] for r in general_child])) == {TURN_MODEL},
              str(_model_names([BUILDS[r["index"]] for r in general_child])))

        check("专家技能白名单生效：代码审查员只带技能二",
              all(S2_MARK in r["blob"] and S1_MARK not in r["blob"] and GEN_MARK not in r["blob"]
                  for r in code_child), str([S1_MARK in r["blob"] for r in code_child]))
        check("专家技能白名单生效：资料整理员只带技能一",
              all(S1_MARK in r["blob"] and S2_MARK not in r["blob"] and GEN_MARK not in r["blob"]
                  for r in general_child), str([S2_MARK in r["blob"] for r in general_child]))

        check("子 Agent 步骤进事件流并标注专家",
              body.count('"stepType": "subagent"') >= 2
              and '"expertId": "child-code"' in body and '"expertName": "资料整理员"' in body,
              body[:600])
        check("父 Agent 收到汇总，含专家名与缺失技能说明",
              any(SUMMARY_MARK in r["blob"] and "专家 代码审查员" in r["blob"]
                  and "s-disabled" in r["blob"] for r in parents),
              str([SUMMARY_MARK in r["blob"] for r in parents]))

        # 未知专家：整批拒绝，不静默退回通用子 Agent
        _SCENARIO["name"] = "unknown-expert"
        _reset("unknown-expert")
        resp = client.post("/api/chat", json={
            "message": "把任务交给不存在的专家",
            "task_id": "experts-unknown-child",
            "expertId": "lead-fin",
            "modelConfig": turn_config,
            "runtime": _runtime(),
        })
        body = resp.text
        check("子任务指定未知专家时整批拒绝并列出可用专家",
              "不存在" in "\n".join(r["blob"] for r in RECORDED)
              and "可用专家" in "\n".join(r["blob"] for r in RECORDED),
              body[:500])
        check("整批拒绝后没有子 Agent 真的启动",
              not any(CHILD_MARK in r["blob"] or SUBAGENT_MARK in r["blob"] for r in RECORDED),
              str(len(RECORDED)))
        check("未知专家场景仍正常收尾", "event: done" in body and "event: error" not in body, body[-300:])

        # 不指定专家：通用子 Agent 继承本轮对话模型
        _SCENARIO["name"] = "plain-child"
        _reset("plain-child")
        client.post("/api/chat", json={
            "message": "不指定专家，普通派发",
            "task_id": "experts-plain-child",
            "expertId": "lead-fin",
            "modelConfig": turn_config,
            "runtime": _runtime(),
        })
        plain_children = [
            r for r in RECORDED if SUBAGENT_MARK in r["blob"] and CHILD_MARK not in r["blob"]
        ]
        check("通用子 Agent 不继承负责人专家身份",
              plain_children and all(LEAD_MARK not in r["blob"] for r in plain_children),
              str(len(plain_children)))
        check("通用子 Agent 使用本轮对话模型",
              [str((BUILDS[r["index"]].get("config") or {}).get("modelName")) for r in plain_children]
              == [TURN_MODEL],
              str([BUILDS[r["index"]].get("config") for r in plain_children]))

        # 提交阶段拒绝：不存在 / 无注册表 / 绑定模型不可用
        resp = client.post("/api/chat", json={
            "message": "使用不存在的负责人",
            "task_id": "experts-lead-missing",
            "expertId": "expert-missing",
            "runtime": _runtime(),
        })
        payload = resp.json()
        check("负责人不存在时提交即拒绝",
              resp.status_code == 404 and payload.get("code") == "EXPERT_NOT_FOUND",
              f"{resp.status_code} {payload}")

        resp = client.post("/api/chat", json={
            "message": "没有携带注册表",
            "task_id": "experts-no-registry",
            "expertId": "lead-fin",
            "runtime": {"skills": []},
        })
        payload = resp.json()
        check("没有注册表时提交即拒绝，不静默当通用助手",
              resp.status_code == 400 and payload.get("code") == "EXPERT_UNAVAILABLE",
              f"{resp.status_code} {payload}")

        resp = client.post("/api/chat", json={
            "message": "负责人模型不可用",
            "task_id": "experts-bad-model",
            "expertId": "bad-model",
            "runtime": _runtime(),
        })
        payload = resp.json()
        check("负责人绑定模型不可用时提交即拒绝",
              resp.status_code == 400 and payload.get("code") == "EXPERT_UNAVAILABLE"
              and "绑定的模型不可用" in payload.get("message", ""),
              f"{resp.status_code} {payload}")
    finally:
        engine_server.WORK_DIR = Path(".")
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> None:
    prev_home = os.environ.get("TONGTIANWORK_HOME")
    temp_home = Path(tempfile.mkdtemp(prefix="tt-experts-home-"))
    os.environ["TONGTIANWORK_HOME"] = str(temp_home)
    try:
        pure_checks()
        if engine_server is not None and ChatModelBase is not None:
            e2e_checks()
        else:
            print("SKIP: 端到端分组未运行（依赖缺失）")
    finally:
        if prev_home is None:
            os.environ.pop("TONGTIANWORK_HOME", None)
        else:
            os.environ["TONGTIANWORK_HOME"] = prev_home
        shutil.rmtree(temp_home, ignore_errors=True)
    print("-" * 52)
    if FAILURES:
        print(f"engine experts selftest: {len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("engine experts selftest: ALL PASS")


if __name__ == "__main__":
    main()
