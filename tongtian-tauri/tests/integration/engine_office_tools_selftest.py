#!/usr/bin/env python3
"""办公产物工具自测（D-33.1：generate_xlsx / generate_docx 落真件）

锁住六件事：

1. **真件**：产出的是合法 OpenXML 包（PK 头 + zip 结构 + 关键 part 名），
   并用 openpyxl / python-docx 原样读回来逐格比对——扩展名与内容不一致就是 D-33 要堵的洞；
2. **注册面**：这两件只在 `office=True` 时出现，默认 `build_toolkit()` 仍是常驻六件
   （常驻清单要求 Rust harness 有实现，见 builtin-tools-contract.test.ts）；
3. **上限一律如实带话**：表数/行数/列数/单元格长度、块数、表格行列，被夹被截被跳过
   都在返回里留一句中文，「实际写进文件的」和「模型以为写进去的」不许悄悄分家；
4. **失败路径一个字节都不留**：错扩展名、越界、坏 JSON、空内容、依赖缺失都不产生文件，
   也不许留下「扩展名对、内容不是 Office」的假件；
5. **口径同源**：三张 Python 名单（沙箱写盘 / 检查点副作用 / 审批敏感）与渲染层
   `step-replay.ts` 的副作用表都含这两件，审批卡片还能认出「往哪儿写哪份文件」；
6. **面向模型的声明里的数字来自常量**：`__doc__` 里的上限文案按常量拼出来反查，
   改常量不改文案会当场红（f-string 当不了 docstring，只能这么核对）。

用法: python tests/integration/engine_office_tools_selftest.py
"""

import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

TEMP_HOME = tempfile.mkdtemp(prefix="tt-office-home-")
os.environ.setdefault("TONGTIANWORK_HOME", TEMP_HOME)

try:
    import server as engine_server
    import checkpoint_runtime
    import sandbox_runtime
    import tool_approval
    import docx  # noqa: F401 - 缺失时整套 SKIP，而不是报一堆假失败
    import openpyxl
    from pptx import Presentation  # noqa: F401
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: engine office tools selftest unavailable ({exc})")
    sys.exit(0)

RESIDENT_TOOLS = {
    "list_directory",
    "view_text_file",
    "read_document",
    "write_text_file",
    "insert_text_file",
    "delete_text_file",
}
OFFICE_TOOLS = {"generate_xlsx", "generate_docx"}

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


def note_of(text: str) -> str:
    """返回文案里的「说明：」段——上限/夹取/跳过都在这里，逐条断言比整串匹配稳。"""
    _head, _sep, tail = text.partition("\n说明：")
    return tail


def dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def fresh_workdir(prefix: str = "tt-office-work-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def package_parts(path: Path) -> list:
    """OpenXML 包的 part 清单；zip 坏了直接抛，让用例红而不是静默空。"""
    with zipfile.ZipFile(str(path)) as archive:
        broken = archive.testzip()
        if broken:
            raise ValueError(f"压缩包内 {broken} 校验失败")
        return archive.namelist()


# ── §1 注册面 ────────────────────────────────────────────────────────────────
def registration_checks() -> None:
    work = fresh_workdir()
    plain = engine_server.build_toolkit(work)
    check("默认注册表还是常驻六件（办公产物不混进内置清单）",
          set(plain.tools) == RESIDENT_TOOLS, str(sorted(plain.tools)))
    office = engine_server.build_toolkit(work, office=True)
    check("office=True 才多出两件",
          set(office.tools) == RESIDENT_TOOLS | OFFICE_TOOLS, str(sorted(office.tools)))
    slides = engine_server.build_toolkit(work, slides=True)
    check("幻灯片模式不带办公两件", OFFICE_TOOLS.isdisjoint(slides.tools), str(sorted(slides.tools)))
    both = engine_server.build_toolkit(work, slides=True, office=True)
    check("两种开关互不吞并对（写作+幻灯片同时可得）",
          {"generate_pptx"} <= set(both.tools) and OFFICE_TOOLS <= set(both.tools),
          str(sorted(both.tools)))
    for name in sorted(OFFICE_TOOLS):
        schema = both.tools[name].json_schema or {}
        props = (schema.get("function") or {}).get("parameters", {}).get("properties") or {}
        check(f"{name} 的 schema 有参数且描述非空",
              bool(props) and all(str(v.get("description") or "").strip() for v in props.values()),
              str(list(props)))
    check("generate_xlsx 的 schema 认 sheets_json",
          "sheets_json" in ((both.tools["generate_xlsx"].json_schema or {}).get("function") or {})
          .get("parameters", {}).get("properties", {}))
    check("generate_docx 的 schema 认 blocks_json",
          "blocks_json" in ((both.tools["generate_docx"].json_schema or {}).get("function") or {})
          .get("parameters", {}).get("properties", {}))
    src = (PROJECT_ROOT / "engine" / "server.py").read_text(encoding="utf-8")
    check("调用点按模式给 office 开关（通用与写作都放行）",
          'office=rc.normalize_mode(mode) in ("general", "writing")' in src)
    check("build_toolkit 的 office 缺省是关", "office: bool = False" in src)


# ── §2 xlsx 真件与读回 ───────────────────────────────────────────────────────
def xlsx_real_checks() -> None:
    work = fresh_workdir()
    toolkit = engine_server.build_toolkit(work, office=True)
    gen = toolkit.tools["generate_xlsx"].original_func
    read_doc = toolkit.tools["read_document"].original_func
    payload = [{
        "name": "销售",
        "header": ["月份", "金额", "备注"],
        "rows": [
            ["1月", 1200, "开年"],
            {"月份": "2月", "金额": 1500.5, "备注": "含退款"},
            ["3月", 0, None],
        ],
    }]
    out = tool_text(gen("销售表.xlsx", dumps(payload)))
    target = work / "销售表.xlsx"
    check("生成返回里报出路径与行数", "销售表.xlsx" in out and "1 张表" in out and "3 行数据" in out, out[:200])
    check("落盘文件存在且是 zip 包（PK 头）", target.exists() and target.read_bytes()[:2] == b"PK")
    parts = package_parts(target) if target.exists() else []
    check("包里有 [Content_Types].xml 与工作表 part",
          "[Content_Types].xml" in parts and any(p.startswith("xl/worksheets/") for p in parts),
          str(parts[:6]))
    wb = openpyxl.load_workbook(str(target))
    ws = wb["销售"]
    check("表名按给定的落地", ws.title == "销售", ws.title)
    check("表头加粗并冻结在 A2",
          ws["A1"].font.bold and ws["C1"].font.bold and ws.freeze_panes == "A2",
          str(ws.freeze_panes))
    check("表头文本正确", [ws.cell(row=1, column=c).value for c in (1, 2, 3)] == ["月份", "金额", "备注"])
    check("数据从第 2 行起逐行落位",
          [ws["A2"].value, ws["A3"].value, ws["A4"].value] == ["1月", "2月", "3月"],
          str([ws["A2"].value, ws["A3"].value, ws["A4"].value]))
    check("金额是数字不是字符串（能参与计算）",
          isinstance(ws["B2"].value, int) and ws["B2"].value == 1200
          and isinstance(ws["B3"].value, float) and ws["B3"].value == 1500.5,
          f"{ws['B2'].value!r}/{ws['B3'].value!r}")
    check("对象行按表头取值落成单元格", ws["C3"].value == "含退款", str(ws["C3"].value))
    check("0 与 None 分别为数字与空（不许变成字符串 0）",
          ws["B4"].value == 0 and ws["C4"].value in (None, ""), f"{ws['B4'].value!r}/{ws['C4'].value!r}")
    check("中文表名/表头/正文都原样读回",
          ws.title == "销售" and ws["C2"].value == "开年" and ws["C1"].value == "备注",
          f"{ws.title}/{ws['C1'].value}/{ws['C2'].value}")
    body = tool_text(read_doc("销售表.xlsx"))
    check("read_document 能读回这份 xlsx（扩展名与内容一致）",
          "## 工作表：销售" in body and "1月" in body and "含退款" in body, body[:200])
    second = tool_text(gen("子目录/嵌套.xlsx", [[["只有数组"], ["也认"]] ]))
    check("直接给二维数组也认（当一张无名表）",
          "已生成工作簿" in second and (work / "子目录" / "嵌套.xlsx").exists(), second[:160])
    if (work / "子目录" / "嵌套.xlsx").exists():
        rows = list(openpyxl.load_workbook(str(work / "子目录" / "嵌套.xlsx")).worksheets[0].iter_rows(values_only=True))
        check("无名表用「工作表1」并如实说明",
              "工作表1" in note_of(second) and rows[0][0] == "只有数组", str(rows))


# ── §3 xlsx 多表行号（跨表累计是真缺陷） ────────────────────────────────────
def xlsx_multi_sheet_checks() -> None:
    work = fresh_workdir()
    gen = engine_server.build_toolkit(work, office=True).tools["generate_xlsx"].original_func
    out = tool_text(gen("两表.xlsx", dumps([
        {"name": "甲", "header": ["x", "y"], "rows": [["a", 1], ["b", 2], ["c", 3]]},
        {"name": "乙", "header": ["x", "y"], "rows": [["d", 4], ["e", 5]]},
    ])))
    wb = openpyxl.load_workbook(str(work / "两表.xlsx"))
    ws_a, ws_b = wb["甲"], wb["乙"]
    check("两张表都落地且各自行数对", (ws_a.max_row, ws_b.max_row) == (4, 3),
          f"{ws_a.max_row}/{ws_b.max_row}")
    check("第二张表从第 2 行开始写（行号不跟着上一张累计）",
          ws_b["A2"].value == "d" and ws_b["A3"].value == "e",
          f"{ws_b['A2'].value!r}/{ws_b['A3'].value!r}")
    check("第一张表末行没有留空洞", ws_a["A4"].value == "c", str(ws_a["A4"].value))
    check("返回里的行数是全表合计", "5 行数据" in out, out[:160])


# ── §4 xlsx 上限 ─────────────────────────────────────────────────────────────
def xlsx_cap_checks() -> None:
    work = fresh_workdir()
    gen = engine_server.build_toolkit(work, office=True).tools["generate_xlsx"].original_func
    over_sheets = [{"name": f"S{i}", "rows": [[i]]} for i in range(engine_server.XLSX_MAX_SHEETS + 5)]
    note = note_of(tool_text(gen("超表.xlsx", dumps(over_sheets))))
    check("工作表超上限时只写前若干张并说明",
          f"超过上限 {engine_server.XLSX_MAX_SHEETS} 张" in note and "已忽略后 5 张" in note, note[:200])
    wb = openpyxl.load_workbook(str(work / "超表.xlsx"))
    check("落盘的工作表数等于上限", len(wb.worksheets) == engine_server.XLSX_MAX_SHEETS,
          str(len(wb.worksheets)))
    big = [[f"r{r}c{c}" for c in range(3)] for r in range(engine_server.XLSX_MAX_ROWS + 7)]
    note = note_of(tool_text(gen("超行.xlsx", [{"name": "T", "rows": big}])))
    check("数据超上限时说明剩多少行没进文件",
          f"只写前 {engine_server.XLSX_MAX_ROWS} 行" in note and "行没进文件" in note, note[:200])
    ws = openpyxl.load_workbook(str(work / "超行.xlsx"))["T"]
    check("落盘行数等于上限", ws.max_row == engine_server.XLSX_MAX_ROWS, str(ws.max_row))
    wide = [["c%d" % c for c in range(engine_server.XLSX_MAX_COLS + 30)]]
    note = note_of(tool_text(gen("超列.xlsx", [{"name": "W", "rows": wide}])))
    check("列超上限时逐行截断并计数",
          f"列数超过 {engine_server.XLSX_MAX_COLS}" in note, note[:200])
    ws = openpyxl.load_workbook(str(work / "超列.xlsx"))["W"]
    check("落盘列数等于上限", ws.max_column == engine_server.XLSX_MAX_COLS, str(ws.max_column))
    long_cell = "字" * (engine_server.OFFICE_MAX_CELL_CHARS + 500)
    note = note_of(tool_text(gen("超字.xlsx", [{"name": "L", "rows": [[long_cell, "短"]]}])))
    check("单元格超长时截断并说明处数",
          f"{engine_server.OFFICE_MAX_CELL_CHARS} 字" in note and "单元格超过" in note, note[:200])
    value = openpyxl.load_workbook(str(work / "超字.xlsx"))["L"]["A1"].value
    check("截断后长度正好是上限", len(value) == engine_server.OFFICE_MAX_CELL_CHARS, str(len(value)))
    nested = tool_text(gen("嵌套.xlsx", [{"name": "N", "rows": [[{"a": [1, 2]}, "ok"]]}]))
    check("嵌套结构转文本写入并如实说明", "已转成文本" in note_of(nested), nested[:200])


# ── §5 表名消毒 ──────────────────────────────────────────────────────────────
def xlsx_name_checks() -> None:
    work = fresh_workdir()
    gen = engine_server.build_toolkit(work, office=True).tools["generate_xlsx"].original_func
    out = tool_text(gen("表名.xlsx", dumps([
        {"name": "销售/2026:报表", "rows": [[1]]},
        {"name": "很长的名字" * 8, "rows": [[2]]},
        {"name": "重名", "rows": [[3]]},
        {"name": "重名", "rows": [[4]]},
    ])))
    names = [ws.title for ws in openpyxl.load_workbook(str(work / "表名.xlsx")).worksheets]
    note = note_of(out)
    check("非法字符换成连字符", "销售-2026-报表" in names, str(names))
    check("改名有说明", "含 Excel 不允许的字符" in note, note[:200])
    check("超长表名夹到上限",
          all(len(n) <= engine_server.OFFICE_MAX_SHEET_NAME_CHARS for n in names),
          str([len(n) for n in names]))
    check("重名自动加序号", names[2] == "重名" and names[3] != names[2], str(names))
    check("重名有说明", "重复，已改成" in note, note[:200])
    empty_note = tool_text(gen("缺名.xlsx", [{"rows": [[1]]}]))
    check("没给表名时用默认名并说明", "工作表1" in note_of(empty_note), empty_note[:200])


# ── §6 列宽 / 数字格式 / 公式 ────────────────────────────────────────────────
def xlsx_format_checks() -> None:
    work = fresh_workdir()
    gen = engine_server.build_toolkit(work, office=True).tools["generate_xlsx"].original_func
    out = tool_text(gen("格式.xlsx", dumps([{
        "name": "F",
        "header": ["项", "金额", "占比"],
        "rows": [["a", 1000, 0.25], ["b", 2000, 0.5], ["c", "不是数", 0.75]],
        "widths": [18, 14, 8],
        "formats": {"金额": "#,##0.00", "C": "0.0%"},
        "formulas": {"D2": "=SUM(B2:B4)", "D3": "=FOO(1)", "XX": "=SUM(1)", "D4": "SUM(1)"},
    }])))
    ws = openpyxl.load_workbook(str(work / "格式.xlsx"))["F"]
    note = note_of(out)
    check("列宽按顺序落到列维度",
          (ws.column_dimensions["A"].width, ws.column_dimensions["C"].width) == (18.0, 8.0),
          f"{ws.column_dimensions['A'].width}/{ws.column_dimensions['C'].width}")
    check("数字格式按表头名命中整列（B2 生效、B1 表头不受影响）",
          ws["B2"].number_format == "#,##0.00" and ws["B1"].number_format == "General",
          f"{ws['B2'].number_format}/{ws['B1'].number_format}")
    check("数字格式也能按列字母给", ws["C2"].number_format == "0.0%", ws["C2"].number_format)
    check("公式落成公式（不是字符串）",
          ws["D2"].value == "=SUM(B2:B4)" and ws["D2"].data_type == "f",
          f"{ws['D2'].value!r}/{ws['D2'].data_type}")
    check("返回里报出公式处数", "1 处公式" in out or "2 处公式" in out, out[:200])
    check("不认识的函数照样写入但如实说明", "FOO" in note, note[:260])
    check("非法单元格引用被忽略并点名", "引用不合法" in note and "XX" in note, note[:260])
    check("没以 = 开头的公式被忽略并点名", "没以 = 开头" in note, note[:260])
    check("公式写入行号不串到别处", ws["D3"].value == "=FOO(1)", str(ws["D3"].value))
    bad_key = tool_text(gen("坏键.xlsx", dumps([{
        "name": "K", "header": ["a", "b"], "rows": [["x", "y"]],
        "formats": {"第 3 列": "#,##0"}, "widths": ["宽"],
    }])))
    check("格式键既不是表头也不是列字母时忽略并说明",
          "既不是表头名也不是列字母" in note_of(bad_key), bad_key[:260])
    check("列宽不是数字时退回默认并说明", "列宽不是数字" in note_of(bad_key), bad_key[:260])
    no_effect = tool_text(gen("无数字.xlsx", dumps([
        {"name": "Z", "header": ["文本"], "rows": [["只有字"]], "formats": {"文本": "#,##0"}}
    ])))
    check("数字格式落在非数字列时说明不会有效果",
          "不是数字，格式不会有效果" in note_of(no_effect), no_effect[:260])


# ── §7 xlsx 失败路径不留文件 ─────────────────────────────────────────────────
def xlsx_failure_checks() -> None:
    work = fresh_workdir()
    gen = engine_server.build_toolkit(work, office=True).tools["generate_xlsx"].original_func
    for name, args, expect in (
        ("改名冒充", ("账.xls", [{"rows": [[1]]}]), ".xlsx 结尾"),
        ("扩展名是 csv", ("账.csv", [{"rows": [[1]]}]), ".xlsx 结尾"),
        ("坏 JSON", ("坏.xlsx", "{oops"), "不是合法 JSON"),
        ("空数组", ("空.xlsx", "[]"), "没有可用的工作表"),
        ("全是不明项", ("怪.xlsx", "[1, \"字符串\", true]"), "既不是对象也不是二维数组"),
    ):
        target = work / str(Path(args[0]).name)
        out = tool_text(gen(args[0], args[1] if isinstance(args[1], str) else dumps(args[1])))
        check(f"xlsx 失败路径：{name} 给出中文原因", expect in out, out[:200])
        check(f"xlsx 失败路径：{name} 不落盘", not target.exists(), str(target))
    outside = tool_text(gen("../逃出去.xlsx", dumps([{"rows": [[1]]}])))
    check("越界路径被拒绝", "路径超出工作目录范围" in outside, outside[:200])
    check("越界路径没有写出工作目录外的文件", not (work.parent / "逃出去.xlsx").exists())
    wrapper = tool_text(gen("包一层.xlsx", dumps({"sheets": [{"name": "S", "rows": [[1]]}]})))
    check('{"sheets": [...]} 写法也能收', "已生成工作簿" in wrapper, wrapper[:200])
    single = tool_text(gen("单对象.xlsx", dumps({"name": "只有一张", "rows": [[9]]})))
    check("单个对象按一张表处理（模型常忘外层方括号）",
          "已生成工作簿" in single and (work / "单对象.xlsx").exists(), single[:200])


# ── §8 docx 真件与读回 ───────────────────────────────────────────────────────
def docx_real_checks() -> None:
    work = fresh_workdir()
    toolkit = engine_server.build_toolkit(work, office=True)
    gen = toolkit.tools["generate_docx"].original_func
    read_doc = toolkit.tools["read_document"].original_func
    blocks = [
        {"type": "heading", "text": "一、结论", "level": 1},
        {"type": "h2", "text": "1.1 依据"},
        {"type": "paragraph", "text": "这是正文段落。"},
        "纯字符串按段落处理",
        {"type": "bullet", "text": "要点甲"},
        {"type": "ul", "text": "要点乙"},
        {"type": "numbered", "text": "第一步"},
        {"type": "ol", "text": "第二步"},
        {"type": "table", "header": ["字段", "说明"], "rows": [["编号", "A-1"], ["负责人", "张三"]]},
    ]
    out = tool_text(gen("报告.docx", dumps(blocks), title="年度总结",
                        header_text="机密", footer_text="第 1 页 / 共 1 页"))
    target = work / "报告.docx"
    check("docx 生成返回报出块数与结构",
          "已生成文档" in out and "9 块" in out and "表格 1 张" in out, out[:220])
    check("docx 是合法 zip 包", target.exists() and target.read_bytes()[:2] == b"PK")
    parts = package_parts(target) if target.exists() else []
    check("包里有 word/document.xml 与样式 part",
          "word/document.xml" in parts and any(p.startswith("word/styles") for p in parts),
          str(parts[:8]))
    document = docx.Document(str(target))
    pairs = [(p.style.name, p.text) for p in document.paragraphs]
    check("title 走 Title 样式", ("Title", "年度总结") in pairs, str(pairs[:3]))
    check("heading level 1/2 落成对应标题样式",
          ("Heading 1", "一、结论") in pairs and ("Heading 2", "1.1 依据") in pairs, str(pairs[:5]))
    check("段落与纯字符串都进正文",
          ("Normal", "这是正文段落。") in pairs and ("Normal", "纯字符串按段落处理") in pairs)
    check("无序列表用 List Bullet", sum(1 for s, _ in pairs if s == "List Bullet") == 2, str(pairs))
    check("有序列表用 List Number", sum(1 for s, _ in pairs if s == "List Number") == 2)
    check("表格内容逐格对上",
          [[c.text for c in row.cells] for row in document.tables[0].rows]
          == [["字段", "说明"], ["编号", "A-1"], ["负责人", "张三"]],
          str([[c.text for c in row.cells] for row in document.tables[0].rows]))
    check("表头单元格加粗",
          all(run.bold for cell in document.tables[0].rows[0].cells for run in cell.paragraphs[0].runs))
    section = document.sections[0]
    check("页眉页脚按入参落地",
          section.header.paragraphs[0].text == "机密"
          and section.footer.paragraphs[0].text == "第 1 页 / 共 1 页",
          f"{section.header.paragraphs[0].text}/{section.footer.paragraphs[0].text}")
    from docx.oxml.ns import qn

    normal = document.styles["Normal"]
    check("中文字体走 w:eastAsia（只设西名不管中文）",
          normal.element.rPr.rFonts.get(qn("w:eastAsia")) == engine_server.OFFICE_CJK_FONT
          and normal.font.name == engine_server.OFFICE_LATIN_FONT,
          f"{normal.font.name}/{normal.element.rPr.rFonts.get(qn('w:eastAsia'))}")
    check("标题样式也挂上中文字体",
          document.styles["Heading 1"].element.rPr.rFonts.get(qn("w:eastAsia"))
          == engine_server.OFFICE_CJK_FONT)
    body = tool_text(read_doc("报告.docx"))
    check("read_document 能读回这份 docx",
          "年度总结" in body and "要点甲" in body and "张三" in body, body[:220])
    only_title = tool_text(gen("只有标题.docx", "[]", title="就一个标题"))
    check("只有标题没有正文块也能生成（标题算一块内容）",
          "已生成文档" in only_title and (work / "只有标题.docx").exists(), only_title[:200])


# ── §9 docx 上限与跳过 ───────────────────────────────────────────────────────
def docx_cap_checks() -> None:
    work = fresh_workdir()
    gen = engine_server.build_toolkit(work, office=True).tools["generate_docx"].original_func
    many = [{"type": "paragraph", "text": f"第 {i} 段"} for i in range(engine_server.DOCX_MAX_BLOCKS + 20)]
    note = note_of(tool_text(gen("超长.docx", dumps(many))))
    check("块数超上限时说明剩多少没进文件",
          f"{engine_server.DOCX_MAX_BLOCKS} 块" in note and "没进文件" in note, note[:220])
    paragraphs = docx.Document(str(work / "超长.docx")).paragraphs
    check("落盘块数等于上限", len(paragraphs) == engine_server.DOCX_MAX_BLOCKS, str(len(paragraphs)))
    # 表格行列的越界用「把上限临时调小」来造，而不是真造一张 200×20 的表：
    # python-docx 的 table.cell() 每次都要走一遍行表，4000 格要跑一分多钟，
    # 而这条用例钉的是「超了会夹 + 会说明」这件事，跟上限具体是多少无关。
    # 真实上限的值由 §13 拿常量反查 __doc__，两半合起来才完整。
    saved_caps = (engine_server.DOCX_MAX_TABLE_ROWS, engine_server.DOCX_MAX_TABLE_COLS)
    engine_server.DOCX_MAX_TABLE_ROWS, engine_server.DOCX_MAX_TABLE_COLS = 6, 4
    try:
        fat_table = {
            "type": "table",
            "header": [f"列{c}" for c in range(10)],
            "rows": [[f"r{r}c{c}" for c in range(10)] for r in range(9)],
        }
        note = note_of(tool_text(gen("超表.docx", dumps([fat_table]))))
        check("表格行列超上限时分别说明",
              f"超过 {engine_server.DOCX_MAX_TABLE_COLS} 列" in note
              and f"超过 {engine_server.DOCX_MAX_TABLE_ROWS} 行" in note, note[:260])
        table = docx.Document(str(work / "超表.docx")).tables[0]
        check("落盘表格行列等于上限（表头占一行）",
              (len(table.columns), len(table.rows))
              == (engine_server.DOCX_MAX_TABLE_COLS, engine_server.DOCX_MAX_TABLE_ROWS + 1),
              f"{len(table.columns)}/{len(table.rows)}")
        check("截断后的表格内容仍是原样前缀",
              table.cell(1, 0).text == "r0c0" and table.cell(0, 3).text == "列3",
              f"{table.cell(1, 0).text}/{table.cell(0, 3).text}")
    finally:
        engine_server.DOCX_MAX_TABLE_ROWS, engine_server.DOCX_MAX_TABLE_COLS = saved_caps
    weird = tool_text(gen("怪块.docx", dumps([
        {"type": "image", "text": "贴图"},
        {"type": "paragraph", "text": "正常段落"},
        {"type": "heading", "text": "   "},
        123,
    ])))
    note = note_of(weird)
    check("未知类型被跳过并点名可用类型",
          "不认识" in note and "heading / paragraph / bullet / numbered / table" in note, note[:260])
    check("未知类型/空正文/不是对象的项一起计数", "3 块被跳过" in note, note[:260])
    check("剩下的正常块仍然落盘",
          [p.text for p in docx.Document(str(work / "怪块.docx")).paragraphs] == ["正常段落"])
    level = tool_text(gen("层级.docx", dumps([
        {"type": "heading", "text": "零级", "level": 0},
        {"type": "heading", "text": "九级", "level": 9},
        {"type": "heading", "text": "文字层级", "level": "两"},
    ])))
    styles = [p.style.name for p in docx.Document(str(work / "层级.docx")).paragraphs]
    check("标题层级夹在 1–4 且非数字回落 1",
          styles == ["Heading 1", "Heading 4", "Heading 1"], f"{styles}/{level[:120]}")


# ── §10 docx 失败路径 ────────────────────────────────────────────────────────
def docx_failure_checks() -> None:
    work = fresh_workdir()
    gen = engine_server.build_toolkit(work, office=True).tools["generate_docx"].original_func
    out = tool_text(gen("文.doc", dumps([{"type": "paragraph", "text": "x"}])))
    check("docx 拒收 .doc（旧二进制格式不是我们能写的东西）", ".docx 结尾" in out, out[:200])
    check("docx 拒收时不落盘", not (work / "文.doc").exists())
    out = tool_text(gen("坏.docx", "{oops"))
    check("坏 JSON 给出中文原因", "不是合法 JSON" in out, out[:200])
    check("坏 JSON 不落盘", not (work / "坏.docx").exists())
    out = tool_text(gen("空.docx", "[]"))
    check("空内容 + 无标题时报错而非交一份白文档", "没有可用的内容块" in out, out[:200])
    check("空内容不落盘", not (work / "空.docx").exists())
    out = tool_text(gen("../../逃.docx", dumps([{"type": "paragraph", "text": "x"}])))
    check("docx 越界被拒绝且不写出工作目录",
          "路径超出工作目录范围" in out and not (work.parent / "逃.docx").exists(), out[:200])
    out = tool_text(gen("包装.docx", dumps({"blocks": [{"type": "paragraph", "text": "x"}]})))
    check('{"blocks": [...]} 写法也能收', "已生成文档" in out, out[:200])


# ── §11 依赖缺失：三件都回中文，不抛异常、不留假件 ──────────────────────────
def missing_dependency_checks() -> None:
    work = fresh_workdir()
    toolkit = engine_server.build_toolkit(work, slides=True, office=True)
    blocked = {"openpyxl", "docx", "pptx"}
    saved = {name: sys.modules.get(name) for name in blocked}
    try:
        for name in blocked:
            sys.modules[name] = None  # import 直接 ImportError（CPython 的既定行为）
        for tool_name, args, label in (
            ("generate_xlsx", ("缺依赖.xlsx", "[{\"rows\":[[1]]}]"), "未安装 openpyxl"),
            ("generate_docx", ("缺依赖.docx", "[{\"type\":\"paragraph\",\"text\":\"x\"}]"), "未安装 python-docx"),
            ("generate_pptx", ("缺依赖.pptx", "演示", "[{\"title\":\"x\"}]"), "未安装 python-pptx"),
        ):
            fn = toolkit.tools[tool_name].original_func
            try:
                out = tool_text(fn(*args))
                raised = ""
            except Exception as exc:  # noqa: BLE001
                out, raised = "", f"{type(exc).__name__}: {exc}"
            check(f"{tool_name} 缺依赖时回中文说明而不是抛异常",
                  label in out and not raised, out[:180] or raised)
            check(f"{tool_name} 缺依赖时不落任何文件",
                  not (work / args[0]).exists(), str(args[0]))
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
    # 复原后必须还能真写出来：上面那手如果漏了复原，这里会红
    out = tool_text(toolkit.tools["generate_xlsx"].original_func("复原.xlsx", "[{\"rows\":[[1]]}]"))
    check("复原依赖后同一进程仍能正常生成", "已生成工作簿" in out, out[:200])


# ── §12 名单同源与审批摘要 ───────────────────────────────────────────────────
def registry_sync_checks() -> None:
    for name in sorted(OFFICE_TOOLS):
        check(f"{name} 在沙箱写盘名单里（L0 只读据此拒绝）",
              name in sandbox_runtime.MUTATING_TOOLS)
        check(f"{name} 在检查点副作用表里", checkpoint_runtime.is_side_effect_tool(name) is True)
        check(f"{name} 在审批敏感工具里", name in tool_approval.SENSITIVE_TOOLS)
        # 运行时真正生效的是这条：非只读即需审批（SENSITIVE_TOOLS 目前只是本表自述，
        # 没有任何消费方——把两件补进表里是「按表的口径补齐」，不是新增了一道闸）
        check(f"{name} 在敏感档位下确实需要审批",
              tool_approval.needs_approval(name, "sensitive") is True)
        check(f"{name} 在只读档位下不需要审批但会被沙箱拒绝",
              tool_approval.needs_approval("view_text_file", "sensitive") is False)
    summary = tool_approval.summarize_input(
        "generate_xlsx", {"file_path": "out/表.xlsx", "sheets_json": "[{\"rows\":[[1]]}"}
    )
    check("审批摘要认出工作簿与目标路径", "工作簿：" in summary and "表.xlsx" in summary, summary)
    check("审批摘要只报内容体积不糊正文", f"（内容 {len('[{\"rows\":[[1]]}')} 字）" in summary, summary)
    summary_doc = tool_approval.summarize_input(
        "generate_docx", {"file_path": "报告.docx", "blocks_json": "[]", "title": "年度总结"}
    )
    check("审批摘要带得上文档标题",
          "文档：" in summary_doc and "年度总结" in summary_doc, summary_doc)
    check("审批摘要没标题时不挂空尾巴",
          not tool_approval.summarize_input("generate_docx", {"file_path": "a.docx"}).endswith("标题："),
          tool_approval.summarize_input("generate_docx", {"file_path": "a.docx"}))
    ts = (PROJECT_ROOT / "src" / "shared" / "step-replay.ts").read_text(encoding="utf-8")
    block = ts.split("SIDE_EFFECT_TOOLS = [")[1].split("] as const")[0]
    for name in sorted(OFFICE_TOOLS):
        check(f"{name} 也在渲染层副作用表里（恢复时不重放）", f"'{name}'," in block, block[:200])
    canvas = (PROJECT_ROOT / "src" / "renderer" / "lib" / "mode-canvas.ts").read_text(encoding="utf-8")
    writing = canvas.split("writing: [")[1].split("]")[0]
    check("写作模式的画布认 .docx / .xlsx 为本类产物",
          "'docx'" in writing and "'xlsx'" in writing, writing)
    check("幻灯片模式没被顺手改坏", "slides: ['pptx', 'html', 'htm']" in canvas)


# ── §13 面向模型的声明与常量同源 ────────────────────────────────────────────
def docstring_source_checks() -> None:
    toolkit = engine_server.build_toolkit(fresh_workdir(), office=True)
    docs = {
        name: str(toolkit.tools[name].original_func.__doc__ or "")
        for name in sorted(OFFICE_TOOLS)
    }
    xlsx_expected = (
        f"{engine_server.XLSX_MAX_SHEETS} 张表",
        f"{engine_server.XLSX_MAX_ROWS} 行",
        f"{engine_server.XLSX_MAX_COLS} 列",
        f"{engine_server.OFFICE_MAX_CELL_CHARS} 字",
    )
    for phrase in xlsx_expected:
        check(f"generate_xlsx 声明里的上限与常量同源：{phrase}",
              phrase in docs["generate_xlsx"], docs["generate_xlsx"][:200])
    docx_expected = (
        f"{engine_server.DOCX_MAX_BLOCKS} 块",
        f"{engine_server.DOCX_MAX_TABLE_ROWS} 行",
        f"{engine_server.DOCX_MAX_TABLE_COLS} 列",
        f"{engine_server.OFFICE_MAX_CELL_CHARS} 字",
    )
    for phrase in docx_expected:
        check(f"generate_docx 声明里的上限与常量同源：{phrase}",
              phrase in docs["generate_docx"], docs["generate_docx"][:200])
    check("声明里写清「数值由 Excel/WPS 打开时计算」",
          "数值由 Excel/WPS 打开时计算" in docs["generate_xlsx"])
    check("返回文案里也带同一句口径（不只写在声明里）",
          "公式数值由 Excel/WPS 打开时计算" in tool_text(
              engine_server.build_toolkit(fresh_workdir(), office=True)
              .tools["generate_xlsx"].original_func("口径.xlsx", "[{\"rows\":[[1]]}]")
          ))
    check("声明里不出现本工程没有的工具名",
          not re.search(r"画布|Slides 工具|canvas", docs["generate_docx"] + docs["generate_xlsx"]))
    check("上限常量彼此自洽（表名 31 字、列号 3 字母装得下）",
          engine_server.OFFICE_MAX_SHEET_NAME_CHARS == 31
          and engine_server.XLSX_MAX_COLS < 26 ** 3)
    # 工具只在注册的那几种模式里由 schema 可见；把它们写进通用 SYS_PROMPT，
    # 就等于在 design/slides 模式下告诉模型「你有这件工具」却调不到（grants 那条口径同理）
    check("通用提示词不点名办公工具",
          "generate_xlsx" not in engine_server.SYS_PROMPT
          and "generate_docx" not in engine_server.SYS_PROMPT)
    check("模式提示词也没被顺手改（幻灯片仍只点名 generate_pptx）",
          "generate_pptx" in engine_server.rc.compose_mode("slides")
          and "generate_xlsx" not in engine_server.rc.compose_mode("slides")
          and "generate_docx" not in engine_server.rc.compose_mode("writing"))


def main() -> None:
    print(f"engine office tools selftest (work root: {TEMP_HOME})")
    registration_checks()
    xlsx_real_checks()
    xlsx_multi_sheet_checks()
    xlsx_cap_checks()
    xlsx_name_checks()
    xlsx_format_checks()
    xlsx_failure_checks()
    docx_real_checks()
    docx_cap_checks()
    docx_failure_checks()
    missing_dependency_checks()
    registry_sync_checks()
    docstring_source_checks()
    print("-" * 50)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine office tools selftest: ALL PASS")


if __name__ == "__main__":
    main()
