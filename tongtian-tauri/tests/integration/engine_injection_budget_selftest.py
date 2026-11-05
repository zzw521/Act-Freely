#!/usr/bin/env python3
"""
注入面总量上限自测（D-18 / 卡 #155）

锁住四件「改了就有人看得见」的事：
1. `_budget_sections` 的三档语义：整段排得下 → 原样进；卡在边界 → 截一部分并留标记；
   剩的位置少于 MIN_SECTION_FIT_CHARS → 整段不进模型，但必须留下一条**指名道姓**的说明
   （本仓库的铁律：读不到/没进去就显式说，绝不静默丢内容）；
2. 技能清单里 description 的单条上限（300 字）与「一条汇总不给每条各留一条」的说明口径；
3. 意识段与附件段的**整轮总量**上限——单件上限互不知晓彼此，三份 6000 字的意识文件
   加起来就能独自超过模型窗口，长任务基准 heavy 档量到过（104 次任务调用配 103 次摘要调用）；
4. 这些上限不许动到 Rust `harness/prompt.rs` 逐字镜像的内容：MODE_PROMPTS 的
   design/writing 两段与 Rust 侧比对**逐字相等**，normal 输入不产生任何新说明（不给界面添噪声）。

离线运行，只依赖 engine/runtime_context.py 与 src-tauri 源码文本。
用法: ./vendor/python/python.exe -X utf8 tests/integration/engine_injection_budget_selftest.py
"""

import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

import runtime_context as rc

RUST_PROMPT = PROJECT_ROOT / "src-tauri" / "src" / "harness" / "prompt.rs"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _skill(sid: str, description: str, **extra) -> dict:
    entry = {"id": sid, "name": f"技能 {sid}", "description": description,
             "content": f"{sid} 的正文", "kind": "skill"}
    entry.update(extra)
    return entry


def _runtime(files: dict, skills: list) -> dict:
    return {"awareness": {"mode": True, "autoMemory": False, "files": files},
            "skills": skills}


# ── 1. _budget_sections 三档语义 ──────────────────────────────

def budget_section_checks() -> None:
    print("--- 1. 总量上限的三档语义 ---")
    limit = 1000
    sections = [("甲", "A" * 400), ("乙", "B" * 400), ("丙", "C" * 400)]
    kept, notes = rc._budget_sections(sections, limit, "测试注入")
    check("排得下的整段原样保留", kept[0] == "A" * 400 and kept[1] == "B" * 400, str(kept)[:80])
    check("排不下的整段不进模型", all("C" not in text for text in kept), str(kept)[:80])
    check("没进的那一段留下指名道姓的说明",
          any("丙" in n and "未进入模型" in n and "总量上限 1000" in n for n in notes), str(notes))
    check("说明摊出原文长度（用户能判断少了多少）",
          any("该段原文共 400 字" in n for n in notes), str(notes))

    edge = [("甲", "A" * 600), ("乙", "B" * 500)]
    kept, notes = rc._budget_sections(edge, limit, "测试注入")
    check("卡在边界的段截一部分进模型", len(kept) == 2 and kept[1].startswith("B"), str(kept)[:120])
    check("截断标记写进正文本身（模型也看得见少了）",
          "已截断后进入模型" in kept[1] and "原文共 500 字" in kept[1], kept[1][-120:])
    check("截断段不再重复报整段丢弃",
          not any("乙未进入模型" in n for n in notes), str(notes))

    tiny = [("甲", "A" * 995), ("乙", "B" * 10)]
    kept, notes = rc._budget_sections(tiny, limit, "测试注入")
    check("只剩不到一句话说得清的量时宁可整段不进", len(kept) == 1, str(kept)[:80])
    check("整段不进也必须有说明（不许静默消失）",
          any("乙未进入模型" in n for n in notes), str(notes))
    check("阈值就是 MIN_SECTION_FIT_CHARS", rc.MIN_SECTION_FIT_CHARS == 300, str(rc.MIN_SECTION_FIT_CHARS))

    fits, notes = rc._budget_sections([("甲", "A" * 500)], limit, "测试注入")
    check("没触顶时不制造任何说明（normal 输入零噪声）", fits == ["A" * 500] and notes == [], str(notes))
    empty, notes = rc._budget_sections([], limit, "测试注入")
    check("空清单不报错", empty == [] and notes == [])


# ── 2. 技能描述单条上限 ───────────────────────────────────────

def skill_description_checks() -> None:
    print("--- 2. 技能清单描述的长度上限 ---")
    check("缺省描述上限 300 字", rc.MAX_SKILL_DESC_CHARS == 300, str(rc.MAX_SKILL_DESC_CHARS))

    short = _skill("s-a", "把 CSV 读成表并按列汇总")
    text, notes = rc.compose_skills([short])
    check("短描述逐字进清单（不截、不添说明）", "把 CSV 读成表并按列汇总" in text, text[:200])
    check("没触顶时技能侧零说明", notes == [], str(notes))

    raw = "口" * 700
    text, notes = rc.compose_skills([_skill("s-b", raw)])
    check("超限描述截到 300 字", ("口" * 300) in text and ("口" * 301) not in text, text[:200])
    check("描述截断标记写明原文长度与取正文的办法",
          "…（描述已截断，原文共 700 字，完整做法请用 use_skill 取正文）" in text, text[200:400])
    check("超限描述有一条汇总说明并点名技能 id",
          any("1 个技能/套件指令的描述超出 300 字" in n and "s-b" in n for n in notes), str(notes))
    check("汇总说明声明正文未受影响（use_skill 仍可取全文）",
          any("正文未受影响" in n for n in notes), str(notes))

    many = [_skill(f"m{i}", "长" * 500) for i in range(9)]
    _, notes = rc.compose_skills(many)
    joined = " ".join(notes)
    check("多个超限时只出一条汇总，不给每个技能各留一条",
          sum("描述超出 300 字" in n for n in notes) == 1, str(notes))
    check("汇总点名上限 8 个并加「等」",
          "9 个技能/套件指令的描述超出" in joined and "等" in joined, joined[:300])
    check("汇总里第 9 个 id 不展开", "m8" not in joined, joined[-200:])

    blank = [{"id": "c-1", "name": "套件指令一", "description": "", "content": "", "kind": "command"}]
    text, _ = rc.compose_skills(rc.normalize_skills({"skills": blank}))
    check("空描述用占位而不是留空行（套件与技能措辞不同）",
          "（套件包未写描述）" in text, text[-300:])
    text, _ = rc.compose_skills(rc.normalize_skills({"skills": [
        {"id": "k-1", "name": "技能一", "description": "", "content": "正文"}]}))
    check("技能空描述写明来自 SKILL.md", "（SKILL.md 未写描述）" in text, text[-300:])

    heavy = [_skill(f"h{i}", "描" * (600 + i)) for i in range(30)]
    text, _ = rc.compose_skills(heavy)
    check("30 个技能的清单段有上界（不随描述长度线性膨胀）",
          len(text) < 30 * (rc.MAX_SKILL_DESC_CHARS + 60) + 2000, f"清单段 {len(text)} 字")


# ── 3. 意识段整轮总量上限 ─────────────────────────────────────

def awareness_budget_checks() -> None:
    print("--- 3. 意识注入的总量上限 ---")
    check("意识段总量上限 12000 字", rc.MAX_AWARENESS_TOTAL_CHARS == 12000,
          str(rc.MAX_AWARENESS_TOTAL_CHARS))

    files = {"SOUL.md": "风" * 6000, "USER_PROFILE.md": "像" * 6000, "WORK_MANUAL.md": "册" * 6000}
    text, notes = rc.compose_awareness({"awareness": {"mode": True, "files": files}})
    check("三份 6000 字的意识文件不会整块进模型", len(text) <= rc.MAX_AWARENESS_TOTAL_CHARS + 600,
          f"意识段 {len(text)} 字")
    check("按注入优先级保留靠前的（SOUL 在前）", "风" * 100 in text and "来自 SOUL.md" in text, text[:120])
    check("排在最后的整段不进模型且留下说明",
          any("意识文件「WORK_MANUAL.md」未进入模型" in n and "总量上限 12000" in n for n in notes), str(notes))
    check("卡在边界的第二份截断并留标记",
          "已截断后进入模型" in text, text[-160:])

    mixed = {"SOUL.md": "风" * 7000, "USER_PROFILE.md": "像" * 6000, "WORK_MANUAL.md": "册" * 6000}
    text, notes = rc.compose_awareness({"awareness": {"mode": True, "files": mixed}})
    check("单件上限说明仍在（每份各自超限的那条照旧报）",
          any("意识文件 SOUL.md 超出 6000 字" in n for n in notes), str(notes)[:300])
    check("单件说明与总量说明并存，不因套了预算就少报",
          any("总量上限" in n for n in notes) and any("超出 6000 字" in n for n in notes), str(notes)[:300])
    check("单件+总量叠加时意识段标题仍逐字保留",
          "## 用户意识（长期生效，优先于你的默认表述习惯）" in text, text[:120])

    light = {"SOUL.md": "简短风格约定", "USER_PROFILE.md": "称呼我张工"}
    text, notes = rc.compose_awareness({"awareness": {"mode": True, "files": light}})
    check("正常意识原样注入（本刀不改中位场景）",
          "简短风格约定" in text and "称呼我张工" in text and notes == [], str(notes))
    check("意识段标题仍逐字保留",
          "## 用户意识（长期生效，优先于你的默认表述习惯）" in text, text[:120])

    off, notes = rc.compose_awareness({"awareness": {"mode": False, "files": light}})
    check("意识开关关闭时不注入也不套预算", off == "" and any("意识开关已关闭" in n for n in notes), str(notes))


# ── 4. 附件正文整轮总量上限 ───────────────────────────────────

def attachment_budget_checks() -> None:
    print("--- 4. 附件正文的总量上限 ---")
    check("附件正文总量上限 48000 字", rc.MAX_ATTACH_TOTAL_CHARS == 48000,
          str(rc.MAX_ATTACH_TOTAL_CHARS))

    atts = [{"fileName": f"文档{i}.md", "mime": "text/markdown", "text": f"文档{i}正文" + "内" * 17996}
            for i in range(6)]
    text, notes = rc.compose_attachments(atts)
    check("六份 18000 字正文不会整块进模型", len(text) <= rc.MAX_ATTACH_TOTAL_CHARS + 900,
          f"附件段 {len(text)} 字")
    dropped = [n for n in notes if "未进入模型" in n and "总量上限" in n]
    check("排不下的三份各有说明", len(dropped) == 3, str(dropped)[:300])
    check("说明点名是哪份附件",
          any("文档5.md" in n for n in dropped), str(dropped)[:300])
    check("进模型的部分仍带附件标题（不出现无名正文）", "### 附件：文档0.md" in text, text[:120])
    check("单件上限说明与总量上限说明并存且不互相顶替",
          any("已截断后进入模型" in n for n in notes) or "已截断后进入模型" in text, str(notes)[:300])

    one = [{"fileName": "合同.md", "mime": "text/markdown", "text": "甲方与乙方" + "约" * 500}]
    text, notes = rc.compose_attachments(one)
    check("单份小附件原样注入且零说明", "甲方与乙方" in text and notes == [], str(notes))
    check("附件段标题仍逐字保留", "## 用户上传的附件正文" in text, text[:120])

    broken = [{"fileName": "坏文件.pdf", "error": "正文抽取为空"}, *atts]
    text, notes = rc.compose_attachments(broken)
    check("读取失败的附件仍走原有说明，不被预算逻辑吃掉",
          any("附件「坏文件.pdf」未能读取正文：正文抽取为空" in n for n in notes), str(notes)[:300])
    check("失败附件不占总量预算（后面的内容不因它被挤掉）",
          len([n for n in notes if "总量上限" in n]) == 3, str(notes)[:400])

    image = [{"fileName": "a.png", "image": {"data": "", "mime": "image/png"}}]
    _, notes = rc.compose_attachments(image)
    check("图片分支不受正文预算影响", any("没有可下发的图像数据" in n for n in notes), str(notes))


# ── 5. 汇总入口与 Rust 镜像不漂移 ─────────────────────────────

def _rust_mode_prompt(source: str, mode: str) -> str | None:
    """把 Rust `mode_prompt()` 分支里的字符串字面量还原成 Python 字符串。

    Rust 用「反斜杠 + 换行」做跨行续接，Python 侧读到的是原始文本，先去掉续接符
    再解 \\n 转义；这一步做错了比对就会假红，所以只取这一条通路，不做通用解析。
    """
    arm = re.search(rf'"{mode}" =>\s*\{{\s*"', source)
    if arm is None:
        return None
    start = arm.end() - 1
    end = source.index('"', start + 1)
    raw = source[start + 1:end]
    raw = re.sub(r"\\[ \t]*\n[ \t]*", "", raw)
    return raw.replace("\\n", "\n").replace('\\"', '"')


def prompt_mirror_checks() -> None:
    print("--- 5. 与 Rust harness/prompt.rs 逐字比对 ---")
    if not RUST_PROMPT.exists():
        print(f"[SKIP] 未找到 {RUST_PROMPT}")
        return
    source = RUST_PROMPT.read_text(encoding="utf-8", errors="replace")
    for mode in ("design", "writing"):
        rust = _rust_mode_prompt(source, mode)
        check(f"Rust 侧 {mode} 段能取到（取不到说明对端改了写法，本用例要重写）",
              rust is not None and rust != "", str(rust)[:80])
        if rust:
            check(f"{mode} 段与 Python MODE_PROMPTS 逐字相等（本刀未动镜像内容）",
                  rc.MODE_PROMPTS[mode] == rust,
                  f"py={rc.MODE_PROMPTS[mode][:60]!r} rs={rust[:60]!r}")
    start = source.index("fn mode_prompt(")
    end = source.index("\n}\n", start)          # 顶层函数以列零的 } 收尾
    body = source[start:end]
    check("general 模式两侧都不追加内容（Rust 走兜底分支，没有 general 臂）",
          rc.MODE_PROMPTS["general"] == "" and '"general" =>' not in body, body[:120])
    check("slides 段仍留在 Python（Rust harness 没有 generate_pptx，本就不镜像）",
          "generate_pptx" in rc.MODE_PROMPTS["slides"], rc.MODE_PROMPTS["slides"][:80])

    base = "你是通义工作台助手。"
    runtime = _runtime(
        {"SOUL.md": "S" * 6000, "USER_PROFILE.md": "U" * 6000, "WORK_MANUAL.md": "W" * 6000},
        [_skill(f"x{i}", "desc" * 150) for i in range(30)],
    )
    prompt, notes = rc.compose_system_prompt(base, runtime, query="整理报表")
    check("system prompt = 基础人设原样在前", prompt.startswith(base), prompt[:60])
    check("重注入负载下固定段有上界（不随文件/描述长度线性膨胀）",
          len(prompt) - len(base) <= rc.MAX_AWARENESS_TOTAL_CHARS + 30 * (rc.MAX_SKILL_DESC_CHARS + 60) + 2500,
          f"注入部分 {len(prompt) - len(base)} 字")
    check("意识与技能的触顶说明都从汇总入口流出（界面看得见少了什么）",
          any("WORK_MANUAL.md" in n for n in notes) and any("描述超出" in n for n in notes), str(notes)[:400])
    mirror = rc.context_notes(runtime, "整理报表")
    check("context_notes 与 compose_system_prompt 的说明同口径",
          all(any(n in m for m in mirror) for n in notes if "描述超出" in n or "未进入模型" in n),
          f"notes={notes[:1]} mirror={mirror[:1]}")
    _, light_notes = rc.compose_system_prompt(base, _runtime({"SOUL.md": "短"}, [_skill("y", "短描述")]), "整理报表")
    check("轻负载下 system prompt 侧不产生任何新说明", light_notes == [], str(light_notes))


def main() -> None:
    budget_section_checks()
    skill_description_checks()
    awareness_budget_checks()
    attachment_budget_checks()
    prompt_mirror_checks()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("injection budget selftest: ALL PASS")


if __name__ == "__main__":
    main()
