"""
通天办公 - IM 媒体的统一承载（缺陷 D-41 的「ContentPart + MediaBackend」那一半）

为什么要有这一层：六条通道的入站事件此前只有 `text`，出站 `send(creds, conversation_id, text)`
也只发文本。用户在群里发的截图、文件、语音，通道要么当「不是文本消息」如实忽略，要么干脆看不见
——引擎因此**从来不知道那条消息里带了什么**。这一层把「一条消息 = 若干部分」变成框架事实，
并且把三条边界一次定死：

1. **大小与类型有闸**。单个部分默认上限 `MAX_PART_BYTES`，一条消息最多 `MAX_PARTS_PER_MESSAGE` 个部分，
   MIME 到 `image/video/audio/file` 的归类由一张表决定。超限、类型不认识、名字里带路径分隔符的，
   **一律拒绝落盘**而不是「先存下来再说」——附件是外部输入，落盘之后就是模型和 shell 的读取面。
2. **落盘只进本层的目录**（`{engine_home}/im_media/{channel_id}/{conversation_id}/`），
   文件名由「sha 前缀 + 消毒过的原名」组成，写之前先做**包含性检查**（resolve 后必须还在根里）。
   下载动作由调用方注入的 `fetch` 完成：通道负责「怎么带上凭据」，本层负责「能写多大、写到哪」。
3. **不下传凭据、不泄露签名 URL**。钉钉的 `downloadUrl`、飞书的临时链接都是**带签名的绝对地址**，
   拿到就能下载。所以 `ContentPart.url` 只活在一次下载里，进台账、进回贴、进 `view()` 的
   永远是 `note`（中文原因）与 `sha256`/`size` 这些事实，绝不回显整串 URL。

诚实边界（不许被读成已做）：
- 本层**不知道任何一家的协议**。哪个字段是 downloadCode、回贴文件要调哪个方法，仍是各通道自己的事；
  没接媒体的通道在 `view()` 里如实标 `supportsMediaIn=False`，引擎把「收到了 1 张图片」这句
  中文说明带进任务文本与台账，**而不是静默丢掉**——丢掉就是替用户谎报「他发了条空消息」。
- 真实凭据下的收发**没跑过**（本机只有假服务端），文件能不能真的在钉钉群里发出去，取决于各家
  机器人权限与配额，不取决于本层。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Sequence

import data_paths as dp

# ── 口径常量（一张地方说清，别散在各通道） ──────────────────────────

PART_KINDS = ("text", "image", "video", "audio", "file")

#: 单个部分的字节上限。20 MiB 是「群里一张原图 / 一份 PDF」的真实体量，
#: 再大就该由用户显式选择放到工作区，而不是 IM 网关顺手替他存。
MAX_PART_BYTES = 20 * 1024 * 1024
#: 一条消息最多几个部分：富文本图文混排常见 3～6 段，8 是留了余量的硬顶。
MAX_PARTS_PER_MESSAGE = 8
#: 一个会话目录最多留多少字节（超出按 mtime 从旧到新清）。附件不能无限堆在用户盘上。
MAX_SESSION_BYTES = 200 * 1024 * 1024
#: 文件名消毒后保留的长度；目录名（会话标识 / 实例键）也走这个上界——
#: 文件系统单段名普遍上限 255，这里留足余量，超长标识截断后靠指纹区分。
MAX_NAME_CHARS = 80
#: 目录名被消毒或截断时补的原文指纹长度：同一实例每次算出同一个，不同标识不撞在一起
SLUG_FINGERPRINT_CHARS = 10

#: MIME → 部分类型。只认「用户真的会在 IM 里发的」那几类，
#: 其余（svg / html / 可执行）一律归不成像的 file 或直接拒——svg 能带脚本，不当图片喂给模型。
_MIME_KINDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/tiff"), "image"),
    (("video/mp4", "video/quicktime", "video/x-msvideo", "video/webm", "video/mpeg"), "video"),
    (("audio/mpeg", "audio/wav", "audio/x-wav", "audio/ogg", "audio/mp4", "audio/aac", "audio/flac"), "audio"),
    (
        (
            "application/pdf",
            "text/plain",
            "text/csv",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/zip",
            "application/x-7z-compressed",
        ),
        "file",
    ),
)

#: 明确拒绝的 MIME（在「能落盘」之前先问「该不该落盘」）
_BLOCKED_MIMES = ("image/svg+xml", "text/html", "application/x-executable", "application/x-msdownload")

_EXT_KINDS = {
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image",
    "bmp": "image", "tif": "image", "tiff": "image",
    "mp4": "video", "mov": "video", "avi": "video", "webm": "video", "mpeg": "video", "mkv": "video",
    "mp3": "audio", "wav": "audio", "ogg": "audio", "m4a": "audio", "aac": "audio", "flac": "audio",
    "amr": "audio", "silk": "audio",
    "pdf": "file", "txt": "file", "md": "file", "csv": "file", "doc": "file", "docx": "file",
    "xls": "file", "xlsx": "file", "ppt": "file", "pptx": "file", "zip": "file", "7z": "file",
}

_UNSAFE_NAME = re.compile(r"[^\w.\-() ]+", re.UNICODE)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Windows 连目录名都不允许出现的字符（会话标识与实例键最终会变成真实目录）
_FS_ILLEGAL = re.compile(r'[<>:"|?*/\\]')

Fetch = Callable[[str], Awaitable[tuple[int, str, bytes]]]


class MediaError(Exception):
    """`CODE: 中文原因` ——与 IM 框架其余错误口径同源（前端 errorTextOf 直接映射）"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# ── 值对象 ─────────────────────────────────────────────────────────


@dataclass
class ContentPart:
    """消息的一个部分：文本、图片、视频、语音或文件

    `url` / `ref` 是**平台侧的一次性凭据面**（钉钉 downloadUrl 带签名、飞书是临时链接），
    只在本次下载内有效；任何对外视图（台账、回贴、界面）都只读 `kind/name/mime/size/sha256/note`。
    """

    kind: str = "text"
    text: str = ""
    name: str = ""
    mime: str = ""
    size: int = -1  # -1 表示平台没说，不能当成 0
    url: str = ""
    ref: str = ""
    sha256: str = ""
    local_path: str = ""
    note: str = ""
    at_ms: int = 0

    def view(self) -> dict:
        """给台账 / 界面 / 日志的形状：没有 url，也没有 ref 原文"""
        return {
            "kind": self.kind,
            "name": self.name,
            "mime": self.mime,
            "size": self.size,
            "sha256": self.sha256[:16],
            "stored": bool(self.local_path),
            "note": self.note,
        }


@dataclass
class MediaStats:
    """本层的落盘事实（`view()` 给界面，也是配额与清理的依据）"""

    files: int = 0
    bytes: int = 0
    rejected: int = 0
    lastError: str = ""


# ── 归类与消毒（纯函数，自测直接打表） ─────────────────────────────


def kind_for_mime(mime: str) -> str:
    lowered = str(mime or "").strip().lower().split(";")[0].strip()
    if not lowered or lowered in _BLOCKED_MIMES:
        return ""
    for prefixes, kind in _MIME_KINDS:
        if lowered in prefixes:
            return kind
    # 前缀兜底：厂商会给没登记的具体分支（image/heif 这种），认大类不认全名
    for big in ("image", "video", "audio"):
        if lowered.startswith(f"{big}/"):
            return "" if big == "image" and lowered == "image/svg+xml" else big
    if lowered.startswith("text/"):
        return "file"
    return ""


def kind_for_name(name: str, mime: str = "") -> str:
    """先信 MIME（平台说的是准的），MIME 缺失或认不出来才按后缀兜"""
    by_mime = kind_for_mime(mime)
    if by_mime:
        return by_mime
    ext = str(name or "").rsplit(".", 1)
    if len(ext) == 2 and ext[1].lower() in _EXT_KINDS:
        return _EXT_KINDS[ext[1].lower()]
    return ""


def safe_name(raw: str) -> str:
    """只留一个文件名：路径分隔符、控制字符、开头点号一律去掉

    钉钉/飞书给的 `fileName` 是用户能控制的字符串，`../../x` 或 `a\\b` 直接拼路径就是穿越。
    """
    name = _CONTROL_CHARS.sub("", str(raw or "")).strip()
    name = name.replace("\\", "/").split("/")[-1]
    name = _UNSAFE_NAME.sub("_", name).strip(" .")
    if len(name) > MAX_NAME_CHARS:
        head, _, tail = name.rpartition(".")
        name = f"{head[: MAX_NAME_CHARS - len(tail) - 1]}.{tail}" if tail and head else name[:MAX_NAME_CHARS]
    return name


def check_part(part: ContentPart) -> str:
    """这个部分能不能落盘：返回中文原因，空串表示可以"""
    if part.kind not in PART_KINDS:
        return f"未知的消息部分类型（{part.kind or '空'}）"
    if part.kind != "text" and not (part.url or part.ref):
        return f"{_label(part.kind)}没有可用的下载信息"
    if part.size > MAX_PART_BYTES:
        return f"{_label(part.kind)}体积 {part.size} 字节超过单文件上限 {MAX_PART_BYTES} 字节"
    if str(part.mime or "").lower() in _BLOCKED_MIMES:
        return f"出于安全不接收这类内容（{part.mime}）"
    return ""


def _label(kind: str) -> str:
    return {"text": "文本", "image": "图片", "video": "视频", "audio": "语音", "file": "文件"}.get(kind, kind or "内容")


def aggregate_text(parts: Sequence[ContentPart]) -> str:
    """把「若干部分」摊回旧消息处理方只认的那一个字符串

    文本部分原样拼接；非文本部分**必须留一行中文说明**——任务侧看不到附件时，
    「用户发了条空消息」和「用户发了张图，本平台没接图片下载」是两件完全不同的事。
    """
    texts = [str(p.text or "").strip() for p in parts if p.kind == "text" and str(p.text or "").strip()]
    others = [p for p in parts if p.kind != "text"]
    out = "\n".join(texts).strip()
    notes = []
    for p in others:
        head = f"[{_label(p.kind)}"
        if p.name:
            head += f"：{p.name}"
        if p.size >= 0:
            head += f"，{p.size} 字节"
        head += "]"
        if p.local_path:
            head += "（已存到本机，路径见会话台账）"
        elif p.note:
            head += f"（{p.note}）"
        notes.append(head)
    if notes:
        out = f"{out}\n" + "\n".join(notes) if out else "\n".join(notes)
    return out.strip()


def unsupported_note(parts: Sequence[ContentPart], why: str) -> list[ContentPart]:
    """通道没接下载时给每个非文本部分补一句中文原因（不静默丢）"""
    for p in parts:
        if p.kind != "text" and not p.note:
            p.note = why
    return list(parts)


def has_media(parts: Iterable[ContentPart]) -> bool:
    return any(p.kind != "text" for p in parts or [])


def parts_identity(parts: Sequence[ContentPart]) -> str:
    """附件的排重身份，只给 `im_policy.MessageDedupe` 用，不进视图、台账或日志

    为什么要单独算：一条纯图片消息的「文本」是这里拼出来的中文说明，两张不同的照片
    说明可能逐字相同。按那个文本排重会把用户连发的第二张照片当重复吞掉。
    平台给的引用码（钉钉 downloadCode、飞书 file_key……）才是「哪一份附件」的事实。

    拿不到引用码时返回空串——身份不明就不许冒充成「同一份附件」，
    调用方（排重）因此只退回按 message_id 判。
    """
    out = []
    for p in parts or []:
        if p.kind == "text":
            continue
        tag = str(p.ref or p.name or "").strip()
        if not tag:
            return ""
        out.append(f"{p.kind}:{tag[:120]}")
    return ";".join(out)


# ── MediaBackend：落盘、限额、清理 ─────────────────────────────────


def media_root() -> Path:
    """附件根：与凭据文件同一个数据根（D-43），但**不与任务工作区混写**

    放在 engine_home 下而不是 workspace 下：IM 消息到达时还没有任务，
    写进某个会话目录等于替用户决定这条消息属于哪件事。
    """
    return dp.engine_home() / "im_media"


class MediaBackend:
    """把 ContentPart 变成磁盘上的文件，并把大小/类型/路径三条闸守住

    `fetch(url)` 由调用方注入，返回 `(status, content_type, body)`：本层不碰 aiohttp，
    也不知道哪家的头是 `x-acs-dingtalk-access-token` 还是 `Authorization`。
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        max_part_bytes: int = MAX_PART_BYTES,
        max_parts: int = MAX_PARTS_PER_MESSAGE,
        max_session_bytes: int = MAX_SESSION_BYTES,
    ) -> None:
        self.root = Path(root) if root is not None else media_root()
        self.max_part_bytes = max_part_bytes
        self.max_parts = max_parts
        self.max_session_bytes = max_session_bytes
        self.stats = MediaStats()

    # ── 目录 ──────────────────────────────────────────────────────

    def _slug(self, value: str) -> str:
        """会话标识消毒：去掉分隔符与控制字符，只留最后一段，长度有上界

        但**带 `..` 段的一律拒绝而不是改名收下**：`".."` 消毒成 `"_"` 会和真实标识为 `"_"`
        的会话进同一个目录，两个不相干会话的附件混在一处就是跨会话泄漏。
        宁可不落盘并给出中文原因，也不替用户把两个会话并成一个。
        """
        raw = _CONTROL_CHARS.sub("", str(value or ""))
        if any(seg == ".." for seg in raw.replace("\\", "/").split("/")):
            raise MediaError("IM_MEDIA_PATH_ESCAPE", f"会话标识里带了上级目录段：{raw[:40]}")
        last = raw.replace("\\", "/").split("/")[-1]
        safe = _FS_ILLEGAL.sub("_", last).strip(" .") or "_"
        if safe != last or len(safe) > MAX_NAME_CHARS:
            # 动过原文（换了非法字符、剪过分隔符、截了长度）就要留一段原文指纹：
            # D-41 之后目录名来自 `kind:channelId`，同一个类型可以有多个实例，
            # "fake:a" 与 "fake_a" 若都变成 "fake_a" 就等于把两个实例的附件倒进同一个目录
            # ——那是跨实例泄漏，比不落盘严重得多。超长同理：两个只差尾部的长标识
            # 截断后会撞在一起，而真实平台的会话号确实能长到这个量级。
            digest = hashlib.sha1(last.encode("utf-8")).hexdigest()[:SLUG_FINGERPRINT_CHARS]
            keep = max(1, MAX_NAME_CHARS - len(digest) - 1)
            safe = f"{safe[:keep]}-{digest}"
        return safe

    def dir_for(self, channel_id: str, conversation_id: str) -> Path:
        target = (self.root / self._slug(channel_id) / self._slug(conversation_id)).resolve()
        base = self.root.resolve()
        if base != target and base not in target.parents:
            raise MediaError("IM_MEDIA_PATH_ESCAPE", f"会话目录跑到了附件根之外：{channel_id}/{conversation_id}")
        target.mkdir(parents=True, exist_ok=True)
        return target

    # ── 写 ────────────────────────────────────────────────────────

    def write_bytes(self, channel_id: str, conversation_id: str, part: ContentPart, body: bytes) -> ContentPart:
        """已拿到字节时直接落盘（通道自己下载完的场合）"""
        return self._write(self.dir_for(channel_id, conversation_id), part, body)

    async def materialize(
        self,
        parts: Sequence[ContentPart],
        *,
        channel_id: str,
        conversation_id: str,
        fetch: Fetch | None = None,
    ) -> list[ContentPart]:
        """把带下载信息的部分逐个落到本层目录，返回**同一个列表的新副本**（不改调用方的对象）

        一个部分失败不影响其它部分：图片坏了一只，不该把同一句话里的另一张也丢掉。
        超过 `max_parts` 的附件不下载、不静默删——如实标「本条超出部分未处理」，
        因为回贴与任务文本都要靠这句说明才知道「用户其实发了 9 张图」。
        """
        out: list[ContentPart] = []
        directory: Path | None = None
        seen_media = 0
        for p in parts:
            copy = _clone(p)
            if copy.kind != "text":
                seen_media += 1
                if seen_media > self.max_parts:
                    self.stats.rejected += 1
                    copy.note = copy.note or f"一条消息最多处理 {self.max_parts} 个附件，本条超出部分未下载"
                    out.append(copy)
                    continue
            if directory is None and copy.kind != "text":
                directory = self.dir_for(channel_id, conversation_id)
            out.append(await self._one(copy, directory, fetch))
        return out

    async def _one(self, part: ContentPart, directory: "Path | None", fetch: Fetch | None) -> ContentPart:
        if part.kind == "text":
            return part
        reason = check_part(part)
        if reason:
            self.stats.rejected += 1
            part.note = part.note or reason
            return part
        if directory is None:
            directory = self.dir_for("", "")
        if fetch is None:
            if not part.note:
                part.note = "本机未提供下载通道，附件未落盘"
            self.stats.rejected += 1
            return part
        if not part.url:
            # 只有 downloadCode / file_key 这类平台内引用：能不能换成 URL 是通道的事，
            # 通道没换就别在这里猜——猜出来的下载地址每家都不一样。
            if not part.note:
                part.note = "该平台附件需要先换成下载地址，通道未实现这一步"
            self.stats.rejected += 1
            return part
        try:
            status, ctype, body = await fetch(part.url)
        except MediaError as exc:
            part.note = exc.detail or exc.code
            self.stats.rejected += 1
            return part
        except asyncio.TimeoutError:
            part.note = "下载超时，附件未落盘"
            self.stats.rejected += 1
            return part
        except Exception as exc:  # 网络形状千奇百怪，但不能因为一张图把整条消息弄炸
            part.note = f"下载失败：{type(exc).__name__}"
            self.stats.rejected += 1
            return part
        if int(status or 0) not in range(200, 300):
            part.note = f"平台返回 HTTP {status}，附件未落盘"
            self.stats.rejected += 1
            return part
        if not body:
            part.note = "平台返回空内容，附件未落盘"
            self.stats.rejected += 1
            return part
        if len(body) > self.max_part_bytes:
            part.note = f"实际下载 {len(body)} 字节，超过单文件上限 {self.max_part_bytes} 字节"
            self.stats.rejected += 1
            return part
        # MIME 只在一处说了算：平台头 > 平台在消息里说的 > 后缀；三处都认不出来就不落盘
        mime = str(ctype or part.mime or "").strip().lower().split(";")[0].strip()
        kind = kind_for_mime(mime) or kind_for_name(part.name, mime)
        if not kind:
            part.note = f"内容类型无法归类（{mime or '平台没说'}），未落盘"
            self.stats.rejected += 1
            return part
        part.mime, part.kind = mime, kind
        part.size = len(body)
        return self._write(directory, part, body)

    def _write(self, directory: Path, part: ContentPart, body: bytes) -> ContentPart:
        digest = hashlib.sha256(body).hexdigest()
        part.sha256 = digest
        name = safe_name(part.name or f"{_label(part.kind)}.{_ext(part.mime)}")
        prefix = digest[:12]
        base = self.root.resolve()
        # 内容寻址：先在本会话目录里找同一份内容（用户重发同图、平台按不同名字回调都算一份）
        # 名字不参与判重，否则 `{digest}-a.png` 与 `{digest}-b.png` 会各存一份，配额与统计都失真
        existing = sorted(p for p in directory.glob(f"{prefix}-*") if p.is_file() and p.suffix != ".part")
        if existing:
            part.local_path = str(existing[0])
            part.name = existing[0].name
            part.note = "与已收到的同一份内容（未重复保存）"
            return part
        target = (directory / f"{prefix}-{name}").resolve()
        if base not in target.parents:
            self.stats.rejected += 1
            part.note = "落盘位置越出附件根，已拒绝"
            return part
        part.name = target.name
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(body)
        os.replace(tmp, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        part.local_path = str(target)
        part.at_ms = int(time.time() * 1000)
        self.stats.files += 1
        self.stats.bytes += len(body)
        self.prune(directory)
        return part

    # ── 清理与只读视图 ────────────────────────────────────────────

    def prune(self, directory: Path) -> int:
        """先扫掉半截写入（`.part`），再按 mtime 从旧到新删，直到该会话目录回到配额内

        **只在本会话目录内动手**：别的会话的附件不是本次消息的债。
        """
        for half in directory.glob("*.part"):
            try:
                half.unlink()
            except OSError as exc:
                self.stats.lastError = f"半截文件清理失败：{type(exc).__name__}"
                break
        try:
            files = sorted((p for p in directory.glob("*") if p.is_file() and p.suffix != ".part"),
                           key=lambda p: p.stat().st_mtime)
        except OSError as exc:
            self.stats.lastError = f"目录读取失败：{type(exc).__name__}"
            return 0
        total = sum(p.stat().st_size for p in files)
        removed = 0
        for stale in files:
            if total <= self.max_session_bytes:
                break
            try:
                size = stale.stat().st_size
                stale.unlink()
                total -= size
                removed += 1
            except OSError as exc:
                self.stats.lastError = f"旧附件清理失败：{type(exc).__name__}"
                break
        return removed

    def footprint(self, channel_id: str = "", conversation_id: str = "") -> dict:
        target = self.root
        if channel_id:
            target = target / self._slug(channel_id)
            if conversation_id:
                target = target / self._slug(conversation_id)
        files = [p for p in target.glob("**/*") if p.is_file()] if target.is_dir() else []
        return {
            "root": str(self.root),
            "scope": str(target.relative_to(self.root)) if target != self.root else "(全部)",
            "files": len(files),
            "bytes": sum(p.stat().st_size for p in files),
            "rejected": self.stats.rejected,
            "lastError": self.stats.lastError,
        }


def _clone(part: ContentPart) -> ContentPart:
    return replace(part)


def _ext(mime: str) -> str:
    return {
        "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
        "video/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav",
        "application/pdf": "pdf", "text/plain": "txt",
    }.get(str(mime or "").lower(), "bin")


__all__ = [
    "ContentPart",
    "MAX_PART_BYTES",
    "MAX_PARTS_PER_MESSAGE",
    "MAX_SESSION_BYTES",
    "MediaBackend",
    "MediaError",
    "MediaStats",
    "aggregate_text",
    "check_part",
    "has_media",
    "kind_for_mime",
    "kind_for_name",
    "media_root",
    "parts_identity",
    "safe_name",
    "unsupported_note",
]
