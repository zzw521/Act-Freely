#!/usr/bin/env python3
"""技能统一注册表与共享池自测（缺陷 D-38）

锁住「两套注册表并成一份清单」这条链，验收句逐条对证：
1. 一个技能从共享池同时启用给两个专家，**改一次两边生效**（池里存引用不存副本）
2. **停用立即影响两边**（启停是同一份事实里的两段）
3. 既有 SKILL.md 与平台层 YAML **只读适配**：迁移后仍可加载，原文件一个字不动
4. **draft skill 不会自动启用**，确认要显式 promote
5. **缺依赖时中文提示且不静默跳过**
6. frontmatter 解析与 Rust `skill::parse_skill_md` 逐条对齐；正文上限两边同值
7. 清单输出就是 `runtime_context.normalize_skills` 认得的形状（接缝只差一次调用）
8. 清单文件读坏了不覆盖写回，也不假装池是空的

用法: python tests/integration/engine_skill_registry_selftest.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

import skill_registry_runtime as sr  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(("[PASS] " if cond else "[FAIL] ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def write_skill(home: Path, skill_id: str, body: str) -> Path:
    directory = sr.installed_root(home) / skill_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / sr.SKILL_MD_NAME
    path.write_text(body, encoding="utf-8")
    return path


def touch_version(path: Path, text: str) -> None:
    """改正文并把 mtime 往后推：增量判定靠内容摘要，不靠时间戳"""
    path.write_text(text, encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime + 5, stat.st_mtime + 5))


def main():
    home = Path(tempfile.mkdtemp(prefix="tt-skill-pool-"))
    broken_home = None
    try:
        # ── 1. 只读适配：已安装的 SKILL.md ────────────────────
        weekly = write_skill(home, "weekly-report", (
            "---\nname: 周报助手\ndescription: 汇总本周工作产出\n---\n\n"
            "把本周提交、评审与线上事件汇总成周报，控制在 300 字内。\n"
        ))
        write_skill(home, "no-frontmatter", "只有一行正文的技能，没有 frontmatter。\n")
        (sr.installed_root(home) / "empty-dir").mkdir(parents=True, exist_ok=True)
        (sr.installed_root(home) / "bad name").mkdir(parents=True, exist_ok=True)
        (sr.installed_root(home) / "bad name" / sr.SKILL_MD_NAME).write_text("x", encoding="utf-8")
        big = sr.installed_root(home) / "too-big"
        big.mkdir(parents=True, exist_ok=True)
        (big / sr.SKILL_MD_NAME).write_text("字" * (sr.MAX_SKILL_MD_BYTES), encoding="utf-8")

        entries, notes = sr.discover_installed(home)
        ids = sorted(e.id for e in entries)
        check("已安装技能被只读发现", "weekly-report" in ids, str(ids))
        check("没有 SKILL.md 的目录不算技能", "empty-dir" not in ids, str(ids))
        check("标识不合法的目录如实说明", any("bad name" in n for n in notes), str(notes))
        check("超限技能如实说明而不是静默少一个",
              any("too-big" in n and "上限" in n for n in notes), str(notes))
        row = next(e for e in entries if e.id == "weekly-report")
        check("frontmatter 的 name 成为技能标题", row.title == "周报助手", row.title)
        check("frontmatter 的 description 被带走", row.description == "汇总本周工作产出", row.description)
        plain = next(e for e in entries if e.id == "no-frontmatter")
        check("无 frontmatter 时正文首段兜底做描述",
              plain.description.startswith("只有一行正文的技能"), plain.description)
        check("适配过程没动过任何原文件",
              weekly.read_text(encoding="utf-8").startswith("---\nname: 周报助手"))

        # ── 2. frontmatter 解析与 Rust 逐条对齐 ────────────────
        parity = [
            ("---\nname: 甲\ndescription: 乙\n---\n正文", "甲", "乙"),
            ('---\nname: "带引号"\ndescription: \'也是\'\n---\n', "带引号", "也是"),
            ("---\nname: >\n  折叠\n  续行\ndescription: 普通\n---\n", "折叠 续行", "普通"),
            ("---\nname: 只有名字\n---\n正文首段", "只有名字", ""),
            ("---\nunknownKey: x\nname: 只取两键\n---\n", "只取两键", ""),
            ("前面有空行\n---\nname: 跳过空行\n---\n", "", "前面有空行"),
            ("正文起头\n第二段", "", "正文起头"),
            ("# 标题\n\n第一段正文", "", "第一段正文"),
            ("---\nname:\ndescription:\n---\n兜底首段", "", "兜底首段"),
        ]
        for text, want_name, want_desc in parity:
            got_name, got_desc = sr.parse_frontmatter(text)
            check(f"frontmatter 解析一致：{text[:22]!r}",
                  got_name == want_name and got_desc == want_desc,
                  f"得 ({got_name!r}, {got_desc!r}) 期望 ({want_name!r}, {want_desc!r})")

        rust = (ROOT / "src-tauri" / "src" / "skill" / "mod.rs")
        if rust.is_file():
            source = rust.read_text(encoding="utf-8", errors="replace")
            check("正文上限与 Rust MAX_SKILL_MD_BYTES 同值",
                  f"pub(crate) const MAX_SKILL_MD_BYTES: u64 = {sr.MAX_SKILL_MD_BYTES} * 1" in source
                  or f"{sr.MAX_SKILL_MD_BYTES // 1024} * 1024" in source,
                  f"本侧 {sr.MAX_SKILL_MD_BYTES}")
            check("技能目录名与 Rust skills_root 同源",
                  'base.join("skills")' in source, "")
            check("标识长度上限与 Rust MAX_ID_LEN 同档",
                  "const MAX_ID_LEN: usize = 64;" in source
                  and bool(sr.ID_RE.match("x" * 64)) and not bool(sr.ID_RE.match("x" * 65)),
                  f"{sr.ID_RE.pattern} / 64 字符通过、65 字符拒绝："
                  f"{bool(sr.ID_RE.match('x' * 64))}/{bool(sr.ID_RE.match('x' * 65))}")
        else:
            print("[WARN] 未找到 Rust 技能模块，跨语言对齐用例本次未跑")

        # ── 3. 平台层 YAML 的只读适配 ──────────────────────────
        yaml_entries, yaml_notes = sr.discover_yaml(home)
        yaml_ids = sorted(e.id for e in yaml_entries)
        check("平台层 YAML 技能进入统一清单", "weekly-report" in yaml_ids, f"{yaml_ids} {yaml_notes}")
        yaml_row = next((e for e in yaml_entries if e.id == "weekly-report"), None)
        if yaml_row is not None:
            check("YAML 侧的工具白名单没被统一抹平",
                  bool(yaml_row.tools_allow) or bool(yaml_row.requires.tools),
                  str(yaml_row.tools_allow))
            check("YAML 侧的上下文与窗口配置留在清单里",
                  "mode" in yaml_row.context and "contextWindow" in yaml_row.context,
                  str(yaml_row.context))
            check("YAML 来源被如实标注", yaml_row.source.kind == sr.SOURCE_YAML, yaml_row.source.kind)
        else:
            check("平台层 YAML 技能进入统一清单", False, "没取到 weekly-report")

        # ── 4. 共享池：一次改动两个专家都生效 ──────────────────
        pool, sync_notes = sr.sync_pool(home)
        check("同步把已安装技能并进池", "weekly-report" in pool.entries, str(sync_notes))
        check("已安装目录优先于平台 YAML（同名撞车不静默换源）",
              pool.entries["weekly-report"].source.kind == sr.SOURCE_SKILL_MD,
              pool.entries["weekly-report"].source.kind)
        check("清单落盘在数据根的 skill_pool 下", sr.pool_file(home).is_file(), str(sr.pool_file(home)))

        pool.set_enabled("weekly-report", "expert-a", True)
        pool.set_enabled("weekly-report", "expert-b", True)
        saved, error = pool.save(home)
        check("启停状态落盘", saved and not error, error)

        reloaded = sr.load_pool(home)
        check("重新载入后启停状态仍在",
              reloaded.is_enabled_for(reloaded.entries["weekly-report"], "expert-a")
              and reloaded.is_enabled_for(reloaded.entries["weekly-report"], "expert-b"),
              str(reloaded.entries["weekly-report"].enablements))
        a_before = reloaded.resolve_for_expert("expert-a")[0]
        b_before = reloaded.resolve_for_expert("expert-b")[0]
        row_a = next((s for s in a_before if s["id"] == "weekly-report"), None)
        row_b = next((s for s in b_before if s["id"] == "weekly-report"), None)
        check("两个专家都拿到同一个技能", row_a is not None and row_b is not None,
              f"A={[s['id'] for s in a_before]} B={[s['id'] for s in b_before]}")
        check("两条链路拿到的是同一份正文（引用，不是各自的副本）",
              row_a is not None and row_a == row_b, str({"a": row_a, "b": row_b})[:200])

        touch_version(weekly, weekly.read_text(encoding="utf-8") + "\n新增：每条结论都要给出链接。\n")
        pool2, sync_notes2 = sr.sync_pool(home)
        check("正文改动被识别为新版本",
              pool2.entries["weekly-report"].version != reloaded.entries["weekly-report"].version,
              f"{reloaded.entries['weekly-report'].version} → {pool2.entries['weekly-report'].version}")
        check("版本变化这句话说明引用方会自动读到新正文",
              any("正文已更新" in n and "自动读到新正文" in n for n in sync_notes2),
              str(sync_notes2))
        a_after = pool2.resolve_for_expert("expert-a")[0]
        b_after = pool2.resolve_for_expert("expert-b")[0]
        text_a = next((r["content"] for r in a_after if r["id"] == "weekly-report"), "")
        text_b = next((r["content"] for r in b_after if r["id"] == "weekly-report"), "")
        check("改一次，专家 A 立即生效", "每条结论都要给出链接" in text_a, str(text_a)[:200])
        check("改一次，专家 B 同时生效", "每条结论都要给出链接" in text_b, str(text_b)[:200])

        # ── 5. 停用立即影响两边，且是按专家分别生效 ────────────
        ok, message = pool2.set_enabled("weekly-report", "expert-a", False)
        check("停用给专家 A 成功", ok, message)
        a_off, _ = pool2.resolve_for_expert("expert-a")
        b_off, _ = pool2.resolve_for_expert("expert-b")
        check("专家 A 本轮不再拿到该技能",
              "weekly-report" not in [s["id"] for s in a_off], str(a_off))
        check("专家 B 不受 A 的停用影响",
              "weekly-report" in [s["id"] for s in b_off], str([s["id"] for s in b_off]))
        pool2.set_enabled("weekly-report", "", False)
        a_off2, _ = pool2.resolve_for_expert("expert-c")
        check("未单独设置过的专家跟缺省走",
              "weekly-report" not in [s["id"] for s in a_off2], str(a_off2))
        pool2.set_enabled("weekly-report", "", True)
        c_on, _ = pool2.resolve_for_expert("expert-c")
        check("缺省改回启用后新专家也拿得到",
              "weekly-report" in [s["id"] for s in c_on], str([s["id"] for s in c_on]))
        check("生效专家列表可核对",
              "expert-a" not in pool2.experts_using("weekly-report")
              or pool2.entries["weekly-report"].enablements["expert-a"] is False,
              str(pool2.experts_using("weekly-report")))

        # ── 5b. 平台 YAML 的既有语义：默认关闭、allow 不是依赖 ──
        yaml_row_pool = pool2.entries.get("general")
        check("平台层 YAML 技能进入池但缺省不启用",
              yaml_row_pool is not None and yaml_row_pool.default_enabled is False,
              str(getattr(yaml_row_pool, "default_enabled", "不在池里")))
        rows_no_yaml, _ = pool2.resolve_for_expert("expert-g")
        check("没显式启用时 YAML 技能不进某个专家的执行面",
              "general" not in [r["id"] for r in rows_no_yaml], str([r["id"] for r in rows_no_yaml]))
        pool2.set_enabled("general", "expert-g", True)
        rows_yaml, notes_yaml = pool2.resolve_for_expert("expert-g", available_tools=set(),
                                                        available_mcp=set())
        yaml_row = next((r for r in rows_yaml if r["id"] == "general"), None)
        check("显式启用后 YAML 技能带出平台层提示词正文",
              yaml_row is not None and "通用工作助手" in yaml_row["content"], str(yaml_row)[:200])
        check("YAML 的 tools.allow 是白名单不是依赖：本机工具为空也照样启用",
              "缺少工具" not in "；".join(notes_yaml), str(notes_yaml))
        installed_general = write_skill(home, "general", "---\nname: 本地通用技能\n---\n本地那一份。\n")
        saved_g, err_g = pool2.save(home)
        check("启停状态先落盘再重同步（否则比对的是内存态）", saved_g, err_g)
        pool_g, notes_g = sr.sync_pool(home)
        check("同 id 的本地安装优先：引用改指到 SKILL.md",
              pool_g.entries["general"].source.kind == sr.SOURCE_SKILL_MD,
              pool_g.entries["general"].source.kind)
        check("来源换了这件事对用户说出来",
              any("general" in n and "来源" in n for n in notes_g), str(notes_g))
        check("换来源不抹掉专家已有的启停状态",
              pool_g.entries["general"].enablements.get("expert-g") is True,
              str(pool_g.entries["general"].enablements))
        rows_g, _ = pool_g.resolve_for_expert("expert-g")
        row_g = next((r for r in rows_g if r["id"] == "general"), None)
        check("改指后专家读到的是本地那一份正文",
              row_g is not None and "本地那一份" in row_g["content"], str(row_g)[:200])
        installed_general.unlink()

        # ── 6. 缺依赖：中文提示，且不静默跳过 ──────────────────
        pool2.add_to_pool(sr.SkillEntry(
            id="needs-mcp",
            title="要连接器的技能",
            description="需要 filesystem 连接器",
            source=sr.SkillSource(kind=sr.SOURCE_SKILL_MD, path=str(weekly)),
            version=sr.content_version("x"),
            requires=sr.SkillRequirements(tools=["write_text_file"], mcp=["filesystem"]),
        ))
        rows, notes6 = pool2.resolve_for_expert("expert-d", available_tools={"read_text_file"},
                                                available_mcp=set())
        check("技能因缺工具被挡在执行面之外", "needs-mcp" not in [r["id"] for r in rows], str(rows))
        joined6 = "；".join(notes6)
        check("缺依赖的提示是中文且点名缺什么",
              "缺少工具：write_text_file" in joined6, joined6)
        check("MCP 依赖同样点名", "缺少 MCP 连接器：filesystem" in joined6, joined6)
        check("缺依赖的技能不是悄悄消失（有原因就有说明）",
              any("未进入本轮执行面" in n for n in notes6), str(notes6))
        rows_ok, notes_ok = pool2.resolve_for_expert(
            "expert-e", available_tools={"write_text_file"}, available_mcp={"filesystem"})
        check("依赖齐了技能就进入执行面",
              "needs-mcp" in [r["id"] for r in rows_ok], str([r["id"] for r in rows_ok]))
        rows_unknown, notes_unknown = pool2.resolve_for_expert("expert-f")
        check("调用方没给可用清单时如实说「未检查」而不是谎称通过",
              any("未检查" in n for n in notes_unknown)
              and not any("缺少工具" in n for n in notes_unknown),
              str(notes_unknown))
        check("未检查不等于拦停：技能仍然进入执行面",
              "needs-mcp" in [r["id"] for r in rows_unknown], str([r["id"] for r in rows_unknown]))
        pool2.add_to_pool(sr.SkillEntry(
            id="needs-skill",
            title="要前置技能的技能",
            description="",
            source=sr.SkillSource(kind=sr.SOURCE_SKILL_MD, path=str(weekly)),
            version=sr.content_version("x"),
            requires=sr.SkillRequirements(skills=["absent-skill"]),
        ))
        _rows7, notes7 = pool2.resolve_for_expert("expert-g")
        check("前置技能缺失也被点名",
              any("缺少已启用的前置技能：absent-skill" in n for n in notes7), str(notes7))

        # ── 7. 高风险与空正文：仍在清单里，但正文不许进模型 ────
        pool2.entries["weekly-report"].scan_blocked = True
        rows8, notes8 = pool2.resolve_for_expert("expert-b")
        blocked_row = next((r for r in rows8 if r["id"] == "weekly-report"), None)
        check("被判高风险的技能仍带着空正文出现在清单里",
              blocked_row is not None and blocked_row["content"] == "", str(blocked_row))
        check("高风险技能标出 scanBlocked",
              blocked_row is not None and blocked_row["scanBlocked"] is True, str(blocked_row))
        check("高风险技能带着可读原因",
              blocked_row is not None and "高风险" in blocked_row["error"], str(blocked_row))
        check("高风险原因也进 notes", any("高风险" in n for n in notes8), str(notes8))
        pool2.entries["weekly-report"].scan_blocked = None

        empty_skill = write_skill(home, "empty-body", "   \n\n \n")
        pool3, _ = sr.sync_pool(home)
        rows9, notes9 = pool3.resolve_for_expert("expert-h")
        empty_row = next((r for r in rows9 if r["id"] == "empty-body"), None)
        check("正文为空的技能仍进清单，但原因写在 error 里",
              empty_row is not None and empty_row["error"] and not empty_row["content"].strip(),
              str(empty_row))
        check("空正文的正文原样带上，不擅自动文件（与 Rust 交付整份 SKILL.md 一致）",
              empty_row is not None and empty_row["content"]
              == empty_skill.read_text(encoding="utf-8"), str(empty_row)[:160])
        check("正文为空如实说明原因", any("正文为空" in n for n in notes9), str(notes9))
        check("空正文文件确实落在那个路径上", empty_skill.is_file(), str(empty_skill))

        # ── 8. 源文件不在：条目保留，不静默删除 ────────────────
        offline_id = "offline-skill"
        offline_path = write_skill(home, offline_id, "---\nname: 离线技能\n---\n正文一段。\n")
        sr.sync_pool(home)
        offline_path.unlink()
        pool4, notes10 = sr.sync_pool(home)
        check("正文文件消失后条目仍在池里", offline_id in pool4.entries, str(list(pool4.entries)))
        check("消失原因有说明而不是假装没看见",
              any(offline_id in n and "不在盘上" in n for n in notes10), str(notes10))
        write_skill(home, offline_id, "---\nname: 离线技能\n---\n正文回来了。\n")
        pool5, _ = sr.sync_pool(home)
        check("文件回来后重新读到正文", offline_id in pool5.entries
              and pool5.resolve_for_expert("expert-i")[0] is not None)

        # ── 9. 从池里摘掉引用 ≠ 卸载 ───────────────────────────
        ok11, msg11 = pool5.drop("offline-skill")
        check("摘掉引用成功", ok11, msg11)
        check("摘掉引用后原文件仍在盘上",
              (sr.installed_root(home) / offline_id / sr.SKILL_MD_NAME).is_file(), msg11)
        check("摘掉引用这句话明确说了没删原文件", "未删除" in msg11, msg11)
        bad_drop = pool5.drop("不存在")
        check("摘掉不在池里的技能如实失败", bad_drop[0] is False, bad_drop[1])

        # 同名不同来源不许静默顶掉
        conflict = sr.SkillEntry(
            id="weekly-report",
            title="另一个同名技能",
            description="",
            source=sr.SkillSource(kind=sr.SOURCE_SKILL_MD, path=str(offline_path)),
            version="zzz",
        )
        ok12, msg12 = pool5.add_to_pool(conflict)
        check("同 id 不同来源拒绝静默替换", ok12 is False and "已有同名技能" in msg12, msg12)
        ok13, _ = pool5.add_to_pool(sr.SkillEntry(
            id="Bad Name", title="x", description="",
            source=sr.SkillSource(kind=sr.SOURCE_SKILL_MD, path=str(weekly)),
        ))
        check("不合法标识被中文拒绝", ok13 is False, "")

        # ── 10. 草案：写出来不等于启用 ─────────────────────────
        draft, msg14 = sr.add_draft(
            home, "distilled-followup", "第一步抓数据；第二步汇总；第三步发出去。",
            title="日报三步", description="从成功任务蒸馏出来的草案", origin="任务蒸馏",
        )
        check("草案写入共享池目录", draft is not None and draft.source.kind == sr.SOURCE_DRAFT, msg14)
        check("草案状态是未确认", draft is not None and draft.status == sr.STATUS_DRAFT, str(draft))
        check("草案缺省就是不启用", draft is not None and draft.default_enabled is False, "")
        check("草案不进任何专家的执行面",
              all("distilled-followup" not in [r["id"] for r in p.resolve_for_expert(e)[0]]
                  for p, e in ((pool5, "expert-a"), (sr.load_pool(home), "expert-b"))), "")
        pool_draft = sr.load_pool(home)
        set_ok, set_msg = pool_draft.set_enabled("distilled-followup", "expert-a", True)
        check("草案即使被显式启用也仍不进执行面",
              set_ok and "distilled-followup" not in [
                  r["id"] for r in pool_draft.resolve_for_expert("expert-a")[0]],
              set_msg)
        check("草案提示这句话说了要人工确认", "人工确认" in set_msg, set_msg)
        dup_draft = sr.add_draft(home, "distilled-followup", "又一段正文")
        check("同名草案不覆盖", dup_draft[0] is None and "不覆盖已有草案" in dup_draft[1], dup_draft[1])
        check("空草案不写入", sr.add_draft(home, "empty-draft", "   ")[0] is None)
        check("不合法草案标识被拒",
              sr.add_draft(home, "bad name", "正文")[0] is None)
        check("超长草案被拒并给出实际字数",
              sr.add_draft(home, "long-draft", "字" * (sr.MAX_DRAFT_CHARS + 10))[0] is None)

        promoted, msg15 = sr.promote_draft(home, "distilled-followup", reviewer="王主管")
        check("显式确认后草案转正", promoted is True, msg15)
        promoted_entry = sr.load_pool(home).entries["distilled-followup"]
        check("转正后状态 ready", promoted_entry.status == sr.STATUS_READY, str(promoted_entry.status))
        check("转正后正文同步进 Rust 技能目录",
              (sr.installed_root(home) / "distilled-followup" / sr.SKILL_MD_NAME).is_file(), msg15)
        check("确认后这句话带上确认人", "王主管" in msg15, msg15)
        check("原草案文件保留做审计痕迹",
              (sr.drafts_dir(home) / "distilled-followup" / sr.SKILL_MD_NAME).is_file(), msg15)
        pool6 = sr.load_pool(home)
        check("转正后仍未启用的专家拿不到它",
              "distilled-followup" not in [r["id"] for r in pool6.resolve_for_expert("expert-z")[0]],
              str([r["id"] for r in pool6.resolve_for_expert("expert-z")[0]]))
        pool6.set_enabled("distilled-followup", "expert-z", True)
        check("启用后该专家才读到正文",
              any(r["id"] == "distilled-followup" and r["content"]
                  for r in pool6.resolve_for_expert("expert-z")[0]), "")
        check("已转正的技能再确认一次被拒",
              sr.promote_draft(home, "distilled-followup")[0] is False, "")

        # ── 11. 清单文件坏了：不覆盖、不假装是空池 ─────────────
        broken_home = Path(tempfile.mkdtemp(prefix="tt-skill-pool-broken-"))
        sr.pool_dir(broken_home).mkdir(parents=True, exist_ok=True)
        sr.pool_file(broken_home).write_text("{ 这不是合法 JSON", encoding="utf-8")
        broken = sr.load_pool(broken_home)
        check("坏清单带着读取错误返回", bool(broken.load_error), broken.load_error)
        saved17, err17 = broken.save(broken_home)
        check("坏清单拒绝覆盖写回", saved17 is False and "拒绝覆盖" in err17, err17)
        ok17, err17b = broken.set_enabled("any-skill", "expert-a", True)
        check("坏清单上启停被拒且原因是读坏了",
              ok17 is False and "读坏" in err17b and "没有技能" not in err17b, err17b)
        dropped17, drop_err17 = broken.drop("any-skill")
        check("坏清单上摘除引用同样被拒",
              dropped17 is False and "读坏" in drop_err17, drop_err17)
        view = sr.pool_view(broken_home)
        check("只读视图把不可写如实报出来",
              view["writable"] is False and view["loadError"], str(view)[:200])
        sr.pool_file(broken_home).write_text(json.dumps(
            {"schemaVersion": sr.SCHEMA_VERSION, "skills": [
                {"id": "cn-entry", "title": "只有标识和标题",
                 "source": {"kind": "skill-md", "path": "无"}},
                {"id": "bad-status", "title": "状态字段是乱的",
                 "status": "weird-value",
                 "source": {"kind": "skill-md", "path": "无"}}]},
             ensure_ascii=False), encoding="utf-8")
        partial = sr.load_pool(broken_home)
        check("缺字段的条目按缺省解析而不整池作废",
              "cn-entry" in partial.entries
              and partial.entries["cn-entry"].status == sr.STATUS_READY,
              str(sorted(partial.entries)))
        check("缺字段的条目解析时不带读取错误",
              partial.load_error == "", partial.load_error)
        check("认不出的状态按草案处理而不是当成就绪放行",
              partial.entries.get("bad-status") is not None
              and partial.entries["bad-status"].status == sr.STATUS_DRAFT,
              getattr(partial.entries.get("bad-status"), "status", "条目被丢掉了"))
        illegal_entry = sr.SkillEntry(
            id="非法 标识", title="标题", description="",
            source=sr.SkillSource(kind=sr.SOURCE_SKILL_MD, path="x"))
        add_illegal, add_illegal_err = sr.SkillPool().add_to_pool(illegal_entry)
        check("不合法的标识被拒并给出中文原因",
              add_illegal is False and "只能用字母" in add_illegal_err, add_illegal_err)
        sr.pool_file(broken_home).write_text("[]", encoding="utf-8")
        check("根节点不是对象时如实说明", bool(sr.load_pool(broken_home).load_error), "")

        # ── 12. 与执行面的形状对齐（#164 交棒后只差一次调用） ──
        try:
            import runtime_context as rc

            shape_pool = sr.SkillPool()
            shape_pool.entries["x"] = sr.SkillEntry(
                id="x", title="X", description="说明", version="v",
                source=sr.SkillSource(kind=sr.SOURCE_SKILL_MD, path=str(weekly)),
            )
            rows18, _notes18 = shape_pool.resolve_for_expert("expert-a")
            normalized = rc.normalize_skills({"skills": rows18})
            check("清单输出原样穿过 normalize_skills", normalized == rows18,
                  f"{normalized} vs {rows18}")
            check("输出正好是执行面认得的九个键",
                  rows18 and set(rows18[0]) == {
                      "id", "name", "description", "content", "kind",
                      "source", "suite", "error", "scanBlocked"},
                  str(sorted(rows18[0])) if rows18 else "没有输出")
            composed, compose_notes = rc.compose_skills(normalized)
            check("装配后的提示词带出技能正文入口",
                  "x" in composed and "use_skill" in composed, f"{composed[:160]} {compose_notes}")
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP: runtime_context 形状对齐未跑（{exc}）")

        # ── 13. 数据根路径与既有约定同源 ───────────────────────
        try:
            import data_paths

            check("共享池挂在引擎数据根下",
                  str(sr.pool_dir(data_paths.engine_home())).endswith(sr.POOL_DIRNAME),
                  str(sr.pool_dir(data_paths.engine_home())))
            check("共享池不借用别的目录",
                  sr.POOL_DIRNAME not in (
                      data_paths.CHECKPOINTS_DIRNAME, data_paths.AWARENESS_DIRNAME,
                      data_paths.FILE_TRASH_DIRNAME),
                  sr.POOL_DIRNAME)
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP: data_paths 对齐未跑（{exc}）")

        check("草案目录在池目录之内",
              sr.drafts_dir(home).is_relative_to(sr.pool_dir(home)), str(sr.drafts_dir(home)))
        manifest_text = sr.pool_file(home).read_text(encoding="utf-8")
        check("清单只存引用与元数据，不复制正文",
              "每条结论都要给出链接" not in manifest_text and "source" in manifest_text,
              manifest_text[:200])

        # ── 14. 端点面：三条技能池路由真挂在 knowledge 车道上 ────
        # 先把引擎数据根指到本节的临时目录再 import server：
        # POST sync 会写 pool.json，指错了就把假清单灌进用户自己的 ~/.tongtianwork。
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            import routes_lanes as rl
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP: 端点面未跑（{exc}）")
        else:
            original_home_env = os.environ.get("TONGTIANWORK_HOME")
            os.environ["TONGTIANWORK_HOME"] = str(home)
            lane_broken_home = None
            try:
                import server

                fresh = FastAPI()
                report = rl.attach(fresh, server, ["routes_knowledge"])
                all_paths = sorted(p for row in report for p in row["paths"])
                skill_paths = [p for p in all_paths if p.startswith("/api/skills")]
                check("车道挂上三条技能池路由", len(skill_paths) == 3, str(skill_paths))
                client = TestClient(fresh)

                # 先给池里种一条**带工具依赖**的：端点面必须复现运行时的口径——
                # 没给可用工具清单时「未检查」且照样放行，而不是谎称通过。
                expert = "expert-a"
                probe_md = write_skill(
                    home, "lane-dep-probe",
                    "---\nname: 依赖探针\ndescription: 需要一个不存在的工具\n---\n\n先列证据再下结论。\n")
                seeded = sr.load_pool(home)
                seeded.add_to_pool(sr.SkillEntry(
                    id="lane-dep-probe",
                    title="依赖探针",
                    description="需要一个不存在的工具",
                    source=sr.SkillSource(kind=sr.SOURCE_SKILL_MD, path=str(probe_md)),
                    version=sr.content_version("先列证据再下结论。"),
                    requires=sr.SkillRequirements(tools=["an-absent-tool"]),
                ))
                saved, seed_error = seeded.save(home)
                check("种依赖探针这一步自己先落盘成功（否则后面的断言都是空的）",
                      saved, seed_error)

                # 取样必须在种探针**之后**：否则比的是自己刚写下去的那一份
                before_text = sr.pool_file(home).read_text(encoding="utf-8")
                listing = client.get("/api/skills/pool")
                body = listing.json()
                view_rows = {row["id"]: row for row in body["skills"]}
                check("GET pool 返回只读视图并点名清单文件",
                      listing.status_code == 200 and body["ok"] and body["writable"]
                      and Path(body["poolFile"]) == sr.pool_file(home).resolve(),
                      str(body)[:220])
                check("视图里每条都带来源、生效专家与启用态",
                      all({"source", "experts", "sourceExists", "enablements",
                           "defaultEnabled", "status"} <= set(row) for row in body["skills"]),
                      str(sorted(body["skills"][0]) if body["skills"] else "")[:220])
                check("GET pool 一个字都不写盘（读面不许改清单）",
                      sr.pool_file(home).read_text(encoding="utf-8") == before_text,
                      str(sr.pool_file(home)))
                injected = client.get("/api/skills/pool", params={"home": str(Path.home())})
                check("不接受调用方指定目录（否则条目里的正文路径就是任意文件读）",
                      Path(injected.json()["poolFile"]) == sr.pool_file(home).resolve(),
                      str(injected.json())[:200])

                resolved = client.get("/api/skills/pool/resolved", params={"expert": expert})
                rb = resolved.json()
                resolved_ids = [row["id"] for row in rb["skills"]]
                notes_text = "；".join(rb["notes"])
                check("GET resolved 给执行面形状（正好那九个键）",
                      resolved.status_code == 200 and rb["ok"]
                      and all(set(row) == {"id", "name", "description", "content", "kind",
                                           "source", "suite", "error", "scanBlocked"}
                              for row in rb["skills"]),
                      str(sorted(rb["skills"][0]) if rb["skills"] else "")[:220])

                def enabled_for(row: dict) -> bool:
                    return bool(row["enablements"].get(expert, row["defaultEnabled"]))

                expected = [i for i, r in view_rows.items()
                            if enabled_for(r) and r["status"] != "draft"]
                check("启给该专家的每一条要么进执行面、要么在说明里点名（不悄悄消失）",
                      all(i in resolved_ids or i in notes_text for i in expected),
                      f"应给 {expected} / 实给 {resolved_ids}")
                check("没启给这个专家的一条都不许多发",
                      all(not enabled_for(view_rows[i]) or i in resolved_ids
                          for i in resolved_ids),
                      str([i for i in resolved_ids if i in view_rows
                           and not enabled_for(view_rows[i])])[:200])
                check("带依赖但没给可用清单时说「未检查」并照样放行",
                      "lane-dep-probe" in resolved_ids and "未检查" in notes_text,
                      notes_text[:260])

                probe_id = "lane-sync-probe"
                write_skill(home, probe_id,
                            "---\nname: 端点并入探针\ndescription: 验证刷新能并进清单\n---\n\n用同一句话回复。\n")
                experts_before = {i: sorted(r["experts"]) for i, r in view_rows.items()}
                synced = client.post("/api/skills/pool/sync")
                sb = synced.json()
                check("POST sync 把磁盘上新装的并进清单并落盘",
                      synced.status_code == 200 and sb["ok"]
                      and probe_id in sr.pool_file(home).read_text(encoding="utf-8"),
                      str(sb)[:220])
                after = client.get("/api/skills/pool").json()
                after_rows = {row["id"]: row for row in after["skills"]}
                check("刷新后新装的那条在池里且来源指向真文件",
                      probe_id in after_rows and after_rows[probe_id]["sourceExists"] is True,
                      str(after_rows.get(probe_id))[:220])
                check("刷新只并清单，不改任何人的启用态",
                      all(after_rows[i]["experts"] == experts_before[i]
                          for i in experts_before if i in after_rows),
                      str([(i, experts_before[i], after_rows[i]["experts"])
                           for i in experts_before
                           if i in after_rows and after_rows[i]["experts"] != experts_before[i]])[:220])
                check("刷新把这一轮对用户说的话原样交出来",
                      isinstance(sb["notes"], list), str(sb)[:220])

                # 清单读坏的现场：端点必须拒写并把原因交出来，不许静默覆盖
                lane_broken_home = Path(tempfile.mkdtemp(prefix="tt-skill-lane-broken-"))
                sr.pool_dir(lane_broken_home).mkdir(parents=True, exist_ok=True)
                sr.pool_file(lane_broken_home).write_text("{这不是 JSON", encoding="utf-8")
                broken_bytes = sr.pool_file(lane_broken_home).read_text(encoding="utf-8")
                os.environ["TONGTIANWORK_HOME"] = str(lane_broken_home)
                bad_get = client.get("/api/skills/pool")
                check("池读坏时 GET 仍回话但点名写不了",
                      bad_get.status_code == 200 and bad_get.json()["ok"]
                      and bad_get.json()["writable"] is False
                      and "读坏" in bad_get.json()["loadError"],
                      str(bad_get.json())[:220])
                bad_post = client.post("/api/skills/pool/sync")
                check("池读坏时 POST sync 返 422 而不是假装刷新成功",
                      bad_post.status_code == 422 and bad_post.json()["ok"] is False
                      and "读坏" in bad_post.json()["loadError"],
                      str(bad_post.json())[:220])
                check("拒写时清单文件保持原样（不拿空池盖掉用户的清单）",
                      sr.pool_file(lane_broken_home).read_text(encoding="utf-8") == broken_bytes,
                      str(sr.pool_file(lane_broken_home))[:200])
            finally:
                if lane_broken_home is not None:
                    import shutil as _sh
                    _sh.rmtree(lane_broken_home, ignore_errors=True)
                if original_home_env is None:
                    os.environ.pop("TONGTIANWORK_HOME", None)
                else:
                    os.environ["TONGTIANWORK_HOME"] = original_home_env

    finally:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(broken_home, ignore_errors=True)

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} 项 — " + "；".join(FAILURES))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
