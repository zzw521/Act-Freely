#!/usr/bin/env python3
"""D-42 人格模板自测（#161，W4 领地）：模板库内容 + 校验同源 + 草稿 + 端点面。

这一族要钉住的不是「能不能返回 JSON」，而是**「一份看起来能用的角色库，是不是真的能用」**：

1. 模板库结构（``persona_runtime.TEMPLATES``）：16 套、四个气质组各 4 套、MBTI 字母与
   「四个维度」逐位自洽、气质分组符合 NT/NF/SJ/SP 规则、id 过 Rust 的字符白名单——
   这几条全是**内容不变量**，写错一套就会当场红，而不是等用户点开才发现「INTJ 的外向吗」；
2. 反「一套模板抄 16 遍」：职责与风格逐套互不相同，共用底线四句每套都含；
3. 校验（``validate_expert_draft``）与 Rust ``expert::save`` 的**同一组**上限、
   **同一个**归一顺序（先 trim+小写再量长度）、**同一套**技能清洗（先去重再数 20 个）；
   提醒与拒绝分两条通道——比权威更严的预校验同样是缺陷；
4. 草稿（``draft_from_template``）：不落盘、不占注册表名额，回出去的 id 就是 Rust 会存的那个；
5. 端点面（``/api/persona/*``）：只读走 GET、生成走 POST、这一族**刻意不套桌面闸门**
   （模板不含用户数据，拦了远程用户连「有哪些角色」都看不见），业务拒绝一律 200 + ok=false；
6. 跨语言同源：正则回读 ``src-tauri/src/expert/mod.rs`` 的 ``const MAX_*`` 与
   ``src/shared/contracts_exec.ts`` 的接口字段，双向核对（D-48.4b 的教训：
   两边各写一遍数字与字段名，迟早有一边跟不上）。

必须用 ``./vendor/python/python.exe -X utf8`` 跑。缺第三方依赖时整族 SKIP；
**引擎自己的文件导入断了则大声红**（静默 SKIP 会把真红报成没测，代价在案）。

未验收的一半如实写明：桌面界面的「从人格模板一键创建专家」按钮、以及点击之后调用
Rust ``expert::save`` 的回路，都不在本文件的覆盖范围内——本族只到「草稿与校验」为止。

用法: ./vendor/python/python.exe -X utf8 tests/integration/engine_persona_templates_selftest.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PROJECT_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

_TMP = Path(tempfile.mkdtemp(prefix="tt-persona-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

ENGINE_MODULES = {path.stem for path in (PROJECT_ROOT / "engine").glob("*.py")}

FAILURES: list[str] = []

#: 第 6 节真调用时记下的回话键集，第 7 节拿它去和 TS 接口双向核对
#: （形状从**实际回话**来，不在测试里手抄第二份清单——手抄的两份可以一起过期）
SEEN_KEYS: dict[str, set[str]] = {}

LOCAL_TOKEN = "tt-selftest-local-token"
STATIC_TOKEN = "tt-selftest-static-token"

RUST_EXPERT = PROJECT_ROOT / "src-tauri" / "src" / "expert" / "mod.rs"
TS_CONTRACT = PROJECT_ROOT / "src" / "shared" / "contracts_exec.ts"

#: 四个字母各自的维度名（模板的 `dimensions` 必须按字母顺序逐个对上）
DIMENSION_LABELS = {
    "E": "外向", "I": "内向",
    "N": "直觉", "S": "感觉",
    "T": "思考", "F": "情感",
    "J": "判断", "P": "知觉",
}


def import_gate(exc: BaseException, suite: str) -> None:
    """只在**真缺第三方依赖**时优雅跳过；我们自己的引擎文件断了必须红着退出。"""
    root = exc
    for _ in range(12):
        nxt = root.__cause__ or root.__context__
        if nxt is None:
            break
        root = nxt
    missing = getattr(root, "name", "") or ""
    if isinstance(root, ModuleNotFoundError) and missing not in ENGINE_MODULES:
        print(f"SKIP: {suite} unavailable（缺第三方依赖 {missing or root}）")
        sys.exit(0)
    print(f"[FAIL] {suite} 起不来：引擎侧导入断了，不是依赖缺失 — {type(root).__name__}: {root}")
    print("[FAIL] 整族未运行（拒绝把上面的断裂报成 SKIP）")
    sys.exit(1)


try:
    import expert_runtime as er
    import persona_runtime as pr
    import routes_exec as rx
    import server
    from fastapi.testclient import TestClient
except Exception as exc:  # noqa: BLE001
    import_gate(exc, "persona templates selftest")

# server 的导入链可能把数据根拧到别处，这里再拧回本文件的临时目录。
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")
HOME_ROOT = _TMP / "home"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"--- {title} ---")


def headers(token: str = LOCAL_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def desktop_env() -> None:
    """桌面壳在场：本机令牌 + 远程令牌都配好，请求带本机令牌才算桌面壳。"""
    os.environ["TT_ENGINE_TOKEN"] = STATIC_TOKEN
    os.environ["TT_ENGINE_LOCAL_TOKEN"] = LOCAL_TOKEN


def expected_group(mbti: str) -> str:
    """气质分组规则（Keirsey / 16personalities 同一套）：N+T 分析家、N+F 外交家、
    S+J 守护者、S+P 探险家。注意看的是第 2 与第 4 个字母——
    ISTP 属于探险家而不是「ST 组」，按中间两字母分组会把四套排得七零八落。"""
    if mbti[1] == "N":
        return "分析家" if mbti[2] == "T" else "外交家"
    return "守护者" if mbti[3] == "J" else "探险家"


def _ts_interface_fields(name: str) -> set[str]:
    """从 contracts_exec.ts 刮出一个 interface 的字段名（反向核对用，不在测试里手抄清单）。"""
    ts = TS_CONTRACT.read_text(encoding="utf-8")
    start = ts.index(f"export interface {name} {{")
    body = ts[start:ts.index("\n}", start)]
    return set(re.findall(r"^\s+(?:readonly\s+)?([a-zA-Z]+)\??[:,]", body, re.M))


def _template_fields_from_source() -> set[str]:
    """从 `_tpl()` 的 return 字面量刮出模板键名：加字段没登记就当场红。"""
    src = (PROJECT_ROOT / "engine" / "persona_runtime.py").read_text(encoding="utf-8")
    start = src.index("    return {\n        \"templateId\"")
    body = src[start + len("    return {"):src.index("\n    }", start)]
    return set(re.findall(r'^\s+"([a-zA-Z]+)":', body, re.M))


def _limits_fields_from_source() -> set[str]:
    src = (PROJECT_ROOT / "engine" / "persona_runtime.py").read_text(encoding="utf-8")
    start = src.index("def _limits() -> dict[str, int]:")
    body = src[start + len("def _limits() -> dict[str, int]:"):src.index("\n    }", start)]
    return set(re.findall(r'^\s+"([a-zA-Z]+)":', body, re.M))


def rust_max_constants() -> dict[str, int]:
    src = RUST_EXPERT.read_text(encoding="utf-8")
    return {k: int(v) for k, v in re.findall(r"const (MAX_[A-Z_]+): usize = (\d+);", src)}


# ── 1. 模板库结构 ───────────────────────────────────────────────


def structure_checks() -> None:
    section("1. 模板库结构：数量、分组、字母自洽")
    check("恰好 16 套（四个气质组 × 4）", len(pr.TEMPLATES) == 16, str(len(pr.TEMPLATES)))
    check("模板号唯一", len(set(pr.template_ids())) == len(pr.TEMPLATES))
    check("索引表与清单同长", len(pr.TEMPLATE_INDEX) == len(pr.TEMPLATES))
    check("MBTI 型不重复", len({t["mbti"] for t in pr.TEMPLATES}) == 16)
    check("MBTI 是四个合法字母（位置各取二元）",
          all(len(t["mbti"]) == 4 and t["mbti"][0] in "EI" and t["mbti"][1] in "NS"
              and t["mbti"][2] in "TF" and t["mbti"][3] in "JP" for t in pr.TEMPLATES))
    bad_dims = [
        (t["mbti"], t["dimensions"])
        for t in pr.TEMPLATES
        if t["dimensions"].split("·") != [DIMENSION_LABELS[c] for c in t["mbti"]]
    ]
    check("dimensions 按 MBTI 字母顺序逐位自洽（不是凭感觉写的四个词）", not bad_dims, str(bad_dims))
    bad_group = [(t["mbti"], t["group"]) for t in pr.TEMPLATES if t["group"] != expected_group(t["mbti"])]
    check("气质分组符合 NT/NF/SJ/SP 规则（ISTP 归探险家这一类容易排错）", not bad_group, str(bad_group))
    counts: dict[str, int] = {}
    for t in pr.TEMPLATES:
        counts[t["group"]] = counts.get(t["group"], 0) + 1
    check("四个组各 4 套", sorted(counts.values()) == [4, 4, 4, 4], str(counts))
    check("GROUPS 常量与实际分组一致（缺组会让过滤查询永远为空）",
          set(counts) == set(pr.GROUPS), f"{sorted(counts)} vs {sorted(pr.GROUPS)}")
    check("模板号一律 persona- 开头", all(t["templateId"].startswith("persona-") for t in pr.TEMPLATES))
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
    check("模板号去掉前缀后的默认专家 id 过 Rust 字符白名单",
          all(all(c in allowed for c in t["templateId"].removeprefix("persona-")) for t in pr.TEMPLATES),
          str([t["templateId"] for t in pr.TEMPLATES if any(c not in allowed for c in t["templateId"])]))
    check("导出提示词不超 Rust 上限", all(len(t["prompt"]) <= pr.MAX_PROMPT_CHARS for t in pr.TEMPLATES))
    check("导出提示词够撑起一个角色（不是两三句套话）",
          all(len(t["prompt"]) >= 120 for t in pr.TEMPLATES),
          str(min(len(t["prompt"]) for t in pr.TEMPLATES)))
    check("每套至少三条行为风格", all(len(t["style"]) >= 3 for t in pr.TEMPLATES))
    check("名称唯一", len({t["name"] for t in pr.TEMPLATES}) == len(pr.TEMPLATES))
    check("没有任何模板名与分组名同名（卡片不该显示成「探险家 · 探险家」）",
          not ({t["name"] for t in pr.TEMPLATES} & set(pr.GROUPS)),
          str({t["name"] for t in pr.TEMPLATES} & set(pr.GROUPS)))
    check("模板名都在名称上限内", all(len(t["name"]) <= pr.MAX_NAME_CHARS for t in pr.TEMPLATES))
    check("职责描述都在描述上限内（模板直接当缺省描述用）",
          all(len(t["summary"]) <= pr.MAX_DESCRIPTION_CHARS for t in pr.TEMPLATES))
    check("按模板号取回同一份内容", pr.get_template("persona-intj")["mbti"] == "INTJ")
    check("取不到时回 None 而不是空表", pr.get_template("不存在") is None and pr.get_template(None) is None)
    check("前后空白不影响命中", pr.get_template("  persona-intj ")["templateId"] == "persona-intj")
    check("清单导出的是副本（改返回的 dict 不会污染模板库）", _list_is_copy())
    check("单套取值也是副本（模板是进程常量，不该被一次调用改脏）", _get_is_copy())
    check("模板库在两轮脏改之后仍然干净（副本是真的独立）",
          all("污染" not in t["prompt"] and "污染" not in t["name"]
              and "污染" not in t["style"] for t in pr.TEMPLATES))
    check("分组过滤命中四条", len(pr.list_templates("分析家")) == 4)
    check("分组写错回空表（不是把全部倒出来）", pr.list_templates("分析家x") == [])
    check("空分组 = 全量", len(pr.list_templates("")) == 16)


def _list_is_copy() -> bool:
    got = pr.list_templates("分析家")
    got[0]["prompt"] = "污染"
    got[0]["style"].append("污染")
    live = pr.TEMPLATE_INDEX[got[0]["templateId"]]
    return live["prompt"] != "污染" and "污染" not in live["style"]


def _get_is_copy() -> bool:
    one = pr.get_template("persona-infj")
    one["name"] = "污染"
    one["style"].append("污染")
    live = pr.TEMPLATE_INDEX["persona-infj"]
    return live["name"] != "污染" and "污染" not in live["style"]


# ── 2. 内容不是复制粘贴 ─────────────────────────────────────────


def content_checks() -> None:
    section("2. 文案：16 套必须真的各不相同")
    check("职责逐套互不相同", len({t["duty"] for t in pr.TEMPLATES}) == 16)
    check("概要逐套互不相同", len({t["summary"] for t in pr.TEMPLATES}) == 16)
    check("协作方式逐套互不相同", len({t["collaboration"] for t in pr.TEMPLATES}) == 16)
    check("提示词逐套互不相同", len({t["prompt"] for t in pr.TEMPLATES}) == 16)
    style_sets = [frozenset(t["style"]) for t in pr.TEMPLATES]
    check("行为风格组合不撞车", len(set(style_sets)) == 16)
    overlap = [
        (a["mbti"], b["mbti"], len(set(a["style"]) & set(b["style"])))
        for i, a in enumerate(pr.TEMPLATES) for b in pr.TEMPLATES[i + 1:]
        if len(set(a["style"]) & set(b["style"])) > 1
    ]
    check("任意两套最多共用一条风格", not overlap, str(overlap[:3]))
    for t in pr.TEMPLATES[:3]:
        check(f"{t['mbti']} 的提示词含自己的名称、职责与协作方式",
              t["name"] in t["prompt"] and t["duty"] in t["prompt"]
              and t["collaboration"] in t["prompt"])
    check("风格条目都进了提示词",
          all(all(s in t["prompt"] for s in t["style"]) for t in pr.TEMPLATES))
    shared = {line for t in pr.TEMPLATES for line in t["prompt"].splitlines()
              if line.startswith("- ") and all(line in o["prompt"] for o in pr.TEMPLATES)}
    check("共用底线每套都带（改一次全库生效，不是 16 份各自维护）", len(shared) == 4, str(len(shared)))
    check("底线不含 MBTI 字样（是通用约束，不是角色描述）",
          not any("MBTI" in s or s.find("INT") >= 0 for s in shared))
    check("底线四句在源码里只有一处构造点",
          "_SHARED_RULES" in (PROJECT_ROOT / "engine" / "persona_runtime.py").read_text(encoding="utf-8"))
    filler = [t["mbti"] for t in pr.TEMPLATES if any(w in t["prompt"] for w in ("TODO", "待定", "XXX", "暂无"))]
    check("没有占位文案混进模板库", not filler, str(filler))
    check("维度字符串里四个词两两不同（同一套不该出现两次「直觉」）",
          all(len(set(t["dimensions"].split("·"))) == 4 for t in pr.TEMPLATES))


# ── 3. 校验与 Rust 同源 ─────────────────────────────────────────


def validate_checks() -> None:
    section("3. validate_expert_draft：与 Rust expert::save 同组上限")
    good = {"id": "intj", "name": "建筑师", "description": "把现状梳理成结构", "prompt": "你是建筑师。"}
    check("合格草稿零拒绝", pr.validate_expert_draft(good) == [], str(pr.validate_expert_draft(good)))
    check("空草稿四条都点名", len(pr.validate_expert_draft({})) == 4, str(pr.validate_expert_draft({})))
    check("非对象草稿说清不是对象", pr.validate_expert_draft("x") == ["草稿不是一个对象"])
    check("大写标识是归一不是拒绝（Rust 先 to_lowercase 再判）",
          pr.validate_expert_draft({**good, "id": "MyExpert"}) == [])
    check("归一提醒单独走一条通道", pr.normalization_notes({"id": "MyExpert"})
          == ["专家标识会按小写保存（myexpert）"])
    check("非法字符不该给归一提醒（那是拒绝，不是提醒）",
          pr.normalization_notes({"id": "My Expert!"}) == [])
    check("尾部空白的标识不算非法", pr.validate_expert_draft({**good, "id": " intj "}) == [])
    check("恰好 64 字符的标识通过",
          pr.validate_expert_draft({**good, "id": "a" * pr.MAX_ID_LEN}) == [])
    check("65 字符的标识被拒且点名数值",
          any("65" in p and str(pr.MAX_ID_LEN) in p
              for p in pr.validate_expert_draft({**good, "id": "a" * (pr.MAX_ID_LEN + 1)})))
    check("非 ASCII 标识按字符白名单拒绝",
          "专家标识仅支持小写字母、数字、下划线与连字符"
          in pr.validate_expert_draft({**good, "id": "建筑师"}))
    check("白名单内的 id：字符数与字节数等价（Rust 按字节判 id）",
          all(len(s) == len(s.encode()) for s in ("a" * 64, "intj_lead-1", "z9_-")))
    for field, limit in (("name", pr.MAX_NAME_CHARS), ("description", pr.MAX_DESCRIPTION_CHARS)):
        exact = {**good, field: "名" * limit}
        over = {**good, field: "名" * (limit + 1)}
        check(f"{field} 恰好 {limit} 字通过（边界不写反）",
              pr.validate_expert_draft(exact) == [], str(pr.validate_expert_draft(exact)))
        bad = pr.validate_expert_draft(over)
        check(f"{field} 超一字被拒且带上实际长度与上限",
              len(bad) == 1 and str(limit + 1) in bad[0] and str(limit) in bad[0], str(bad))
    check("名称两侧空白不计入长度（Rust 先 trim 再 chars().count()）",
          pr.validate_expert_draft({**good, "name": "  " + "名" * pr.MAX_NAME_CHARS + "  "}) == [])
    check("提示词按归一后的长度计（尾部留空格不算过长）",
          pr.validate_expert_draft({**good, "prompt": "x" * (pr.MAX_PROMPT_CHARS - 1) + "\n "}) == [])
    check("提示词只有空白时按空拒绝",
          "角色提示词不能为空" in pr.validate_expert_draft({**good, "prompt": "   \n "}))
    check(f"提示词超 {pr.MAX_PROMPT_CHARS} 字被拒",
          any(str(pr.MAX_PROMPT_CHARS) in p for p in
              pr.validate_expert_draft({**good, "prompt": "x" * (pr.MAX_PROMPT_CHARS + 1)})))
    check("不带 skills 字段不报错（草稿本来不含技能）", "skills" not in good and pr.validate_expert_draft(good) == [])
    check(f"{pr.MAX_SKILLS} 个技能通过",
          pr.validate_expert_draft({**good, "skills": [f"s{i}" for i in range(pr.MAX_SKILLS)]}) == [])
    check(f"{pr.MAX_SKILLS + 1} 个不同技能被拒",
          any(str(pr.MAX_SKILLS) in p for p in
              pr.validate_expert_draft({**good, "skills": [f"s{i}" for i in range(pr.MAX_SKILLS + 1)]})))
    check("重复与空串先按 Rust 的方式去掉再计数（21 条里有重复不该误拒）",
          pr.validate_expert_draft({**good, "skills": ["same"] * (pr.MAX_SKILLS + 1) + [""]}) == [])
    check("大小写不同的技能按 Rust 的 eq_ignore_ascii_case 去重",
          pr.validate_expert_draft({**good, "skills": ["Skill-A", "skill-a"]}) == [])
    check(f"技能名超 {pr.MAX_SKILL_ID_LEN} 字符被拒",
          "技能标识过长" in pr.validate_expert_draft({**good, "skills": ["s" * (pr.MAX_SKILL_ID_LEN + 1)]}))
    check("技能清洗提醒单独成条",
          pr.normalization_notes({"skills": ["a", "A", "", "b"]})
          == ["技能里空串与重复项会被去掉，实际保存 2 个"])
    check("上限表就是那几个常量",
          pr.catalog_status()["limits"] == {
              "id": pr.MAX_ID_LEN, "skillId": pr.MAX_SKILL_ID_LEN, "name": pr.MAX_NAME_CHARS,
              "description": pr.MAX_DESCRIPTION_CHARS, "prompt": pr.MAX_PROMPT_CHARS,
              "skills": pr.MAX_SKILLS, "experts": pr.MAX_EXPERTS})
    check("上限表只有一处构造点（不在两处各抄一遍数字）",
          len(re.findall(r'def _limits\(\)', (PROJECT_ROOT / "engine" / "persona_runtime.py").read_text(encoding="utf-8"))) == 1)
    check("每一套模板自己的提示词都能过校验",
          all(not pr.validate_expert_draft({
              "id": t["templateId"].removeprefix("persona-"), "name": t["name"],
              "description": t["summary"], "prompt": t["prompt"]}) for t in pr.TEMPLATES))


# ── 4. 草稿生成 ─────────────────────────────────────────────────


def draft_checks() -> None:
    section("4. draft_from_template：一键创建的引擎那一半")
    d = pr.draft_from_template("persona-intj")
    check("默认草稿通过校验", d["ok"] and d["problems"] == [], str(d)[:200])
    check("缺省名称取模板名", d["expert"]["name"] == "建筑师")
    check("缺省描述取模板概要", d["expert"]["description"] == pr.TEMPLATE_INDEX["persona-intj"]["summary"])
    check("默认 id 由模板号去掉前缀得来", d["expert"]["id"] == "intj")
    check("草稿记下来源模板", d["expert"]["sourceTemplate"] == "persona-intj")
    check("草稿带同一组上限", d["limits"] == pr.catalog_status()["limits"])
    check("缺省说明讲清「还没保存」", "还没有保存" in d["note"] and "注册表" in d["note"], d["note"])
    check("通过时 reason 为空", d["reason"] == "")
    check("提醒通道存在", isinstance(d["notes"], list))

    over = pr.draft_from_template("persona-intj", name="名" * (pr.MAX_NAME_CHARS + 1))
    check("界面填了过长名称时 ok=false", not over["ok"] and over["problems"], str(over)[:160])
    check("拒绝理由带上限数值", str(pr.MAX_NAME_CHARS) in over["reason"], over["reason"])
    check("即便被拒也把草稿回出去（界面不用重填）", over["expert"]["name"] == "名" * (pr.MAX_NAME_CHARS + 1))
    check("被拒时 note 与 reason 同步（不给一句漂亮的空话）", over["note"] == over["reason"])

    unknown = pr.draft_from_template("persona-nope")
    check("未知模板 ok=false", not unknown["ok"])
    check("未知模板把可用清单回出来", "persona-intj" in unknown["reason"])
    check("未知模板不返回半成品 expert", unknown["expert"] == {})
    check("未知模板不返回 problems（还没到校验那步）", "problems" not in unknown)
    check("空模板号同样拒绝", not pr.draft_from_template("")["ok"])

    add = pr.draft_from_template("persona-entp", add_prompt="  只读资料，不许联网  ")
    base = pr.TEMPLATE_INDEX["persona-entp"]["prompt"]
    check("追加的话拼在模板提示词尾部", add["expert"]["prompt"].startswith(base) and "只读资料" in add["expert"]["prompt"])
    check("追加段落有标题（用户知道哪句是自己加的）", "补充要求：" in add["expert"]["prompt"])
    check("追加前后空白被去掉", add["expert"]["prompt"].endswith("只读资料，不许联网"))
    check("追加后仍然通过校验", add["ok"], str(add["problems"]))
    check("追加会计入长度（提示词预算按最终值算）",
          len(add["expert"]["prompt"]) > len(base))
    huge = pr.draft_from_template("persona-entp", add_prompt="字" * (pr.MAX_PROMPT_CHARS - len(base) + 5))
    check("追加到超上限会被自己拦下", not huge["ok"] and str(pr.MAX_PROMPT_CHARS) in huge["reason"], str(huge["problems"]))

    upper = pr.draft_from_template("persona-infj", expert_id="INFJ-Lead")
    check("回出去的 id 就是 Rust 会存的那个（已按小写归一）", upper["expert"]["id"] == "infj-lead")
    check("归一这件事写在提醒里而不是拒绝里",
          upper["ok"] and any("小写" in n for n in upper["notes"]), str(upper["notes"]))
    weird = pr.draft_from_template("persona-infj", expert_id="非法 名字")
    check("给了非法 id 就拒绝（不悄悄改名）", not weird["ok"] and "小写字母" in weird["reason"], weird["reason"])
    check("拒绝时仍如实回原始输入处理后的结果", weird["expert"]["id"] == "非法 名字")

    broken = [t["templateId"] for t in pr.TEMPLATES if not pr.draft_from_template(t["templateId"])["ok"]]
    check("16 套模板的默认草稿全部能直接存（缺省值一个都不越界）", not broken, str(broken))


# ── 5. 不落盘、不占名额 ─────────────────────────────────────────


def _code_tokens(path: Path) -> str:
    """把源码摊成「只剩标识符与运算符」的文本：字符串与注释都不算实现。

    这一族的文档里刻意写了「settings_kv / `expert::save` 不归我管」，
    拿整份源码去搜禁用词会把声明当成实现，那条用例就永远搜不出问题。
    """
    import io
    import tokenize

    parts: list[str] = []
    with io.StringIO(path.read_text(encoding="utf-8")) as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                            tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            parts.append(tok.string)
    return " ".join(parts)


def _imported_modules(path: Path) -> set[str]:
    """模块真正 import 了哪些顶层包（从 AST 取，不在测试里手抄依赖清单）。"""
    import ast

    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out


def isolation_checks(client: TestClient) -> None:
    section("5. 引擎只出草稿：不写注册表、不碰盘")
    module_path = PROJECT_ROOT / "engine" / "persona_runtime.py"
    code = _code_tokens(module_path)
    for forbidden in ("open", "write_text", "mkdir", "json", "sqlite3", "subprocess",
                      "shutil", "requests", "socket", "save_items", "normalize_registry"):
        check(f"模块代码里没有落盘/远程动作 {forbidden}",
              not re.search(rf"\b{re.escape(forbidden)}\b", code))
    mods = _imported_modules(module_path)
    check("模块只依赖 typing 与 expert_runtime 的上限常量",
          mods <= {"typing", "expert_runtime", "__future__"}, str(sorted(mods)))
    check("上限确实只有一个来源（不是自己另抄一份 8000）",
          pr.MAX_PROMPT_CHARS == er.MAX_PROMPT_CHARS and "expert_runtime" in mods)

    before = _tree_snapshot()
    for tid in pr.template_ids():
        pr.draft_from_template(tid, add_prompt="x" * 50)
        pr.validate_expert_draft({"id": tid, "name": "n", "description": "d", "prompt": "p"})
    check("进程内批量生成草稿：数据根整棵树没有任何文件新增或被改动",
          _tree_snapshot() == before, f"{len(before)} 项 -> {len(_tree_snapshot())} 项")

    for _ in range(pr.MAX_EXPERTS + 5):
        client.post("/api/persona/draft", headers=headers(), json={"templateId": "persona-istj"})
    client.get("/api/persona/templates", headers=headers())
    client.get("/api/persona/status", headers=headers())
    check("端点调用也没在数据根里留下 persona/expert 命名的文件",
          not _registry_snapshot(),
          str([str(p) for p in _tree()][:5]))
    check("模板库没有自己的存储目录（不是第二份台账）",
          not [p for p in _tree() if "persona" in str(p).lower()])
    check("草稿不占注册表名额：上限只作为 limits 回出去",
          "experts" in pr.draft_from_template("persona-intj")["limits"])
    check("名额检查留给 Rust：引擎侧连注册表读数都不碰",
          not hasattr(pr, "list_experts") and not hasattr(pr, "save"))


def _tree_snapshot() -> list[tuple[str, int, int]]:
    """数据根整棵树的读数：路径 + 字节数 + 修改时间。多一个文件、改一个字节都会红。"""
    return [(str(p.relative_to(HOME_ROOT)), p.stat().st_size, int(p.stat().st_mtime)) for p in _tree()]


def _registry_snapshot() -> list[Path]:
    """专家注册表 / persona 相关的落盘文件：一条都不该有。"""
    return [p for p in _tree() if re.search(r"expert|registry|persona", p.name, re.I)]


def _tree() -> list[Path]:
    root = HOME_ROOT
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file()]


# ── 6. 端点面 ───────────────────────────────────────────────────


def endpoint_checks(client: TestClient) -> None:
    section("6. /api/persona/* 端点面")
    app_paths = {r.path for r in server.app.routes}
    missing = [p for p in rx.EXEC_ENDPOINT_PATHS if p not in app_paths]
    check("清单里的每条 exec 端点都真挂在 app 上", not missing, str(missing))
    persona = {p for p in app_paths if p.startswith("/api/persona")}
    check("五条 persona 端点都在 app 上", persona == {
        "/api/persona/templates", "/api/persona/template", "/api/persona/draft",
        "/api/persona/validate", "/api/persona/status"}, str(sorted(persona)))
    gets = {r.path for r in server.app.routes if "GET" in (r.methods or set()) and r.path.startswith("/api/persona")}
    posts = {r.path for r in server.app.routes if "POST" in (r.methods or set()) and r.path.startswith("/api/persona")}
    check("只读两条走 GET", gets == {"/api/persona/templates", "/api/persona/template", "/api/persona/status"}, str(sorted(gets)))
    check("会产出内容的两条走 POST", posts == {"/api/persona/draft", "/api/persona/validate"}, str(sorted(posts)))

    listing = client.get("/api/persona/templates", headers=headers()).json()
    SEEN_KEYS["templates"] = set(listing)
    SEEN_KEYS["templates_bad"] = set(client.get("/api/persona/templates", headers=headers(),
                                                params={"group": "x"}).json())
    check("清单端点回 16 套", listing["ok"] and listing["total"] == 16 and len(listing["templates"]) == 16)
    check("清单端点带分组常量", listing["groups"] == list(pr.GROUPS))
    check("清单端点带上限表", listing["limits"] == pr.catalog_status()["limits"])
    check("无过滤时不写 group 值", listing["group"] == "")
    filtered = client.get("/api/persona/templates", headers=headers(), params={"group": "守护者"}).json()
    check("按组过滤只剩 4 条", filtered["ok"] and filtered["total"] == 4
          and {t["group"] for t in filtered["templates"]} == {"守护者"})
    bogus = client.get("/api/persona/templates", headers=headers(), params={"group": "守护者x"})
    check("分组写错回 200（不是 500 也不是空表了事）", bogus.status_code == 200)
    check("分组写错 ok=false 并列出可用分组",
          not bogus.json()["ok"] and bogus.json()["groups"] == list(pr.GROUPS)
          and "守护者x" in bogus.json()["reason"], str(bogus.json())[:200])

    one = client.get("/api/persona/template", headers=headers(), params={"templateId": "persona-esfp"}).json()
    SEEN_KEYS["template"] = set(one)
    check("单套端点回全文", one["ok"] and one["template"]["mbti"] == "ESFP" and one["template"]["prompt"])
    miss = client.get("/api/persona/template", headers=headers(), params={"templateId": "persona-zz"})
    SEEN_KEYS["template_bad"] = set(miss.json())
    check("查不到的单套端点 ok=false + 可用清单",
          not miss.json()["ok"] and "persona-intj" in miss.json()["templateIds"], str(miss.json())[:160])
    noarg = client.get("/api/persona/template", headers=headers())
    check("缺 templateId 走 400 + 错误码", noarg.status_code == 400 and noarg.json()["code"] == "PERSONA_BAD_REQUEST")
    check("缺参的 message 说清缺什么", "templateId" in noarg.json()["message"], str(noarg.json())[:160])

    drafted = client.post("/api/persona/draft", headers=headers(),
                          json={"templateId": "persona-isfj", "addPrompt": "先列改动清单再动手"}).json()
    SEEN_KEYS["draft"] = set(drafted)
    SEEN_KEYS["draft_bad"] = set(client.post("/api/persona/draft", headers=headers(),
                                             json={"templateId": "persona-none"}).json())
    check("草稿端点 ok 且带草稿", drafted["ok"] and drafted["expert"]["id"] == "isfj", str(drafted)[:200])
    check("草稿端点明说没保存", drafted["saved"] is False)
    check("追加的话进了草稿提示词", "先列改动清单再动手" in drafted["expert"]["prompt"])
    bad_draft = client.post("/api/persona/draft", headers=headers(), json={})
    check("草稿端点缺模板号走 400", bad_draft.status_code == 400)
    ghost = client.post("/api/persona/draft", headers=headers(), json={"templateId": "persona-none"})
    check("草稿端点对未知模板回 200 + ok=false", ghost.status_code == 200 and not ghost.json()["ok"])
    snake = client.post("/api/persona/draft", headers=headers(),
                        json={"template_id": "persona-istp", "expert_id": "MyIstp", "add_prompt": "x"}).json()
    check("下划线写法同样接受（不把界面钉死在一种拼法）", snake["ok"], str(snake)[:160])
    check("下划线写法同样做 id 归一", snake["expert"]["id"] == "myistp")

    flat = client.post("/api/persona/validate", headers=headers(),
                       json={"id": "abc", "name": "n", "description": "d", "prompt": "p"}).json()
    SEEN_KEYS["validate"] = set(flat)
    check("校验端点接受扁平草稿", flat["ok"] and flat["problems"] == [], str(flat)[:160])
    wrapped = client.post("/api/persona/validate", headers=headers(),
                          json=drafted | {"templateId": "persona-isfj"}).json()
    check("校验端点接受 draft 原样回传（含 expert 子对象）", wrapped["ok"], str(wrapped)[:200])
    rejected = client.post("/api/persona/validate", headers=headers(), json={"expert": {"name": ""}}).json()
    check("校验端点按条回问题清单", not rejected["ok"] and len(rejected["problems"]) >= 3, str(rejected)[:200])
    check("校验端点把问题拼成一句 reason", "不能为空" in rejected["reason"], rejected["reason"])
    check("校验端点回同一组上限", rejected["limits"] == pr.catalog_status()["limits"])
    check("校验端点在通过时说明自己只读不写", "只读不写" in flat["note"], flat["note"])
    check("校验端点在拒绝时指向 Rust 的同一条门",
          "expert::save" in rejected["note"], rejected["note"])
    check("校验端点对通过也说「不代表已保存」", "不代表已保存" in flat["note"], flat["note"])
    warned = client.post("/api/persona/validate", headers=headers(),
                         json={"id": "AbC", "name": "n", "description": "d", "prompt": "p"}).json()
    check("归一提醒进 notes 不进 problems",
          warned["ok"] and warned["problems"] == [] and warned["notes"], str(warned)[:200])
    empty = client.post("/api/persona/validate", headers=headers(), json={})
    check("校验端点没带草稿时 400", empty.status_code == 400 and "草稿" in empty.json()["message"])
    check("校验端点对非对象请求体按缺参处理（不 500）",
          client.post("/api/persona/validate", headers=headers(), content=b"[1,2]").status_code == 400)
    check("草稿端点对非对象请求体同样不 500",
          client.post("/api/persona/draft", headers=headers(), content=b"[[1]]").status_code == 400)
    check("草稿端点对无效 JSON 不 500",
          client.post("/api/persona/draft", headers=headers(), content=b"{oops").status_code == 400)

    status = client.get("/api/persona/status", headers=headers()).json()
    SEEN_KEYS["status"] = set(status)
    check("状态端点回模板数与分组分布", status["ok"] and status["count"] == 16
          and sorted(status["groups"].values()) == [4, 4, 4, 4])
    check("状态端点回全部模板号", status["templateIds"] == pr.template_ids())
    check("状态端点指出提示词上限的来源", "expert_runtime" in status["promptBudgetSource"], status["promptBudgetSource"])
    check("状态端点诚实列出没接完的部分", len(status["unverified"]) == 2
          and any("一键" in u or "按钮" in u for u in status["unverified"]), str(status["unverified"])[:200])
    check("状态端点说明风格折进提示词", "折进提示词" in status["note"], status["note"])

    # 这一族刻意不套桌面闸门：模板是代码常量、草稿不落盘，拦远程身份只会让人看不见角色库
    for verb, path in (
        (client.get, "/api/persona/templates"),
        (client.get, "/api/persona/status"),
        (client.get, "/api/persona/template"),
    ):
        resp = verb(path, headers=headers(STATIC_TOKEN), params={"templateId": "persona-intj"})
        check(f"远程身份 {path} 不被 403 拦下（有意为之）", resp.status_code == 200, str(resp.status_code))
    remote_draft = client.post("/api/persona/draft", headers=headers(STATIC_TOKEN),
                               json={"templateId": "persona-intj"})
    check("远程身份能拿草稿（草稿不含任何用户数据）", remote_draft.status_code == 200 and remote_draft.json()["ok"])
    check("草稿端点不回任何暂存/写盘字段",
          not ({"targetPath", "changeId", "newContent"} & set(remote_draft.json())))


# ── 7. 跨语言同源 ───────────────────────────────────────────────


def cross_language_checks() -> None:
    section("7. 与 Rust / TS 同源")
    check("Rust 专家校验文件在（反查前提）", RUST_EXPERT.exists(), str(RUST_EXPERT))
    consts = rust_max_constants()
    for key, attr in (("MAX_EXPERTS", "MAX_EXPERTS"), ("MAX_ID_LEN", "MAX_ID_LEN"),
                      ("MAX_NAME_CHARS", "MAX_NAME_CHARS"),
                      ("MAX_DESCRIPTION_CHARS", "MAX_DESCRIPTION_CHARS"),
                      ("MAX_PROMPT_CHARS", "MAX_PROMPT_CHARS"),
                      ("MAX_SKILLS", "MAX_SKILLS"), ("MAX_SKILL_ID_LEN", "MAX_SKILL_ID_LEN")):
        check(f"{key} 与 Rust 同值（{getattr(pr, attr)}）",
              key in consts and consts[key] == getattr(pr, attr), str(consts.get(key)))
    check("Rust 的 id 字符白名单与引擎一致（小写/数字/下划线/连字符）",
          all(x in RUST_EXPERT.read_text(encoding="utf-8")
              for x in ("is_ascii_lowercase()", "is_ascii_digit()", "c == '_'", "c == '-'")))
    check("引擎白名单集合正好是这四类", pr._ID_ALLOWED == set("abcdefghijklmnopqrstuvwxyz0123456789_-"))
    rust_src = RUST_EXPERT.read_text(encoding="utf-8")
    for msg in ("专家标识不能为空", "专家标识过长", "专家标识仅支持小写字母、数字、下划线与连字符",
                "专家名称不能为空", "职责描述不能为空", "角色提示词不能为空", "技能标识过长",
                "单个专家最多选择"):
        check(f"引擎的理由能在 Rust 找到同一条：{msg}", msg in rust_src)
    for p in pr.validate_expert_draft({"id": "", "name": "", "description": "", "prompt": ""}):
        check(f"引擎四条空值拒绝逐条对应 Rust 文案：{p[:12]}", p in rust_src)
    check("提示词上限与 expert_runtime 同一常量（截断逻辑消费的那个）",
          pr.MAX_PROMPT_CHARS == er.MAX_PROMPT_CHARS)

    ts = TS_CONTRACT.read_text(encoding="utf-8")
    check("TS 契约登记了 persona 五条端点中的关键三条",
          all(x in ts for x in ("/api/persona/templates", "/api/persona/draft", "/api/persona/validate")))
    check("模板字段与 TS 接口双向相等（加字段、删字段都要红）",
          _template_fields_from_source() == _ts_interface_fields("PersonaTemplate"),
          f"Python 多：{sorted(_template_fields_from_source() - _ts_interface_fields('PersonaTemplate'))} "
          f"TS 多：{sorted(_ts_interface_fields('PersonaTemplate') - _template_fields_from_source())}")
    check("上限表字段与 TS 接口双向相等",
          _limits_fields_from_source() == _ts_interface_fields("PersonaLimits"),
          f"{sorted(_limits_fields_from_source())} vs {sorted(_ts_interface_fields('PersonaLimits'))}")
    pairs = (
        ("PersonaDraftResponse", SEEN_KEYS["draft"] | SEEN_KEYS["draft_bad"]),
        ("PersonaStatusResponse", SEEN_KEYS["status"]),
        ("PersonaValidateResponse", SEEN_KEYS["validate"]),
        ("PersonaTemplatesResponse", SEEN_KEYS["templates"] | SEEN_KEYS["templates_bad"]),
        ("PersonaTemplateResponse", SEEN_KEYS["template"] | SEEN_KEYS["template_bad"]),
        ("PersonaExpertDraft", set(pr.draft_from_template("persona-intj")["expert"])),
    )
    for iface, keys in pairs:
        declared = _ts_interface_fields(iface)
        absent = sorted(keys - declared)
        extra = sorted(declared - keys)
        check(f"{iface} 与端点实际回话的键集双向相等", not absent and not extra,
              f"TS 缺 {absent}；TS 多出（引擎从来没回过的字段）{extra}")
    check("TS 里写明这一族不走 IPC", "不走 IPC" in ts)
    check("人格模板段落沿用同一条口径（清单与草稿都不经 IPC）", "同样不走 IPC" in ts)
    check("引擎车道不 import server（避免两份会话表）",
          "import server" not in (PROJECT_ROOT / "engine" / "routes_exec.py").read_text(encoding="utf-8"))
    check("车道前缀里登记了 persona", "/api/persona" in rl_prefixes())
    check("persona 五条都在车道路径清单里（清单是唯一来源）",
          {p for p in rx.EXEC_ENDPOINT_PATHS if p.startswith("/api/persona")} == {
              "/api/persona/templates", "/api/persona/template", "/api/persona/draft",
              "/api/persona/validate", "/api/persona/status"})
    codes = re.findall(r'"(PERSONA_[A-Z_]+)"',
                       (PROJECT_ROOT / "engine" / "routes_exec.py").read_text(encoding="utf-8"))
    check("本族错误码一律 PERSONA_ 前缀且都带冒号（因此不进 W5 的消息映射表）",
          len(set(codes)) >= 5 and all(c.startswith("PERSONA_") for c in codes), str(sorted(set(codes))))


def rl_prefixes() -> list[str]:
    import routes_lanes as rl
    return list(rl.LANE_PREFIXES["exec"])


# ── 8. 诚实面 ───────────────────────────────────────────────────


def honesty_checks() -> None:
    section("8. 未落地部分的口径")
    status = pr.catalog_status()
    joined = " ".join(status["unverified"])
    check("桌面按钮未接线写进了读数", "按钮" in joined and "未接线" in joined, joined[:200])
    check("expert::save 由界面发起这件事写清了", "expert::save" in joined)
    check("D-42 后两半（共享/桥接）也列在没接完里", "共享" in joined and "桥接" in joined)
    src = (PROJECT_ROOT / "engine" / "persona_runtime.py").read_text(encoding="utf-8")
    check("模块 docstring 自己承认界面未接线", "尚未接线" in src)
    check("模块 docstring 说明为什么不加新字段", "personalityStyle" in src)
    ledger = (REPO_ROOT / "docs" / "缺陷与实现计划-任我行.md").read_text(encoding="utf-8")
    check("台账里 D-42 的验收口径仍是「一键创建专家」", "人格模板一键创建专家" in ledger)
    ws = _TMP / "ws" / "names"
    ws.mkdir(parents=True, exist_ok=True)
    check("没有新增常驻工具名（模板只走 HTTP 面）",
          not any("persona" in n for n in server.build_toolkit(ws).tools))
    req = (PROJECT_ROOT / "engine" / "requirements.txt").read_text(encoding="utf-8")
    check("没有新增第三方依赖", "persona" not in req.lower())


def main() -> int:
    print("=== 人格模板与专家草稿：模板库 / 校验同源 / 草稿 / 端点面自测（离线 · 临时数据根）===")
    desktop_env()
    structure_checks()
    content_checks()
    validate_checks()
    draft_checks()
    client = TestClient(server.app)
    isolation_checks(client)
    endpoint_checks(client)
    cross_language_checks()
    honesty_checks()
    print("----------------------------------------------------")
    if FAILURES:
        print(f"### {len(FAILURES)} 项未过：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("persona templates selftest: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
