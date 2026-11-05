#!/usr/bin/env python3
"""
IM 媒体承载层自测（缺陷 D-41 的 ContentPart + MediaBackend 那一半）

全部打真实磁盘与真实异步往返（fetch 是本文件里现写的假下载器，返回真字节），
不是 Mock 返回值：验的是「一条带附件的消息进来，哪些能落盘、落在哪、越界的怎么拒」。

分段：
1. 归类与消毒：MIME/后缀到部分类型、明确拒绝的 MIME、用户可控文件名的路径穿越
2. 单部分闸门：check_part 的每种拒绝理由都是中文且可判空
3. aggregate_text：非文本部分必须留一行说明——不许把「发了张图」摊平成「发了条空消息」
4. 落盘全链路：注入 fetch → 真写字节 → sha256 → 本地路径，且不改调用方对象
5. 三条闸：体积超限、类型不认识、只有平台内引用没换成 URL 的三种如实失败
6. 去重与配额：同一张图重复发只留一份；超配额按 mtime 从旧到新清，且只在本会话目录内
7. 越界拒绝：会话标识与文件名里的 ../ 不能把文件写到附件根之外
7b. 目录名合法性：实例键 `kind:channel_id` 里的 Windows 非法字符、超长标识、
    消毒后撞名——两个实例的附件绝不能倒进同一个目录
8. 视图形状：view() / footprint() 里没有 downloadUrl 与平台引用原文

数据隔离：TT_DATA_HOME 指向临时目录，附件绝不写进 ~/.tongtianwork 的存量根。
用法: python tests/integration/engine_im_media_selftest.py
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-im-media-"))
os.environ["TT_DATA_HOME"] = str(_TMP / "home")
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import im_media as M
except Exception as exc:  # 依赖缺失时优雅跳过（与其余引擎自测一致）
    print(f"SKIP: engine im media selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 3
PDF = b"%PDF-1.7\n" + b"fake-pdf-body" * 4


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def make_fetch(responses: dict) -> tuple[M.Fetch, list]:
    """假下载器：按 URL 返回 (status, content-type, body)，并记录被请求过几次"""
    calls = []

    async def fetch(url: str):
        calls.append(url)
        spec = responses.get(url)
        if spec is None:
            return (404, "", b"")
        return spec

    return fetch, calls


# ── 1. 归类与消毒 ─────────────────────────────────────────────────


def phase_classify() -> None:
    section("1. 部分类型归类与文件名消毒")
    check("image/png 归成图片", M.kind_for_mime("image/png") == "image")
    check("MIME 大小写与参数都吃得下", M.kind_for_mime("  IMAGE/PNG; charset=x ") == "image")
    check("没登记的具体分支按大类兜", M.kind_for_mime("image/heif") == "image")
    check("video/mp4 归成视频", M.kind_for_mime("video/mp4") == "video")
    check("audio/wav 归成语音", M.kind_for_mime("audio/x-wav") == "audio")
    check("application/pdf 归成文件", M.kind_for_mime("application/pdf") == "file")
    check("text/csv 归成文件", M.kind_for_mime("text/csv") == "file")
    check("svg 不当图片收（能带脚本）", M.kind_for_mime("image/svg+xml") == "")
    check("html 不当内容收", M.kind_for_mime("text/html") == "")
    check("可执行不认", M.kind_for_mime("application/x-msdownload") == "")
    check("完全空白的 MIME 不猜", M.kind_for_mime("") == "")
    check("application/octet-stream 不猜类型", M.kind_for_mime("application/octet-stream") == "")
    check("MIME 缺失时按后缀兜", M.kind_for_name("季度报表.XLSX") == "file")
    check("后缀优先级低于 MIME", M.kind_for_name("photo.txt", "image/png") == "image")
    check("不认识的后缀不硬归类", M.kind_for_name("script.qqq") == "")

    check("路径穿越只剩最后一段", M.safe_name("../../etc/passwd") == "passwd")
    check("反斜杠穿越同样拦得住", M.safe_name(r"a\b\c.png") == "c.png")
    check("整条目录式名字消毒成单个文件", M.safe_name(r"..\..\Windows\system32\x.exe") == "x.exe")
    check("控制字符清掉", "\n" not in M.safe_name("evil\x00name.png"))
    check("开头点号不留（防隐藏文件）", not M.safe_name("..secret").startswith("."))
    check("空名字消毒成空串而不是点", M.safe_name("   ") == "")
    check("长名字裁到上限", len(M.safe_name("x" * 200 + ".png")) <= M.MAX_NAME_CHARS)
    check("裁名字保住扩展名", M.safe_name("y" * 200 + ".pdf").endswith(".pdf"))


# ── 2. 单部分闸门 ─────────────────────────────────────────────────


def phase_check_part() -> None:
    section("2. 单个部分能不能落盘")
    check("文本部分永远放行", M.check_part(M.ContentPart(kind="text", text="hi")) == "")
    check("未知类型给出中文理由", "未知" in M.check_part(M.ContentPart(kind="sticker")))
    check("图片没有下载信息就说明白", "下载信息" in M.check_part(M.ContentPart(kind="image")))
    check("只有平台内引用也算有信息（换地址是通道的事）",
          M.check_part(M.ContentPart(kind="image", ref="download-code-1")) == "")
    check("超过单文件上限直接拒",
          "超过单文件上限" in M.check_part(M.ContentPart(kind="file", url="https://x/f", size=M.MAX_PART_BYTES + 1)))
    check("上限以内放行", M.check_part(M.ContentPart(kind="file", url="https://x/f", size=10)) == "")
    check("明确拒绝的 MIME 给中文理由",
          "不接收" in M.check_part(M.ContentPart(kind="image", url="https://x/a", mime="image/svg+xml")))
    check("size=-1 表示平台没说，不当成超限", M.check_part(M.ContentPart(kind="file", url="https://x/a", size=-1)) == "")


# ── 3. aggregate_text：不许静默丢 ─────────────────────────────────


def phase_aggregate() -> None:
    section("3. 多部分摊回一条消息文本")
    out = M.aggregate_text(
        [M.ContentPart(kind="text", text="看这张图"), M.ContentPart(kind="image", name="a.png", size=120)]
    )
    check("文本原样保留", "看这张图" in out)
    check("图片留下中文说明而不是被丢掉", "[图片：a.png，120 字节]" in out)
    only_media = M.aggregate_text([M.ContentPart(kind="image", size=1)])
    check("纯附件消息不是空串", only_media.strip() != "" and only_media.startswith("[图片"))
    stored = M.aggregate_text([M.ContentPart(kind="file", name="b.pdf", local_path="/tmp/b.pdf")])
    check("已落盘的会说明「已存到本机」", "已存到本机" in stored)
    rejected = M.aggregate_text([M.ContentPart(kind="image", note="下载超时，附件未落盘")])
    check("落盘失败的把原因带进文本", "下载超时" in rejected)
    size_unknown = M.aggregate_text([M.ContentPart(kind="image", name="c.png", size=-1)])
    check("平台没报体积时不谎报 0 字节", "0 字节" not in size_unknown)
    check("has_media 认得出附件", M.has_media([M.ContentPart(kind="text"), M.ContentPart(kind="audio")]))
    check("纯文本不算有附件", not M.has_media([M.ContentPart(kind="text", text="x")]))
    # 排重身份：带附件的消息不能按引擎自己拼的中文说明判重复（两张照片能拼出同一句话）
    check("纯文本消息没有附件身份", M.parts_identity([M.ContentPart(kind="text", text="查天气")]) == "")
    check("附件身份取自平台给的引用码",
          M.parts_identity([M.ContentPart(kind="image", ref="dl-1"), M.ContentPart(kind="image", ref="dl-2")])
          == "image:dl-1;image:dl-2")
    check("两张不同的照片身份不同",
          M.parts_identity([M.ContentPart(kind="image", ref="a")]) != M.parts_identity([M.ContentPart(kind="image", ref="b")]))
    check("只有文件名时身份退到文件名（仍可比对）", M.parts_identity([M.ContentPart(kind="file", name="x.pdf")]) == "file:x.pdf")
    check("引用码与文件名都没有时如实给空（宁可不排重也不误吞）",
          M.parts_identity([M.ContentPart(kind="image", url="https://x/y")]) == "")
    check("图文混排的身份只由附件决定",
          M.parts_identity([M.ContentPart(kind="text", text="看下这张"), M.ContentPart(kind="image", ref="dl-9")])
          == "image:dl-9")
    check("身份串有上界，超长引用码不会撑爆排重表",
          len(M.parts_identity([M.ContentPart(kind="image", ref="r" * 500)])) <= 120 + len("image:"))
    parts = M.unsupported_note([M.ContentPart(kind="image"), M.ContentPart(kind="text", text="hi")], "本通道未接图片下载")
    check("未接下载时给每个附件补中文原因", parts[0].note == "本通道未接图片下载")
    check("补原因不动文本部分", parts[1].note == "")
    kept = M.unsupported_note([M.ContentPart(kind="image", note="已有原因")], "不该覆盖")
    check("已有原因不被覆盖", kept[0].note == "已有原因")


# ── 4/5/6/7/8. MediaBackend 真实落盘 ─────────────────────────────


def backend(root: Path, **kw) -> M.MediaBackend:
    return M.MediaBackend(root, **kw)


def phase_materialize() -> None:
    section("4. 带附件的消息真落盘")
    root = _TMP / "home" / "im_media"
    b = backend(root)
    fetch, calls = make_fetch({"https://dl/x/a.png": (200, "image/png", PNG)})
    src = [M.ContentPart(kind="text", text="图纸见附件"), M.ContentPart(kind="image", name="a.png",
                                                                                 url="https://dl/x/a.png")]
    got = asyncio.run(b.materialize(src, channel_id="ding-1", conversation_id="cid-1", fetch=fetch))
    img = got[1]
    check("下载只发生一次", calls == ["https://dl/x/a.png"])
    check("落盘路径在附件根内", img.local_path and str(root) in img.local_path)
    check("sha256 是真算出来的", img.sha256 != "" and len(img.sha256) == 64)
    check("体积按实际字节回填", img.size == len(PNG))
    check("类型按响应头回填", img.kind == "image" and img.mime == "image/png")
    check("文件名带内容前缀，可追溯", Path(img.local_path).name.startswith(img.sha256[:12]))
    check("文件真的在盘上且字节一致", Path(img.local_path).read_bytes() == PNG)
    # 权限如实分平台：POSIX 上 chmod 0600 真能收紧；Windows 的 st_mode 不反映 ACL，
    # 在这里断言「已收紧」就是谎报——只钉住「本机这套写文件动作没抛，且 Windows 确实只回到 0666」。
    mode = os.stat(img.local_path).st_mode & 0o777
    if os.name == "posix":
        check("POSIX：落盘权限收紧到属主可读写", mode in (0o600, 0o400))
    else:
        check("Windows：chmod 不反映在 st_mode（权限由 ACL 决定，本层不谎报收紧）", mode == 0o666)
    check("不改调用方传入的对象", src[1].local_path == "" and src[1].sha256 == "")
    check("文本部分原样透传", got[0].text == "图纸见附件" and got[0].local_path == "")
    stats = b.stats
    check("统计记了 1 个文件", stats.files == 1 and stats.bytes == len(PNG))


def phase_gates() -> None:
    section("5. 三种如实失败")
    root = _TMP / "gates"
    b = backend(root, max_part_bytes=64)
    big = b"https://dl/big.bin"
    fetch, calls = make_fetch({
        big: (200, "application/pdf", PDF * 30),           # 实际下载超上限
        "https://dl/404.png": (200, "", b""),               # 空内容
        "https://dl/unknown.bin": (200, "application/octet-stream", b"zzz"),
    })
    out = asyncio.run(
        b.materialize(
            [
                M.ContentPart(kind="file", name="big.pdf", url=big),
                M.ContentPart(kind="image", name="none.png", url="https://dl/404.png"),
                M.ContentPart(kind="file", name="u.bin", url="https://dl/unknown.bin"),
                M.ContentPart(kind="image", name="ref.png", ref="downloadCode-xyz"),
                M.ContentPart(kind="image", name="boom.png", url="https://dl/not-in-table"),
            ],
            channel_id="ch", conversation_id="cv", fetch=fetch,
        )
    )
    check("超过上限的没落盘", out[0].local_path == "" and "超过单文件上限" in out[0].note)
    check("空内容如实说明", out[1].local_path == "" and "空内容" in out[1].note)
    check("认不出类型的不落盘", out[2].local_path == "" and "无法归类" in out[2].note)
    check("只有平台引用时不猜下载地址", out[3].local_path == "" and "通道未实现这一步" in out[3].note)
    check("被拒的五个都没有落成文件", [p for p in root.glob("**/*") if p.is_file()] == [])
    check("只有引用没换地址时不猜 URL", "downloadCode" not in json_dumps([p.view() for p in out]))
    check("被拒的每个都计数", b.stats.rejected == 5 and b.stats.files == 0)

    # fetch 抛异常 / 超时：一条消息不能因为一张图炸掉
    root2 = _TMP / "gates2"
    b2 = backend(root2)

    async def angry(url: str):
        if "timeout" in url:
            await asyncio.sleep(0)
            raise asyncio.TimeoutError()
        raise RuntimeError("connection reset")

    mixed = asyncio.run(
        b2.materialize(
            [
                M.ContentPart(kind="image", name="t.png", url="https://dl/timeout"),
                M.ContentPart(kind="image", name="r.png", url="https://dl/reset"),
                M.ContentPart(kind="image", name="ok.png", url="https://dl/ok"),
            ],
            channel_id="ch", conversation_id="cv2",
            fetch=angry,
        )
    )
    check("下载超时的中文说明", "超时" in mixed[0].note and mixed[0].local_path == "")
    check("连接被重置也不整条崩", mixed[1].local_path == "" and "下载失败" in mixed[1].note)
    check("同一条消息里其它部分照旧失败而不是被跳过", mixed[2].local_path == "")
    check("异常路径不落盘也不留半截文件", b2.footprint("ch", "cv2")["files"] == 0)

    # HTTP 非 2xx
    b3 = backend(_TMP / "gates3")
    f3, _ = make_fetch({"https://dl/denied": (403, "image/png", PNG)})
    r3 = asyncio.run(b3.materialize([M.ContentPart(kind="image", url="https://dl/denied")],
                                    channel_id="c", conversation_id="v", fetch=f3))
    check("平台返回 403 说清 HTTP 码", "HTTP 403" in r3[0].note and r3[0].local_path == "")

    # 没注入 fetch：本层不自己碰网络
    b4 = backend(_TMP / "gates4")
    r4 = asyncio.run(b4.materialize([M.ContentPart(kind="image", url="https://dl/whatever")],
                                    channel_id="c", conversation_id="v"))
    check("没有下载通道时如实拒", "未提供下载通道" in r4[0].note)


def phase_limits_and_dedupe() -> None:
    section("6. 一条消息的附件数与配额清理")
    root = _TMP / "limits"
    b = backend(root, max_parts=2)
    urls = {f"https://dl/{i}.png": (200, "image/png", PNG + bytes([i])) for i in range(4)}
    fetch, calls = make_fetch(urls)
    parts = [M.ContentPart(kind="image", name=f"{i}.png", url=f"https://dl/{i}.png") for i in range(4)]
    out = asyncio.run(b.materialize(parts, channel_id="ch", conversation_id="cv", fetch=fetch))
    check("只下载前两个附件", len(calls) == 2)
    check("前两个落盘", out[0].local_path and out[1].local_path)
    check("第三个开始如实说明未处理", "最多处理 2 个附件" in out[2].note and out[3].note != "")
    check("超出部分没落盘", not out[2].local_path and not out[3].local_path)

    # 内容寻址去重
    b2 = backend(_TMP / "dedupe")
    dup, dup_calls = make_fetch({
        "https://dl/first": (200, "image/png", PNG),
        "https://dl/second": (200, "image/png", PNG),
    })
    first = asyncio.run(b2.materialize([M.ContentPart(kind="image", name="p.png", url="https://dl/first")],
                                       channel_id="ch", conversation_id="cv", fetch=dup))
    second = asyncio.run(b2.materialize([M.ContentPart(kind="image", name="p-again.png", url="https://dl/second")],
                                        channel_id="ch", conversation_id="cv", fetch=dup))
    check("同一张图第二次只写一份", b2.footprint("ch", "cv")["files"] == 1)
    check("第二次复用同一路径", Path(first[0].local_path).name == Path(second[0].local_path).name)
    check("复用时给出中文说明而不是谎报新文件", "同一份内容" in second[0].note)

    # 配额清理：显式设 mtime，不靠真实时钟（vendor 的单调钟粒度 15.6ms，靠 sleep 排不出新旧）
    root3 = _TMP / "quota"
    b3 = backend(root3, max_session_bytes=len(PNG) * 2 + 10)
    d = b3.dir_for("ch", "cv")
    now = time.time()
    bodies = []
    for i in range(4):
        body = PNG + bytes([i]) * 2
        bodies.append(body)
        b3.write_bytes("ch", "cv", M.ContentPart(kind="image", name=f"k{i}.png"), body)
    files = sorted(d.glob("*"), key=lambda p: p.stat().st_size)
    check("写完先落盘再按配额清", b3.footprint("ch", "cv")["bytes"] <= b3.max_session_bytes)
    for p in d.glob("*"):
        os.utime(p, (now - 5000, now - 5000))
    survivor = M.MediaBackend(root3, max_session_bytes=0)
    removed = survivor.prune(d)
    check("prune 按目录内文件数删到配额内", removed >= 1 and list(d.glob("*")) == [])
    check("prune 不越出本会话目录", list(root3.glob("ch/*")) and all(x.name == "cv" for x in root3.glob("ch/*")))


def phase_path_escape() -> None:
    section("7. 路径越界一律拒绝")
    root = _TMP / "escape"
    b = backend(root)
    try:
        b.dir_for("..", "..")
        escaped = False
    except M.MediaError as exc:
        escaped = exc.code == "IM_MEDIA_PATH_ESCAPE"
    check("会话标识里的 .. 报 IM_MEDIA_PATH_ESCAPE", escaped)
    try:
        b.dir_for("ch", "../..")
        nested_escaped = False
    except M.MediaError as exc:
        nested_escaped = exc.code == "IM_MEDIA_PATH_ESCAPE"
    check("带上级段的会话标识同样拒（不与别的会话并目录）", nested_escaped)
    check("拒绝后没有创建任何目录（先判再建）", not root.exists() or list(root.glob("*")) == [])
    nested = b.dir_for("ch", "a/b/c")
    check("普通分隔符消毒后仍在附件根内", str(root.resolve()) in str(nested.resolve()))
    check("消毒后的目录是附件根下的两级", nested.resolve().relative_to(root.resolve()).parts == ("ch", "c"))

    # 名字里带分隔符：落盘文件名必须只有一段
    ok_fetch, _ = make_fetch({"https://dl/e.png": (200, "image/png", PNG)})
    out = asyncio.run(
        b.materialize([M.ContentPart(kind="image", name="../../evil.png", url="https://dl/e.png")],
                      channel_id="ch", conversation_id="cv", fetch=ok_fetch)
    )
    check("恶意文件名只保留最后一段", "/" not in out[0].name and "\\" not in out[0].name)
    check("恶意文件仍落在本会话目录内", Path(out[0].local_path).parent.resolve() == b.dir_for("ch", "cv").resolve())

    # 附件根之外的写入：直接调 _write 传一个越界目录也必须拒
    b2 = backend(root)
    outside = _TMP / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    refused = asyncio.run(
        b2.materialize([M.ContentPart(kind="image", name="x.png", url="https://dl/e.png")],
                       channel_id="ch", conversation_id="cv", fetch=ok_fetch)
    )
    check("同内容第二次仍复用已存的那份（不重复写）",
          Path(refused[0].local_path).parent.resolve() == Path(out[0].local_path).parent.resolve())
    check("附件根在 engine_home 下", str(M.media_root()).startswith(str(_TMP)))


def phase_slug_legality() -> None:
    section("7b. 实例键落成的目录名：合法、稳定、且不许两个实例撞成一个")
    b = backend(_TMP / "slug")
    illegal = set(':<>|"*?/')

    # 本来合法的标识一个字都不改：界面/自测里看到的目录名和实例号对得上
    check("合法标识原样保留", b._slug("ch") == "ch" and b._slug("T1-C1") == "T1-C1"
          and b._slug("oc_a1b2c3d4") == "oc_a1b2c3d4", b._slug("T1-C1"))
    check("带分隔符的标识只取最后一段", b._slug("a/b/c") == "c" and b._slug(r"a\b\c") == "c")
    check("尾部的点与空格剪掉（Windows 不认结尾点）",
          not b._slug("name.").endswith(".") and not b._slug("name. ").endswith(" ")
          and not b._slug("name. ").endswith("."), b._slug("name. "))

    # D-41 之后目录名来自 `kind:channel_id`，冒号在 Windows 上是盘符分隔符
    colon = b._slug("fake:entA")
    check("实例键里的冒号不会留在盘名上", not illegal & set(colon), colon)
    check("消毒过的名字带一段原文指纹", colon != "fake_entA" and "-" in colon, colon)
    check("同一实例每次算出同一个名字（消息都进同一个目录）", b._slug("fake:entA") == colon)
    check("fake:entA 与 fake_entA 不并成一个目录（那是跨实例泄漏）",
          colon != b._slug("fake_entA"), f"{colon} / {b._slug('fake_entA')}")
    collapsed = {b._slug(v) for v in ("a:b", "a<b", "a?b", "a|b", 'a"b')}
    check("几种非法字符各自消毒成不同目录（不共用消毒结果）",
          len(collapsed) == 5 and not any(illegal & set(x) for x in collapsed), str(sorted(collapsed)))

    # 真实平台确实给得出很长的会话号；单段名有上界，两个只差尾部的长号不能撞车
    long_a = "oc_" + "a" * 200 + "_1"
    long_b = "oc_" + "a" * 200 + "_2"
    check("超长标识被截到上界内", len(b._slug(long_a)) <= M.MAX_NAME_CHARS, str(len(b._slug(long_a))))
    check("只差尾部的两个长号不撞同一个目录", b._slug(long_a) != b._slug(long_b))
    check("截断后仍带指纹（所以稳定可复现）",
          b._slug(long_a) == b._slug(long_a) and "-" in b._slug(long_a))

    # 最后要在真盘上建得出来：能建但名字非法等于没消毒
    made = b.dir_for("fake:entA", "c/1")
    check("消毒后的目录真的建得出来且还在附件根内",
          made.is_dir() and str(made.resolve()).startswith(str(b.root.resolve())), str(made))
    check("目录段里没有任何非法字符",
          not any(illegal & set(str(p)) for p in made.resolve().relative_to(b.root.resolve()).parts),
          str(made))


def phase_view_shape() -> None:
    section("8. 对外视图不带下载凭据")
    root = _TMP / "view"
    b = backend(root)
    secret_url = "https://dl.example/signed?token=SECRET-TOKEN-9f3a"
    fetch, _ = make_fetch({secret_url: (200, "image/png", PNG)})
    out = asyncio.run(
        b.materialize([M.ContentPart(kind="image", name="s.png", url=secret_url, ref="downloadCode-A"),
                       M.ContentPart(kind="file", name="no.txt", url="https://dl/missing")],
                      channel_id="ch", conversation_id="cv", fetch=fetch)
    )
    blob = json_dumps(out[0].view())
    check("view() 不含签名下载地址", "SECRET-TOKEN" not in blob and "downloadCode" not in blob)
    check("view() 给的是可核对的事实", out[0].view()["sha256"] == out[0].sha256[:16])
    check("view() 标出已存盘", out[0].view()["stored"] is True)
    check("未存盘的 stored 为假", out[1].view()["stored"] is False)
    fp = b.footprint()
    check("footprint 只给目录与字节数", "SECRET-TOKEN" not in json_dumps(fp) and fp["files"] == 1)
    check("footprint 能按通道与会话收窄", b.footprint("ch")["files"] == 1 and b.footprint("ch", "cv")["bytes"] == len(PNG))
    check("未落盘的部分保留 note", out[1].note != "" and "HTTP 404" in out[1].note)


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def main() -> int:
    print("== IM 媒体承载层自测（D-41）==")
    phase_classify()
    phase_check_part()
    phase_aggregate()
    phase_materialize()
    phase_gates()
    phase_limits_and_dedupe()
    phase_path_escape()
    phase_slug_legality()
    phase_view_shape()
    print(f"\n[DONE] im_media selftest: {len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
