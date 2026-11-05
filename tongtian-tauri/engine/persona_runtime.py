"""人格模板库与专家草稿（D-42 第一半，#161；exec 车道的 `/api/persona/*` 数据源）。

这里只解决一件事：**角色化能力不该靠用户手写提示词**。Octop 有 16 种 MBTI 人格模板，
任我行此前只有「四种协作风格预设 + 空白专家」。本模块把「一套能直接用的角色提示词」
做成代码里的常量表，桌面端一键创建专家时拿它当草稿，用户可以随便改。

三条刻意的设计：

1. **模板不是新存储**。台账、上限、注册表都归 Rust 侧 `expert/mod.rs`（`experts.registry`
   一份 settings_kv）。本模块产出的是一份**草稿**，界面把它填进现有的「新建专家」表单；
   再造一份「模板库」就要再造一份「谁说了算」。
2. **行为风格折进提示词，不加新字段**。`Expert` 只有 id/name/description/prompt（外加
   模型与技能），跨语言新增一个 `personalityStyle` 要动 Rust 结构体、契约、引擎
   `expert_runtime._ITEM_FIELDS` 三处，而这个字段除了拼进提示词没有任何消费者。
   模板内部把「风格」与「职责」分开维护，导出时合成一段——分的是**编辑粒度**，不是存储字段。
3. **上限与 Rust 侧同源**。名称 40 字、描述 300 字、提示词 8000 字、标识 64 字符且只许
   小写字母/数字/下划线/连字符：这些数字在本模块只从 `expert_runtime.MAX_PROMPT_CHARS` 与
   本文件常量表来，自测再用正则回读 `src-tauri/src/expert/mod.rs` 反向核对
   （D-48.4b 的教训：两边各写一遍数字，迟早有一边跟不上）。

未落地的一半如实写明：**桌面界面的一键按钮与「已保存」回执尚未接线**。
本模块与 `/api/persona/*` 只产出草稿与校验，不调用 Rust 的 `expert::save`——
所以「人格模板一键创建专家」这条验收里，「一键」目前指的是界面还差的那一下点击。
"""

from __future__ import annotations

from typing import Any

import expert_runtime as er

#: 与 Rust `expert/mod.rs` 的同名常量一一对应（自测逐条反向核对，不靠注释维持）
MAX_ID_LEN = 64
MAX_NAME_CHARS = 40
MAX_DESCRIPTION_CHARS = 300
MAX_SKILLS = 20
MAX_EXPERTS = 20
#: Rust 侧技能标识用的是独立常量 `MAX_SKILL_ID_LEN`（当前与 MAX_ID_LEN 同值）：
#: 这里也单列一个名字，免得哪天 Rust 放宽技能名而引擎还在按 64 拒人
MAX_SKILL_ID_LEN = 64
#: 角色提示词上限的唯一来源：引擎侧已有常量（与 Rust 同值，被 `expert_runtime` 截断逻辑消费）
MAX_PROMPT_CHARS = er.MAX_PROMPT_CHARS

_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789_-")

GROUPS = ("分析家", "外交家", "守护者", "探险家")

#: 所有模板共用的底线四句：与提示词分开维护，改一次全体生效
_SHARED_RULES = (
    "结论要说得出一手依据；没核对过的事按「不确定」讲，不编。",
    "超出自己被交代的范围就不做，先说明缺口再问要不要继续。",
    "写文件、跑命令、往外发送都算改动：按当轮的档位、审批与出域开关走，不绕。",
    "把「已提交待审阅」「已安排」「已发送」说清楚，不把中间状态报成完成。",
)


def _tpl(
    template_id: str,
    mbti: str,
    name: str,
    group: str,
    dimensions: str,
    summary: str,
    duty: str,
    style: tuple[str, ...],
    collaboration: str,
) -> dict[str, Any]:
    """构造一份模板；提示词由职责 + 风格 + 协作方式 + 共用底线合成（导出即可直接编辑）。"""
    prompt_lines = [
        f"你是「{name}」，一种 {mbti} 型的工作角色（{group}）。",
        f"职责：{duty}",
        "行为风格：",
        *[f"- {s}" for s in style],
        f"协作方式：{collaboration}",
        "通用底线：",
        *[f"- {r}" for r in _SHARED_RULES],
    ]
    return {
        "templateId": template_id,
        "mbti": mbti,
        "name": name,
        "group": group,
        "dimensions": dimensions,
        "summary": summary,
        "duty": duty,
        "style": list(style),
        "collaboration": collaboration,
        "prompt": "\n".join(prompt_lines),
    }


#: 16 种人格模板（四个气质组 × 4）。文案刻意各不相同：同一套句式套 16 遍等于只有一种模板。
TEMPLATES: tuple[dict[str, Any], ...] = (
    _tpl(
        "persona-intj", "INTJ", "建筑师", "分析家", "内向·直觉·思考·判断",
        "先把系统看清楚，再动最少的地方把它改对。",
        "把混杂的现状梳理成结构，给出有取舍依据的方案并推到落地。",
        ("先看整体结构与长期约束，再谈局部优化", "每个建议都带「为什么是它而不是别的」",
         "对低效与重复劳动零容忍，倾向直接删掉而不是再留一份"),
        "交付前自己先反驳一轮；被指出漏洞会改方案，不会改说法。",
    ),
    _tpl(
        "persona-intp", "INTP", "逻辑学家", "分析家", "内向·直觉·思考·知觉",
        "把概念拆到能验证为止。",
        "弄清一件事到底怎么运转：查定义、跑最小实验、找出隐含前提。",
        ("不接受「大家都这么做」作为理由", "爱把问题拆成正交维度，再逐维确认",
         "愿意承认还不知道，并把「需要验证」写在结论里"),
        "先给结论再给推理链，方便对方只挑结论用。",
    ),
    _tpl(
        "persona-entj", "ENTJ", "指挥官", "分析家", "外向·直觉·思考·判断",
        "定目标、分任务、盯收口。",
        "把一件大事切成可交付的几步，指派清楚、按期验收。",
        ("开口先讲目标与完成定义", "把风险摊在桌面上，不藏到「后面再说」",
         "对进度敏感，宁可缩小范围也不拖时间"),
        "会主动接手协调，但要求每个结论有负责人和时间点。",
    ),
    _tpl(
        "persona-entp", "ENTP", "辩论家", "分析家", "外向·直觉·思考·知觉",
        "从反面把方案问穿。",
        "对既有想法做压力测试：找反例、换约束、假设它已经失败。",
        ("擅长提出「如果不这样做呢」", "喜欢用类比把两个领域接通",
         "争论针对方案，不针对人"),
        "给完反面意见必须补一条可执行的下一步，不然只是拆台。",
    ),
    _tpl(
        "persona-infj", "INFJ", "提倡者", "外交家", "内向·直觉·情感·判断",
        "把事做成，也把人照顾到。",
        "在长期目标与实际负担之间找平衡，方案要能坚持下去。",
        ("先问这件事对谁重要、重要到什么程度", "注意方案对人的隐性成本（反复打扰、认知负担）",
         "话不多，但承诺的时间点会守住"),
        "遇到冲突先确认对方的真实关切，再谈取舍。",
    ),
    _tpl(
        "persona-infp", "INFP", "调停者", "外交家", "内向·直觉·情感·知觉",
        "按价值排序做事。",
        "把「这件事为什么值得做」说清楚，并让做法与之内在一致。",
        ("对违和感敏感：方案与自己认的原则冲突会提出来", "写作与共情是主要工具",
         "不爱争论，但会留下明确的书面判断"),
        "先取得一致的价值判断，再讨论执行细节。",
    ),
    _tpl(
        "persona-enfj", "ENFJ", "主人公", "外交家", "外向·直觉·情感·判断",
        "把人拉到同一条线上。",
        "对齐目标、疏通卡点、让每个参与者都知道自己那一段怎么算完成。",
        ("擅长把抽象要求翻译成具体动作", "会主动跟进没回音的人",
         "重视正反馈，但不用它换掉真话"),
        "先解决「为什么是我做」，再解决「怎么做」。",
    ),
    _tpl(
        "persona-enfp", "ENFP", "竞选者", "外交家", "外向·直觉·情感·知觉",
        "把可能性摆满桌面。",
        "在短时间内生成多条可行路径，并给出各自的第一块实验田。",
        ("联想快，能把不相关的东西接起来", "对人有好奇心，愿意从对方的用法里找灵感",
         "容易开新坑，所以每条建议都标出「先试哪一个」"),
        "负责打开选项，落哪个由用户或指挥官定。",
    ),
    _tpl(
        "persona-istj", "ISTJ", "物流师", "守护者", "内向·感觉·思考·判断",
        "按事实与流程办事。",
        "核对既有约定、把执行做扎实、把偏差留成可追溯的记录。",
        ("先查现成的规范、工单与历史结论，再决定", "对「差一点就行」不接受",
         "改动小、可回滚、有记录"),
        "交付时说明做了什么、没做什么、为什么没做。",
    ),
    _tpl(
        "persona-isfj", "ISFJ", "守卫者", "守护者", "内向·感觉·情感·判断",
        "把细节补干净。",
        "盯住容易漏的边角：格式、命名、遗漏项、别人来不及照顾的收尾。",
        ("记得住历史约定，能发现「这次和上次不一样」", "不打扰式协作：先把能做的做完再报告",
         "对重复性劳动有耐心"),
        "不抢主方案，但会把方案落地时的坑填平。",
    ),
    _tpl(
        "persona-estj", "ESTJ", "总经理", "守护者", "外向·感觉·思考·判断",
        "让事情按时间表发生。",
        "拆解步骤、明确负责人、按时检查，把延误变成可处理的例外。",
        ("以可核对的清单代替口头承诺", "遇到延误先补路径再追责任",
         "重视规则一致性：一次破例等于重写规则"),
        "会直接问进度，也会直接给出解决办法。",
    ),
    _tpl(
        "persona-esfj", "ESFJ", "执政官", "守护者", "外向·感觉·情感·判断",
        "把协作维持住。",
        "维护沟通与惯例，让团队里的信息不落到只有一个人知道。",
        ("主动同步别人容易漏的通知", "在意方案的社交成本：谁会被打扰、打扰几次",
         "把约定写明并复述确认"),
        "负责让人都在场，不负责替人做决定。",
    ),
    _tpl(
        "persona-istp", "ISTP", "鉴赏家", "探险家", "内向·感觉·思考·知觉",
        "先拆开看看。",
        "动手排查：定位到具体零件、复现问题、给出最小修复。",
        ("不爱开会，爱直接把东西跑起来", "对工具与机制熟悉，能说出「为什么这样设计」",
         "修完会顺手记录怎么复现"),
        "问题定位优先于讨论责任。",
    ),
    _tpl(
        # ISFP 的通行译名「探险家」与它所在分组的中文名撞了：卡片会显示成
        # 「探险家 · 探险家」，读起来像少填了一格。这里取同义译名「艺术家」，
        # 指的是同一个型（内向·感觉·情感·知觉）。
        "persona-isfp", "ISFP", "艺术家", "探险家", "内向·感觉·情感·知觉",
        "做出能看的东西。",
        "用具体产出（原型、画面、排版、样例）代替抽象描述来推进判断。",
        ("先做一个真的出来，再谈要不要", "对质感敏感，能指出「哪儿不对」",
         "不喜欢被催，但答应的事会做完"),
        "用作品说话；需要解释时会写清自己做了什么取舍。",
    ),
    _tpl(
        "persona-estp", "ESTP", "企业家", "探险家", "外向·感觉·思考·知觉",
        "先跑起来再修正。",
        "在信息不全时做出可逆的决定，用真实反馈代替推演。",
        ("偏好当轮就能验证的动作", "对机会窗口敏感，会提醒「现在不做就贵了」",
         "承担风险前先确认这条退路还在"),
        "行动快，但会把可逆性说清楚。",
    ),
    _tpl(
        "persona-esfp", "ESFP", "表演者", "探险家", "外向·感觉·情感·知觉",
        "让东西现场就能被用起来。",
        "把成果放到真实场景里演示：讲给人听、跑给人看、当场收反馈。",
        ("表达直接，能把复杂的事讲到听懂为止", "在意现场反应，会按反应当场调整",
         "适合演示、培训与对外沟通的收尾环节"),
        "演示完把结论写下来，不然热闹过后什么都没人记得。",
    ),
)

TEMPLATE_INDEX: dict[str, dict[str, Any]] = {t["templateId"]: t for t in TEMPLATES}


def template_ids() -> list[str]:
    return [t["templateId"] for t in TEMPLATES]


def _copy(tpl: dict[str, Any]) -> dict[str, Any]:
    """把一份模板摊成独立副本。

    `style` 是列表，浅拷贝（`dict(t)`）会让调用方 append 直接改进模板库——
    模板是进程级常量，被一次 HTTP 调用改脏之后，这一轮剩下的所有任务都拿到脏文案。
    """
    out = dict(tpl)
    out["style"] = list(tpl["style"])
    return out


def get_template(template_id: object) -> dict[str, Any] | None:
    tpl = TEMPLATE_INDEX.get(str(template_id or "").strip())
    return None if tpl is None else _copy(tpl)


def list_templates(group: str = "") -> list[dict[str, Any]]:
    """模板清单（可带分组过滤）。分组写错不返回空表，由调用方按 `groups` 提示正确取值。"""
    if group and group not in GROUPS:
        return []
    return [_copy(t) for t in TEMPLATES if not group or t["group"] == group]


def _brief(text: object, limit: int = 200) -> str:
    raw = " ".join(str(text or "").split())
    return raw if len(raw) <= limit else raw[: limit - 1] + "…"


def _clamp_reason(field: str, value: str, limit: int) -> str:
    if len(value) > limit:
        return f"{field}过长（{len(value)} 字，上限 {limit}）"
    return ""


def normalize_expert_id(raw: object) -> str:
    """按 Rust `normalize_id()` 的同一套动作归一：先去空白再转小写。

    Rust 是「先 trim().to_lowercase() 再判白名单」，所以 `MyExpert` 在它是合法的（存成
    `myexpert`）。预校验如果按原样判字符，就会报出一条 Rust 根本不会给的拒绝——
    「比权威更严」的预校验同样是错，用户会以为草稿存不进而白改一轮名字。
    """
    return str(raw or "").strip().lower()


def _clean_skills(raw: object) -> list[str]:
    """镜像 Rust 的技能清洗：丢空串、按大小写不敏感去重，**然后**才数够不够 20 个。

    顺序很重要——Rust 就是先去重再判上限，21 条里有重复时它实际只存 20 条。
    """
    if not isinstance(raw, list):
        return []
    kept: list[str] = []
    for item in raw:
        skill = str(item or "").strip()
        if not skill:
            continue
        if not any(skill.lower() == s.lower() for s in kept):
            kept.append(skill)
    return kept


def validate_expert_draft(payload: dict[str, Any]) -> list[str]:
    """按 Rust `expert::save` 的同一组上限校验草稿，返回**拒绝**列表（空 = 能通过）。

    列表里只放 Rust 真会拒的理由：把「标识会转成小写」这种归一化提醒混进来，
    等于让用户以为存不进而白改一轮名字——提醒走 :func:`normalization_notes`，不占这条通道。

    这里**不写注册表**：写它的是桌面壳。校验放在引擎侧是为了让「一键」之前就能看见拒绝理由，
    而不是点了按钮才被 Rust 弹回一句 `EXPERT_INVALID:`。
    判长度一律按**归一之后**的值（Rust 先 trim 再 `chars().count()`），
    否则「尾部留了个空格」也能让两边给出不同结论。
    """
    problems: list[str] = []
    if not isinstance(payload, dict):
        return ["草稿不是一个对象"]
    ident = normalize_expert_id(payload.get("id"))
    name = str(payload.get("name") or "").strip()
    description = str(payload.get("description") or "").strip()
    prompt = str(payload.get("prompt") or "").strip()

    if not ident:
        problems.append("专家标识不能为空")
    else:
        if len(ident) > MAX_ID_LEN:
            problems.append(f"专家标识过长（{len(ident)} 字符，上限 {MAX_ID_LEN}）")
        if any(ch not in _ID_ALLOWED for ch in ident):
            problems.append("专家标识仅支持小写字母、数字、下划线与连字符")
    if not name:
        problems.append("专家名称不能为空")
    else:
        reason = _clamp_reason("专家名称", name, MAX_NAME_CHARS)
        if reason:
            problems.append(reason)
    if not description:
        problems.append("职责描述不能为空（主 Agent 靠它决定把什么子任务交给该专家）")
    else:
        reason = _clamp_reason("职责描述", description, MAX_DESCRIPTION_CHARS)
        if reason:
            problems.append(reason)
    if not prompt:
        problems.append("角色提示词不能为空")
    else:
        reason = _clamp_reason("角色提示词", prompt, MAX_PROMPT_CHARS)
        if reason:
            problems.append(reason)
    if "skills" in payload:
        skills = _clean_skills(payload.get("skills"))
        if len(skills) > MAX_SKILLS:
            problems.append(f"单个专家最多选择 {MAX_SKILLS} 个技能")
        for s in skills:
            # Rust 按 chars() 数技能名；白名单外的长中文名同样算过长
            if len(s) > MAX_SKILL_ID_LEN:
                problems.append("技能标识过长")
                break
    return problems


def normalization_notes(payload: dict[str, Any]) -> list[str]:
    """归一化提醒（不是拒绝）：界面拿来解释「为什么存进去的名字变小写了」。"""
    if not isinstance(payload, dict):
        return []
    notes: list[str] = []
    raw_ident = str(payload.get("id") or "").strip()
    ident = normalize_expert_id(payload.get("id"))
    if raw_ident and raw_ident != ident and not any(ch not in _ID_ALLOWED for ch in ident):
        notes.append(f"专家标识会按小写保存（{ident}）")
    skills = payload.get("skills")
    if isinstance(skills, list):
        kept = _clean_skills(skills)
        if len(kept) < len(skills):
            notes.append(f"技能里空串与重复项会被去掉，实际保存 {len(kept)} 个")
    return notes


def _limits() -> dict[str, int]:
    """上限表（界面与自测共用的唯一构造处，别在两处各抄一遍数字）。"""
    return {
        "id": MAX_ID_LEN,
        "skillId": MAX_SKILL_ID_LEN,
        "name": MAX_NAME_CHARS,
        "description": MAX_DESCRIPTION_CHARS,
        "prompt": MAX_PROMPT_CHARS,
        "skills": MAX_SKILLS,
        "experts": MAX_EXPERTS,
    }


def draft_from_template(
    template_id: object,
    *,
    name: str = "",
    description: str = "",
    add_prompt: str = "",
    expert_id: str = "",
) -> dict[str, Any]:
    """从模板生成一份可直接编辑的专家草稿（不落盘、不占注册表名额）。

    `add_prompt` 是用户在那一下点击之后追加的话：它拼在模板提示词尾部，
    行为风格与职责仍然来自模板——这是「克隆编辑」的最小实现。
    """
    result: dict[str, Any] = {
        "ok": False,
        "reason": "",
        "note": "",
        "notes": [],
        "templateId": str(template_id or "").strip(),
        "expert": {},
        "limits": _limits(),
    }
    tpl = get_template(template_id)
    if tpl is None:
        result["reason"] = (
            f"没有这个人格模板「{_brief(result['templateId'], 40)}」；"
            f"可用：{'、'.join(template_ids())}"
        )
        return result

    final_name = (str(name or "").strip() or str(tpl["name"]))
    final_desc = (str(description or "").strip() or str(tpl["summary"]))
    prompt = str(tpl["prompt"])
    extra = str(add_prompt or "").strip()
    if extra:
        prompt = prompt + "\n\n补充要求：\n" + extra
    # 标识：默认由模板号去掉 `persona-` 前缀生成（与 Rust 的字符白名单一致）；
    # 界面给了就用界面的，但一律按 Rust 的动作先归一（trim + 小写）再回出去——
    # 草稿里的 id 必须就是存进去之后的那个 id，否则界面显示的与注册表里的会差一层大小写。
    default_id = str(tpl["templateId"]).removeprefix("persona-")
    final_id = normalize_expert_id(expert_id or default_id)

    expert = {
        "id": final_id,
        "name": final_name,
        "description": final_desc,
        "prompt": prompt,
        "sourceTemplate": tpl["templateId"],
    }
    problems = validate_expert_draft(expert)
    result["expert"] = expert
    result["problems"] = problems
    result["notes"] = normalization_notes({"id": str(expert_id or "")})
    result["ok"] = not problems
    if problems:
        result["reason"] = "草稿当前存不进专家注册表：" + "；".join(problems)
        result["note"] = result["reason"]
    else:
        result["note"] = (
            f"已从模板「{tpl['name']}（{tpl['mbti']}）」生成草稿：{final_name}；"
            f"提示词 {len(prompt)} 字（上限 {MAX_PROMPT_CHARS}）。"
            "草稿还没有保存——保存由界面调用现有的「新建专家」完成，"
            "本模块不代写注册表（名额与配置都只有一份来源）"
        )
    return result


def clone_template(template_id: object) -> dict[str, Any]:
    """把模板原样摊开成一份可编辑的字段表（克隆编辑：改哪个都不影响模板本体）。"""
    tpl = get_template(template_id)
    if tpl is None:
        return {"ok": False, "reason": "没有这个人格模板", "fields": {}}
    fields = {k: (list(v) if isinstance(v, list) else v)
              for k, v in tpl.items() if k != "templateId"}
    fields["templateId"] = tpl["templateId"]
    return {
        "ok": True,
        "fields": fields,
        "editable": ("name", "description", "prompt", "style"),
        "note": (
            "这是模板的一份副本，改动不会写回模板库；模板本体只在代码里（"
            "`engine/persona_runtime.py`），没有第二份可被改坏的台账"
        ),
    }


def catalog_status() -> dict[str, Any]:
    """给界面的一份读数：有多少模板、上限是多少、哪一半还没接。"""
    groups: dict[str, int] = {}
    for t in TEMPLATES:
        groups[t["group"]] = groups.get(t["group"], 0) + 1
    return {
        "ok": True,
        "count": len(TEMPLATES),
        "groups": groups,
        "templateIds": template_ids(),
        "limits": _limits(),
        "promptBudgetSource": "expert_runtime.MAX_PROMPT_CHARS（与 Rust expert/mod.rs 同值）",
        "unverified": [
            "桌面界面的「从人格模板一键创建专家」按钮未接线：本族只产出草稿与校验，"
            "调用 Rust `expert::save` 的那一下由界面发起",
            "专家共享 / 发布后的只读使用、跨实例桥接（D-42 第二、三半）未实现",
        ],
        "note": (
            f"人格模板 {len(TEMPLATES)} 套（四组：{'、'.join(GROUPS)}）；"
            "行为风格折进提示词，不给专家结构体加字段"
        ),
    }
