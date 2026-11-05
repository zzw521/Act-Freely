#!/usr/bin/env python3
"""list_directory / read_document 自测（补齐「不能列目录、不能读 Word/Excel/PDF」的能力缺口）

锁住四件事：

1. `list_directory` 真能列目录：深度 1/2/3、单页上限与截断提示、大小标注、越界拒绝，
   遇到「不是目录 / 不存在」如实报错，而不是假装目录是空的；
2. `read_document` 真能从 .docx/.xlsx/.pptx/.pdf 抽取正文（不是改名后的文本），
   文本类文件走原文，空文档/扫描件/加密 PDF/不支持的格式各自给出可操作的中文原因；
3. **三个读取件的分页预算**（D-48.4b）：正文默认 4000、上限 8000 字符，列表默认 20、上限 50 项，
   越界的请求**如实带话**而不是默默归一化；截断时给出的 `start=` / `offset=` **必须真能翻**——
   用例把各页拼回去逐字符比对原文，并钉住「末页不许喊后面还有」；
4. 这组数字**四处同源**：执行体常量（`ticket_worker_adapter`）、主会话工具声明、`SYS_PROMPT`
   与 Rust `harness/tools.rs` 的常量各写一遍就会漂，所以按常量反查文案与对侧源码；
5. **按剩余窗口取页**（D-48.8，引擎主会话这一面）：`build_toolkit(extra={"read_window": …})`
   给了本轮读数时，三个读取件都**现取**读数并按剩余窗口压小这一页（只压不抬、留余量、
   永不清零、下限按页自己的单位给）；没给读数、读数还没填、或读数坏掉时逐字回到固定预算，
   而且页面里不许出现一句关于窗口的话。

只读+t 临时目录，不碰用户真实工作区与数据目录。解析库缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_document_tools_selftest.py
"""

import os
import re
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

TEMP_HOME = tempfile.mkdtemp(prefix="tt-doc-home-")
os.environ.setdefault("TONGTIANWORK_HOME", TEMP_HOME)

try:
    import server as engine_server
    import tool_approval
    import checkpoint_runtime
    import ticket_worker_adapter as twa
    from agentscope.tool import ToolResponse  # noqa: F401 - 确认注册表可用
    import docx
    import openpyxl
    from pptx import Presentation
    from pypdf import PdfReader, PdfWriter
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine document tools selftest unavailable ({exc})")
    sys.exit(0)

REQUIRED_TOOLS = {
    "list_directory",
    "view_text_file",
    "read_document",
    "write_text_file",
    "insert_text_file",
    "delete_text_file",
}

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def tool_text(resp) -> str:
    return "\n".join(
        str(block.get("text", "") if isinstance(block, dict) else getattr(block, "text", ""))
        for block in (getattr(resp, "content", None) or [])
    )


def entry_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("[目录] ") or line.startswith("[文件] ")]


def view_split(text: str) -> tuple[str, str]:
    """view_text_file 的返回形状：截断时「一行预算说明 + 正文」，没话可说时原样给正文。

    取正文不能拿第一个换行当分界——正文自己就有很多换行；只在首行确实是预算说明时才切。
    """
    head, sep, rest = text.partition("\n")
    if sep and ("单元" in head or "字符" in head):
        return head, rest
    return "", text


def doc_split(text: str) -> tuple[str, str, str]:
    """read_document 的返回形状：头一行 / 空行 / 正文（末尾可能追加一行预算说明）。"""
    head, _sep, rest = text.partition("\n")
    _blank, _s, body_and_note = rest.partition("\n")
    before, sep, tail = body_and_note.rpartition("\n")
    if sep and ("单元" in tail or "字符" in tail):
        return head, before, tail
    return head, body_and_note, ""


def build_pdf(path: Path, body: str) -> None:
    """手写最小 PDF（含正确 xref 偏移），不依赖任何生成库，pypdf 能抽出原文字。"""
    content = f"BT /F1 24 Tf 72 700 Td ({body}) Tj ET".encode("latin-1")
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 5 0 R /Resources << /Font << /F1 4 0 R >> >> >>"
        ),
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        5: b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    }
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in range(1, 6):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for num in range(1, 6):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref_at).encode() + b"\n%%EOF\n"
    path.write_bytes(bytes(out))


def build_fixtures(root: Path) -> None:
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (root / "sub" / "deep").mkdir()
    (root / "sub" / "deep" / "c.txt").write_text("c", encoding="utf-8")
    (root / "empty").mkdir()

    document = docx.Document()
    document.add_heading("项目周报", level=1)
    document.add_paragraph("本周完成了引擎能力补齐。")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "模块"
    table.rows[0].cells[1].text = "状态"
    document.save(str(root / "report.docx"))

    empty_doc = docx.Document()
    empty_doc.add_paragraph("   ")
    empty_doc.save(str(root / "blank.docx"))

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "销售"
    sheet.append(["区域", "金额"])
    sheet.append(["华东", 1200])
    workbook.save(str(root / "sales.xlsx"))

    empty_workbook = openpyxl.Workbook()
    empty_workbook.active.title = "空表"
    empty_workbook.save(str(root / "empty.xlsx"))

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = "季度汇报"
    slide.placeholders[1].text = "营收同比增长 18%"
    presentation.save(str(root / "deck.pptx"))

    build_pdf(root / "plain.pdf", "Hello PDF 2026")

    blank_writer = PdfWriter()
    blank_writer.add_blank_page(width=612, height=792)
    with (root / "blank.pdf").open("wb") as handle:
        blank_writer.write(handle)

    locked_writer = PdfWriter()
    locked_writer.add_blank_page(width=612, height=792)
    locked_writer.encrypt("s3cret")
    with (root / "locked.pdf").open("wb") as handle:
        locked_writer.write(handle)

    (root / "notes.md").write_text("# 标题\n正文内容", encoding="utf-8")
    (root / "legacy.doc").write_bytes(b"\xd0\xcf\x11\xe0old-word")
    (root / "long.md").write_text("x" * 3000, encoding="utf-8")
    # 分页用例的靶子：一份超默认预算的正文、一份按行排好的正文、一个条目多于单页上限的目录
    (root / "huge.md").write_text("x" * 9000, encoding="utf-8")
    (root / "many_lines.txt").write_text(
        "\n".join(f"L{i:04d}:" + "d" * 35 for i in range(1, 401)), encoding="utf-8"
    )
    (root / "zoo").mkdir()
    for index in range(30):
        (root / "zoo" / f"item{index:02d}.txt").write_text("z", encoding="utf-8")


def registry_checks() -> None:
    print("--- 1. 注册表：新工具真的挂在引擎里，且属于只读 ---")
    tmp = Path(tempfile.mkdtemp(prefix="tt-doc-reg-"))
    toolkit = engine_server.build_toolkit(tmp)
    names = set(toolkit.tools.keys())
    check("常驻工具正好是六件文件工具", names == REQUIRED_TOOLS, str(sorted(names)))
    check("list_directory 已注册", "list_directory" in names)
    check("read_document 已注册", "read_document" in names)
    slide_toolkit = engine_server.build_toolkit(tmp, slides=True)
    check("幻灯片模式仍额外注册 generate_pptx", "generate_pptx" in slide_toolkit.tools)
    check(
        "审批链路把新工具当只读",
        {"list_directory", "read_document"} <= set(tool_approval.READ_ONLY_TOOLS),
        str(sorted(tool_approval.READ_ONLY_TOOLS)),
    )
    check(
        "检查点链路把新工具当只读",
        {"list_directory", "read_document"} <= set(checkpoint_runtime._READ_ONLY_TOOLS),
        str(sorted(checkpoint_runtime._READ_ONLY_TOOLS)),
    )


def list_checks(workroot: Path) -> None:
    print("--- 2. list_directory：深度/上限/边界/错误 ---")
    toolkit = engine_server.build_toolkit(workroot)
    list_dir = toolkit.tools["list_directory"].original_func

    top = tool_text(list_dir("."))
    check("含目录头与单页口径", "目录：" in top and "深度 1，" in top and "单页上限 20 条" in top, top[:200])
    check("一页列完不许喊「后面还有」", "接着列请带" not in top, top[-200:])
    check("列出根文件与大小", "[文件] a.txt  5 B" in top, top[:300])
    check("列出子目录", "[目录] sub/" in top and "[目录] empty/" in top, top[:300])
    check("深度 1 不下钻", "sub/b.txt" not in top, top[:300])

    two = tool_text(list_dir(str(workroot), depth=2))
    check("深度 2 下钻一层", "[文件] sub/b.txt" in two, two[:400])
    check("深度 2 不继续下钻", "sub/deep/c.txt" not in two, two[:400])

    three = tool_text(list_dir(".", depth=3))
    check("深度 3 下钻到底", "[文件] sub/deep/c.txt" in three, three[:500])
    clamped = tool_text(list_dir(".", depth=99))
    check("深度超过 3 被夹到 3", "深度 3" in clamped and "[文件] sub/deep/c.txt" in clamped, clamped[:200])

    limited = tool_text(list_dir(".", limit=2))
    check("条数按请求取 2 并如实带话", len(entry_lines(limited)) == 2 and "按你的请求取 2" in limited, limited[:300])
    check("首页点名本次第 1–2 条", "本次第 1–2 条" in limited, limited[:200])
    check("截断给出下一页参数", "接着列请带 offset=2" in limited, limited[-200:])
    second = tool_text(list_dir(".", limit=2, offset=2))
    check("第二页接续且不重叠", "本次第 3–4 条" in second and len(entry_lines(second)) == 2, second[:200])
    check(
        "翻页确实换了条目且接着首页",
        entry_lines(second) != entry_lines(limited)
        and entry_lines(limited) + entry_lines(second) == entry_lines(top)[:4],
        entry_lines(second)[:2],
    )

    over = tool_text(list_dir(".", limit=500))
    check("limit=500 夹到 50 并说明", "「500」超出上限 50" in over and "单页上限 50 条" in over, over[:250])
    floor = tool_text(list_dir(".", limit=0))
    check("limit=0 回落默认 20 并说明", "不是正数" in floor and "单页上限 20 条" in floor, floor[:250])
    junk = tool_text(list_dir(".", limit="很多"))
    check("非整数的条数请求带话而不是默默按默认", "不是整数" in junk and "单页上限 20 条" in junk, junk[:250])
    # 根目录条数是夹具算出来的，不写死：越界偏移回落「从头列起」时页码要跟着事实走
    root_rows = len(entry_lines(top))
    neg_off = tool_text(list_dir(".", offset=-3))
    check(
        "负 offset 从头列起并说明",
        "负数" in neg_off and f"本次第 1–{root_rows} 条" in neg_off,
        neg_off[:250],
    )
    bad_off = tool_text(list_dir(".", offset="abc"))
    check(
        "非整数 offset 从头列起并说明",
        "不是整数" in bad_off and f"本次第 1–{root_rows} 条" in bad_off,
        bad_off[:250],
    )

    # 单页 20 条、目录 30 条：末页必须既给全剩下的，又不喊「后面还有」
    zoo = tool_text(list_dir("zoo"))
    check("超单页的目录只给 20 条", len(entry_lines(zoo)) == 20, str(len(entry_lines(zoo))))
    check("首页喊还有下一页", "接着列请带 offset=20" in zoo, zoo[-200:])
    zoo2 = tool_text(list_dir("zoo", offset=20))
    check("末页给到第 30 条", len(entry_lines(zoo2)) == 10 and "本次第 21–30 条" in zoo2, zoo2[:200])
    check("末页不许喊「后面还有」", "接着列请带" not in zoo2, zoo2[-200:])
    gone = tool_text(list_dir("zoo", offset=99))
    check("偏移越过末尾如实说明", "这个偏移之后没有条目了" in gone, gone[:250])

    empty = tool_text(list_dir("empty"))
    check("空目录如实说明", "（目录为空）" in empty, empty[:200])

    outside = workroot.parent / f"{workroot.name}-outside"
    outside.mkdir(exist_ok=True)
    try:
        denied = tool_text(list_dir(str(outside)))
        check("越界目录被拒绝", "超出工作目录" in denied, denied[:200])
    finally:
        outside.rmdir()

    as_file = tool_text(list_dir("a.txt"))
    check("文件路径报「不是目录」", "不是目录" in as_file, as_file[:200])
    missing = tool_text(list_dir("nope"))
    check("不存在的目录如实报错", "目录不存在" in missing, missing[:200])


def document_checks(workroot: Path) -> None:
    print("--- 3. read_document：真实解析四种文档 + 错误路径 ---")
    toolkit = engine_server.build_toolkit(workroot)
    read_doc = toolkit.tools["read_document"].original_func

    docx_text = tool_text(read_doc("report.docx"))
    check("docx 有读取头", "已读取：" in docx_text and "（docx）" in docx_text, docx_text[:200])
    check("docx 段落正文", "本周完成了引擎能力补齐。" in docx_text, docx_text[:300])
    check("docx 标题", "项目周报" in docx_text, docx_text[:300])
    check("docx 表格转成行", "模块\t状态" in docx_text, docx_text[:400])

    xlsx_text = tool_text(read_doc("sales.xlsx"))
    check("xlsx 标注工作表名", "## 工作表：销售" in xlsx_text, xlsx_text[:300])
    check("xlsx 输出单元格", "华东\t1200" in xlsx_text, xlsx_text[:300])

    pptx_text = tool_text(read_doc("deck.pptx"))
    check("pptx 标注页码", "## 第 1 页" in pptx_text, pptx_text[:300])
    check("pptx 抽到标题与正文", "季度汇报" in pptx_text and "营收同比增长 18%" in pptx_text, pptx_text[:300])

    pdf_text = tool_text(read_doc("plain.pdf"))
    check("pdf 标注扩展名", "（pdf）" in pdf_text, pdf_text[:200])
    check("pdf 抽出正文", "Hello PDF 2026" in pdf_text, pdf_text[:300])

    md_text = tool_text(read_doc("notes.md"))
    check("文本类文件原样返回", "# 标题" in md_text and "正文内容" in md_text, md_text[:200])

    blank_doc = tool_text(read_doc("blank.docx"))
    check("空白 docx 不假装有内容", "未能从" in blank_doc and "抽取到正文" in blank_doc, blank_doc[:250])

    empty_xlsx = tool_text(read_doc("empty.xlsx"))
    check("空表格如实报未抽取到正文", "未能从" in empty_xlsx, empty_xlsx[:250])

    blank_pdf = tool_text(read_doc("blank.pdf"))
    check("无文字 PDF 提示扫描件/OCR", "抽取到正文" in blank_pdf and "OCR" in blank_pdf, blank_pdf[:300])

    locked = tool_text(read_doc("locked.pdf"))
    check("加密 PDF 提示先解密", "已加密" in locked and "密码" in locked, locked[:250])

    unsupported = tool_text(read_doc("legacy.doc"))
    check("不支持的格式列出支持项", "不支持的文档格式" in unsupported and ".docx" in unsupported, unsupported[:250])

    missing = tool_text(read_doc("nope.pdf"))
    check("不存在的文件如实报错", "文件不存在" in missing, missing[:200])

    outside = workroot.parent / f"{workroot.name}-x.pdf"
    outside.write_bytes(b"%PDF-1.4")
    try:
        denied = tool_text(read_doc(str(outside)))
        check("越界文件被拒绝", "超出工作目录" in denied, denied[:200])
    finally:
        outside.unlink()

    clipped = tool_text(read_doc("long.md", max_chars=1000))
    _h, body_clip, note_clip = doc_split(clipped)
    check("按请求取 1000 字符并说明低于默认", "按你的请求取 1000" in note_clip, clipped[-250:])
    check("截断点名还剩多少", "后面还有 2000 个单元" in note_clip, clipped[-250:])
    check("截断给下一页参数", "start=1000" in note_clip, clipped[-250:])
    check("正文正好是前 1000 字符", body_clip == "x" * 1000, str(len(body_clip)))
    next_page = tool_text(read_doc("long.md", max_chars=1000, start=1000))
    _h2, body_next, note_next = doc_split(next_page)
    check("第二页接续正文", body_next == "x" * 1000 and "start=2000" in note_next, str(len(body_next)))
    too_small = tool_text(read_doc("long.md", max_chars=1))
    _h3, body_one, note_one = doc_split(too_small)
    check("请求 1 字符就给 1 字符（不夹下限）", body_one == "x" and "本次只给这些" in note_one, repr(body_one[:20]))
    huge_default = tool_text(read_doc("huge.md"))
    _h4, body_huge, note_huge = doc_split(huge_default)
    check("缺省预算夹在 4000 字符", len(body_huge) == 4000 and "后面还有 5000 个单元" in note_huge, str(len(body_huge)))
    over_ask = tool_text(read_doc("huge.md", max_chars=50000))
    check("超上限的请求被夹住并劝分页", "「50000」超出上限 8000" in over_ask and "要读更多请分页" in over_ask, over_ask[-250:])
    past_end = tool_text(read_doc("long.md", start=9000))
    check("偏移越过末尾如实说明没内容", "已超过正文长度" in past_end, past_end[-250:])
    plain = tool_text(read_doc("notes.md"))
    check("小文件不追加预算说明", "单元" not in plain and "已读取：" in plain, plain[:200])


def budget_source_checks(workroot: Path) -> None:
    print("--- 5. 分页预算四处同源（执行体常量 / 工具声明 / 系统提示 / Rust harness 常量）---")
    check(
        "执行体常量是正文 4000/8000、列表 20/50",
        (twa.TEXT_READ_DEFAULT_UNITS, twa.TEXT_READ_MAX_UNITS, twa.LIST_DEFAULT_ITEMS, twa.LIST_MAX_ITEMS)
        == (4000, 8000, 20, 50),
        str((twa.TEXT_READ_DEFAULT_UNITS, twa.TEXT_READ_MAX_UNITS, twa.LIST_DEFAULT_ITEMS, twa.LIST_MAX_ITEMS)),
    )
    toolkit = engine_server.build_toolkit(workroot)
    docs = {name: str(toolkit.tools[name].original_func.__doc__ or "") for name in
            ("view_text_file", "read_document", "list_directory")}
    # 工具声明是模型唯一能看见的预算说明：数字与常量各写一遍就会漂，所以按常量反查文案
    check(
        "正文两件把常量写进了声明",
        all(f"默认 {twa.TEXT_READ_DEFAULT_UNITS}、上限 {twa.TEXT_READ_MAX_UNITS}" in docs[name]
            for name in ("view_text_file", "read_document")),
        docs["view_text_file"][:200],
    )
    check(
        "列表把常量写进了声明",
        f"默认 {twa.LIST_DEFAULT_ITEMS}、上限 {twa.LIST_MAX_ITEMS}" in docs["list_directory"],
        docs["list_directory"][:200],
    )
    prompt_text = engine_server.SYS_PROMPT
    check(
        "系统提示也写了同一组数字",
        f"正文默认 {twa.TEXT_READ_DEFAULT_UNITS}、上限 {twa.TEXT_READ_MAX_UNITS} 字符" in prompt_text
        and f"目录清单默认 {twa.LIST_DEFAULT_ITEMS}、上限 {twa.LIST_MAX_ITEMS} 项" in prompt_text,
        prompt_text[-200:],
    )
    rust_file = PROJECT_ROOT / "src-tauri" / "src" / "harness" / "tools.rs"
    check("Rust harness 源文件在位（跨语言同源的核对对象）", rust_file.exists(), str(rust_file))
    if rust_file.exists():
        nums = {
            key: int(value.replace("_", ""))
            for key, value in re.findall(r"const ([A-Z_]+): usize = ([0-9_]+);",
                                          rust_file.read_text(encoding="utf-8"))
        }
        check(
            "Rust 侧常量与引擎同源",
            (nums.get("TEXT_UNITS_DEFAULT"), nums.get("TEXT_UNITS_MAX"),
             nums.get("LIST_ITEMS_DEFAULT"), nums.get("LIST_ITEMS_MAX"))
            == (twa.TEXT_READ_DEFAULT_UNITS, twa.TEXT_READ_MAX_UNITS,
                twa.LIST_DEFAULT_ITEMS, twa.LIST_MAX_ITEMS),
            str(nums),
        )


def view_checks(workroot: Path) -> None:
    print("--- 4. view_text_file：行轴 + 字符预算 + 分页拼回原文 ---")
    toolkit = engine_server.build_toolkit(workroot)
    view = toolkit.tools["view_text_file"].original_func

    body = (workroot / "huge.md").read_text(encoding="utf-8")  # 9000 个字符、没有换行
    p1 = tool_text(view("huge.md"))
    note1, b1 = view_split(p1)
    check("缺省按 4000 字符给页", len(b1) == 4000 and b1 == body[:4000], str(len(b1)))
    check("预算说明点名单元是字符", "单元=字符，非 token" in note1, note1[:200])
    check("点名还剩多少并给下一页参数", f"后面还有 {len(body) - 4000} 个单元" in note1 and "start=4000" in note1, note1[-200:])
    p2 = tool_text(view("huge.md", start=4000))
    note2, b2 = view_split(p2)
    check("第二页接续正文并把指针前移", b2 == body[4000:8000] and "start=8000" in note2, str(len(b2)))
    p3 = tool_text(view("huge.md", start=8000))
    note3, b3 = view_split(p3)
    check("末页原样给正文、不追加装饰", b3 == body[8000:] and note3 == "", str(len(b3)))
    check("三页拼回去正好等于原文", b1 + b2 + b3 == body, f"{len(b1)}+{len(b2)}+{len(b3)} vs {len(body)}")

    over = tool_text(view("huge.md", units=50000))
    note_over, b_over = view_split(over)
    check("units 超上限被夹到 8000 并说明", f"超出上限 {twa.TEXT_READ_MAX_UNITS}" in note_over and len(b_over) == 8000, note_over[:200])
    small = tool_text(view("huge.md", units=10))
    note_small, b_small = view_split(small)
    check("请求低于默认只给请求量并说明", len(b_small) == 10 and "本次只给这些" in note_small, str(len(b_small)))
    junk = tool_text(view("huge.md", units="很多"))
    check("非整数 units 带话而不是默默按默认", "不是整数" in junk and len(view_split(junk)[1]) == 4000, junk[:200])
    junk_start = tool_text(view("huge.md", start="前面"))
    check("非整数 start 从头读并说明", "从头开始读" in junk_start, junk_start[:200])
    past = tool_text(view("huge.md", start=90000))
    check("start 越过正文末尾如实说明没内容", "已超过正文长度" in past, past[-200:])

    lines = (workroot / "many_lines.txt").read_text(encoding="utf-8").splitlines()
    first100 = "\n".join(lines[:100])
    ranged = tool_text(view("many_lines.txt", start_line=1, end_line=100))
    note_r, body_r = view_split(ranged)
    check(
        "行范围超预算时按字符夹并说明剩多少",
        body_r == first100[:4000] and f"后面还有 {len(first100) - 4000} 个单元" in note_r,
        str(len(body_r)),
    )
    tail_r = tool_text(view("many_lines.txt", start_line=1, end_line=100, start=4000))
    _n2, body_t = view_split(tail_r)
    check("行范围内也能翻页拼回", body_r + body_t == first100, f"{len(body_r)}+{len(body_t)} vs {len(first100)}")
    whole = tool_text(view("many_lines.txt", start_line=2, end_line=3))
    check("预算内的小范围原样返回", whole == "\n".join(lines[1:3]), whole[:80])
    check("行号越界不假装有内容", tool_text(view("many_lines.txt", start_line=500, end_line=600)) == "", "非空")


class BrokenWindow:
    """读数取不到（通道正在被换掉那一类）：工具面必须把它当「没有窗口」，不许把异常抛给读取。"""

    def reading(self) -> dict:
        raise RuntimeError("窗口读数读不到（用例设定）")


class OddWindow:
    def reading(self):
        return "不是字典"


def window_paging_checks(workroot: Path) -> None:
    print("--- 6. 主会话三个读取件按剩余窗口取页（D-48.8：三面里的引擎这一面）---")
    body = (workroot / "huge.md").read_text(encoding="utf-8")

    # ① 没接窗口读数的装配：与改造前逐字相同，页面里也不许多出一句关于窗口的好话
    plain = engine_server.build_toolkit(workroot)
    plain_note, plain_body = view_split(tool_text(plain.tools["view_text_file"].original_func("huge.md")))
    check(
        "没给 read_window 的装配仍按固定预算给页（自测与老装配点行为不变）",
        len(plain_body) == twa.TEXT_READ_DEFAULT_UNITS and plain_body == body[:twa.TEXT_READ_DEFAULT_UNITS],
        str(len(plain_body)),
    )
    check("没给窗口读数时页面一个字也不提窗口（说了就是在说谎）", "窗口" not in plain_note, plain_note[:160])

    # ② 先建后填：装配时窗口还没归一化算完，迟到的读数也必须生效
    meter = twa.ReadWindowMeter()
    toolkit = engine_server.build_toolkit(workroot, extra={"read_window": meter})
    view = toolkit.tools["view_text_file"].original_func
    list_dir = toolkit.tools["list_directory"].original_func
    read_doc = toolkit.tools["read_document"].original_func
    pre_note, pre_body = view_split(tool_text(view("huge.md")))
    check(
        "窗口还没填时走固定预算（装配期不许猜窗口）",
        len(pre_body) == twa.TEXT_READ_DEFAULT_UNITS and "窗口" not in pre_note,
        str(len(pre_body)),
    )
    meter.configure(8192, True)
    wide_note, wide_body = view_split(tool_text(view("huge.md")))
    check(
        "窗口宽到不用压时不收窄，也不提窗口（只压不抬）",
        len(wide_body) == twa.TEXT_READ_DEFAULT_UNITS and "窗口" not in wide_note,
        f"{len(wide_body)}|{wide_note[:120]}",
    )
    wide_list = tool_text(list_dir("zoo"))
    check(
        "列表在宽窗口下照旧给缺省那一页且不提窗口",
        len(entry_lines(wide_list)) == twa.LIST_DEFAULT_ITEMS and "窗口" not in wide_list,
        str(len(entry_lines(wide_list))),
    )

    # ③ 本轮用量报上来：三个读取件各自现取读数，不是只有正文那两件
    meter.note_usage(7500)
    tight_note, tight_body = view_split(tool_text(view("huge.md")))
    check(
        "本轮用量报上来后正文页真的变小（读到的是现值，不是装配期快照）",
        twa.WINDOW_PAGE_FLOOR_UNITS < len(tight_body) < twa.TEXT_READ_DEFAULT_UNITS,
        str(len(tight_body)),
    )
    check(
        "压小那页点名是剩余窗口压的，并把窗口与用量念出来",
        "剩余上下文窗口" in tight_note and "8192" in tight_note and "7500" in tight_note,
        tight_note[:200],
    )
    check("折算口径在页面里写明（不许被读成厂商 tokenizer 计数）", "折算值不是厂商" in tight_note, tight_note[-200:])
    check("页变小后截断说明照旧给得出下一页指针", "start=" in tight_note and "后面还有" in tight_note, tight_note[-160:])

    rebuilt = ""
    cursor = 0
    for _ in range(40):
        page_note, page_body = view_split(tool_text(view("huge.md", start=cursor)))
        if not page_body:
            break
        rebuilt += page_body
        cursor += len(page_body)
        if "后面还有" not in page_note:
            break
    check(
        "窗口压小后一页一页仍拼得回原文（少给不等于读不到）",
        rebuilt == body and cursor < len(body) * 2,
        f"{len(rebuilt)} vs {len(body)}（游标 {cursor}）",
    )

    doc_head, doc_body, doc_tail = doc_split(tool_text(read_doc("huge.md")))
    check(
        "第三件 read_document 也吃到本轮读数",
        twa.WINDOW_PAGE_FLOOR_UNITS < len(doc_body) < twa.TEXT_READ_DEFAULT_UNITS and "剩余上下文窗口" in doc_tail,
        f"{len(doc_body)}|{doc_tail[:160]}|{doc_head[:60]}",
    )

    meter.note_usage(8000)
    tight_items = len(entry_lines(tool_text(list_dir("zoo"))))
    check(
        "列表按这一页真实条目的长度被压小（缺省那 20 条今天给不满）",
        twa.WINDOW_PAGE_FLOOR_ITEMS <= tight_items < twa.LIST_DEFAULT_ITEMS,
        str(tight_items),
    )
    check("列表压小时也说清是窗口压的，不是默默少列", "窗口" in tool_text(list_dir("zoo")), tool_text(list_dir("zoo"))[:200])

    # ④ 窗口按实测用满：按下限给一页，永不清零；下限必须按页自己的单位给
    meter.note_usage(8192)
    floor_note, floor_body = view_split(tool_text(view("huge.md")))
    check(
        "实测用满时正文仍按下限给一页（给空正文会让模型反复申请同一页）",
        len(floor_body) == twa.WINDOW_PAGE_FLOOR_UNITS,
        str(len(floor_body)),
    )
    check("下限那页如实写明「已经用满」且不许被读成「读完了」", "已经用满" in floor_note and "不是「读完了」" in floor_note, floor_note[:220])
    floor_items = len(entry_lines(tool_text(list_dir("zoo"))))
    check(
        "列表的下限按**条**给（拿正文那 200 个单元当列表下限会顶穿固定上限，等于压不动）",
        floor_items == twa.WINDOW_PAGE_FLOOR_ITEMS,
        str(floor_items),
    )

    # ⑤ 读数坏掉不拦读取
    broken_toolkit = engine_server.build_toolkit(workroot, extra={"read_window": BrokenWindow()})
    broken_view = broken_toolkit.tools["view_text_file"].original_func
    raised = None
    try:
        broken_text = tool_text(broken_view("huge.md"))
    except Exception as exc:  # noqa: BLE001
        broken_text, raised = "", exc
    check(
        "窗口读数抛了不影响读取：吞成「没有窗口」，照样给固定预算那一页",
        raised is None and len(view_split(broken_text)[1]) == twa.TEXT_READ_DEFAULT_UNITS,
        repr(raised),
    )
    odd_toolkit = engine_server.build_toolkit(workroot, extra={"read_window": OddWindow()})
    odd_note, _odd_body = view_split(tool_text(odd_toolkit.tools["view_text_file"].original_func("huge.md")))
    check("读数返回怪形状也当没有窗口（不拿它去压页）", "窗口" not in odd_note, odd_note[:160])

    # ⑥ 同源：窗口算术只住在执行体那一层，主会话只调不另写一套
    server_src = (PROJECT_ROOT / "engine" / "server.py").read_text(encoding="utf-8")
    adapter_src = (PROJECT_ROOT / "engine" / "ticket_worker_adapter.py").read_text(encoding="utf-8")
    check(
        "主会话不另写一套窗口算术（留量比例与预算函数都不在 server.py 里复述）",
        "WINDOW_RESERVE_FRACTION" not in server_src and "def window_budget" not in server_src,
        "",
    )
    check(
        "主会话列表用的是条目那套下限，正文那 200 个单元不许冒充",
        "floor=twa.WINDOW_PAGE_FLOOR_ITEMS" in server_src,
        "",
    )
    check(
        "两套下限各按各的单位（正文=字符、列表=条目），且只有一份来源",
        (twa.WINDOW_PAGE_FLOOR_UNITS, twa.WINDOW_PAGE_FLOOR_ITEMS) == (200, 3)
        and adapter_src.count("WINDOW_PAGE_FLOOR_ITEMS = ") == 1,
        str((twa.WINDOW_PAGE_FLOOR_UNITS, twa.WINDOW_PAGE_FLOOR_ITEMS)),
    )


def main() -> None:
    print(f"engine document tools selftest (work root: {TEMP_HOME})")
    workroot = Path(tempfile.mkdtemp(prefix="tt-doc-work-"))
    build_fixtures(workroot)
    registry_checks()
    list_checks(workroot)
    document_checks(workroot)
    view_checks(workroot)
    budget_source_checks(workroot)
    window_paging_checks(workroot)
    print("-" * 50)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine document tools selftest: ALL PASS")


if __name__ == "__main__":
    main()
