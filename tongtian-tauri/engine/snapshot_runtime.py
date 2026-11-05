"""D-13（#140）应用快照流水线：**用户主动触发 → 预览 → 确认 → 上送**，机密级不上送。

这块代码解决的是「上下文获取弱」里的**快照**那一半：用户把最前方应用的屏幕内容（截图 +
可读文本）交给对话，但**在真正交给模型之前必须看得见、取消得了**，而且判定为机密的
那份**不许上送**。台账 §D-13 的验收三条里，本模块负责「上送前可见可取消」与
「机密级内容不上送」；「浏览器任务可复用已授权会话」在 :mod:`snapshot_relay`。

边界（写代码之前先定死，代码不许越过）：

1. **本模块不截屏。** OS 级屏幕捕获、前台应用识别与快捷键都在桌面壳（Rust）里，
   workspace 车道的 IPC 还没交棒（`src/shared/contracts_workspace.ts` 的
   ``IPC_CHANNELS_WORKSPACE`` 为空、``src-tauri/src/ipc/workspace.rs`` 只有 NOT_IMPLEMENTED
   分支）。所以引擎收的是**调用方交上来的采集物**（正文文本 / base64 图片），
   真机截屏一律记 **未验收**，不许读成「应用快照已可用」。
2. **正文只留在内存里，不落盘。** 未确认的草稿一旦被写进磁盘，就是用户在
   「还没决定要不要给」的时候先被存了一份副本。落盘的只有决策审计
   （``{home}/snapshots/decisions.jsonl``：谁、什么时候、什么分级、上送还是拦下、
   字节数与指纹），**没有正文、没有图片、没有密钥**。代价如实写在
   :func:`SnapshotRegistry.status` 的回话里：引擎重启后未确认的草稿会失效，需要重新触发。
3. **分级判定与 Rust ``security/classify.rs`` 同源。** 三张关键词表、PII 形态、
   置信度取值、`force_local` 口径都照那份实现，漂移由
   ``tests/integration/engine_snapshot_selftest.py`` 直接读 Rust 源码比对字面量钉住。
   **有一处有意的分歧**：明文密钥（``sk-`` / ``sk_``）在 Rust 侧只进脱敏、不进分级，
   本模块把它算成「拦下不上送」——快照上送这个动作本身就是把内容发给模型，
   密钥进了上下文就收不回来。分歧在本文件里点名，不藏进实现细节。
4. **预览与上送是同一条代码路径产出的同一段文本。** 「预览可见」如果和真正发出去的
   内容不是同一个函数算出来的，界面就只是在演一场戏；自测直接断言
   ``preview.text == payload.text``。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:  # privacy_guard 是引擎侧 PII 形态与脱敏的唯一实现（与 Rust security/pii.rs 同源）
    import privacy_guard as pg
except Exception:  # noqa: BLE001 - 缺件时分级仍能跑（关键词半边），PII 半边会如实说没检查
    pg = None  # type: ignore[assignment]

try:
    import data_paths as dp
except Exception:  # noqa: BLE001
    dp = None  # type: ignore[assignment]


# ── 数据根与上限 ────────────────────────────────────────────────

SNAPSHOTS_DIRNAME = "snapshots"
DECISIONS_FILENAME = "decisions.jsonl"

#: 单份采集正文的字符上限：屏幕可读文本远小于文档，超了就是有人把整本日志粘进来了
MAX_CAPTURE_TEXT_CHARS = 200_000
#: 预览与上送正文的截断点（进模型上下文的东西要有数，界面按这份长度展示）
MAX_UPLINK_TEXT_CHARS = 12_000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
#: 允许作为快照上送的图片形态（截图导出只会是这三种）
IMAGE_MIME_WHITELIST = ("image/png", "image/jpeg", "image/webp")
#: 同时在库里等待确认的草稿数上限；超出时最旧的一份按「过期」处理并记进审计
MAX_PENDING_DRAFTS = 12
#: 预览到确认的存活时间（分钟）。太短会让用户在读长截图时被抢跑，太长等于把屏幕内容挂在库里
DEFAULT_TTL_MINUTES = 15

# ── 分级：与 Rust security/classify.rs 同源 ─────────────────────

SENSITIVITY_PUBLIC = "public"
SENSITIVITY_INTERNAL = "internal"
SENSITIVITY_CONFIDENTIAL = "confidential"

SENSITIVITY_LABELS = {
    SENSITIVITY_PUBLIC: "未检出敏感",
    SENSITIVITY_INTERNAL: "内部敏感",
    SENSITIVITY_CONFIDENTIAL: "机密",
}

#: 证件/资质类关键词 → confidential（severity high）
CERT_KEYWORDS = ("身份证", "护照", "营业执照", "驾驶证", "社保卡", "银行卡")
#: 商业机密类关键词 → confidential（severity medium）
COMMERCIAL_KEYWORDS = (
    "合同",
    "协议",
    "薪资",
    "报价",
    "招标",
    "投标",
    "财务报表",
    "客户名单",
)
#: 内部标记类关键词 → internal（severity low）
INTERNAL_KEYWORDS = ("内部", "机密", "绝密", "仅限内网")

#: 数值型 PII 的展示名（与 Rust 的 finding.pattern 逐字一致）
PII_PATTERNS = {
    "idCard": ("18 位身份证号", "个人身份证号"),
    "phone": ("11 位手机号", "个人手机号"),
    "bankCard": ("16-19 位银行卡号", "银行账户信息"),
    "email": ("邮箱地址", "个人邮箱地址"),
}

#: 本模块内部对「命中来源」的分类，与 Rust ScanFinding.kind 对齐并多加一类
KIND_PII = "pii"
KIND_KEYWORD = "keyword"
KIND_SECRET = "secret"

# ── 草稿状态机 ──────────────────────────────────────────────────

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_WITHHELD = "withheld"

STATUS_LABELS = {
    STATUS_PENDING: "待确认（尚未上送）",
    STATUS_CONFIRMED: "已确认并生成上送载荷",
    STATUS_CANCELLED: "已取消（草稿已销毁）",
    STATUS_EXPIRED: "已过期（草稿已销毁）",
    STATUS_WITHHELD: "被拦下（机密级不上送）",
}

# ── 结论码：界面按 code 选文案，中文句子留在本模块里当唯一事实源 ──

CODE_NOTHING_CAPTURED = "SNAPSHOT_NOTHING_CAPTURED"
CODE_TEXT_TOO_LARGE = "SNAPSHOT_TEXT_TOO_LARGE"
CODE_IMAGE_TOO_LARGE = "SNAPSHOT_IMAGE_TOO_LARGE"
CODE_IMAGE_FORMAT = "SNAPSHOT_IMAGE_FORMAT_UNSUPPORTED"
CODE_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
CODE_EXPIRED = "SNAPSHOT_EXPIRED"
CODE_CONFIDENTIAL = "SNAPSHOT_CONFIDENTIAL_WITHHELD"
CODE_INTERNAL_NEEDS_ACK = "SNAPSHOT_INTERNAL_NEEDS_ACK"
CODE_REDACTION_BLOCKED = "SNAPSHOT_REDACTION_BLOCKED"
CODE_BAD_BASE64 = "SNAPSHOT_IMAGE_BASE64_INVALID"
CODE_LIMIT_INVALID = "SNAPSHOT_LIMIT_INVALID"
CODE_PARAM_MISSING = "SNAPSHOT_PARAM_MISSING"

#: 哪些码是「拦下」（403 语义），哪些是「这份草稿的事没做对」（400 语义）
_WITHHELD_CODES = frozenset({CODE_CONFIDENTIAL, CODE_INTERNAL_NEEDS_ACK, CODE_REDACTION_BLOCKED})


def _brief(text: object, limit: int = 200) -> str:
    """把任意回话压成一行短文本（审计与界面都不该拿到整段屏幕内容）。"""
    flat = " ".join(str(text or "").split())
    # 省略号也占一位：说好「最长 limit 字」就不能交付 limit+1 字
    return flat if len(flat) <= limit else flat[: max(1, limit - 1)] + "…"


# ── 分级 ────────────────────────────────────────────────────────


@dataclass
class Classification:
    """一份文本的分级结论。字段名对齐 Rust ``ScanResult``，界面与路由都读这一份。"""

    sensitivity: str = SENSITIVITY_PUBLIC
    confidence: float = 0.5
    findings: list[dict] = field(default_factory=list)
    summary: str = ""
    pii: bool = False
    force_local: bool = False
    #: 明文密钥命中数。Rust 侧不进分级，本模块据此拦下上送（见文件头第 3 条边界）
    secret_hits: int = 0

    @property
    def is_confidential(self) -> bool:
        return self.sensitivity == SENSITIVITY_CONFIDENTIAL

    def to_dict(self) -> dict:
        return {
            "sensitivity": self.sensitivity,
            "sensitivityLabel": SENSITIVITY_LABELS.get(self.sensitivity, self.sensitivity),
            "confidence": self.confidence,
            "findings": list(self.findings),
            "summary": self.summary,
            "pii": self.pii,
            "forceLocal": self.force_local,
            "secretHits": self.secret_hits,
        }


def _push(findings: list[dict], kind: str, pattern: str, severity: str, description: str) -> None:
    """同 kind+pattern 只记一条（Rust 的 push_finding 也是这个去重口径）。"""
    for item in findings:
        if item["kind"] == kind and item["pattern"] == pattern:
            return
    findings.append({"kind": kind, "pattern": pattern, "severity": severity, "description": description})


def _pii_hits(text: str) -> dict[str, int]:
    """按 Rust/privacy_guard 同源的形态数一遍 PII 与明文密钥。

    走的是 ``privacy_guard`` 的切分与判定件（含私有的 ``_digit_runs`` / ``_word_runs``）——
    这里要的是**同一份规则**，不是第二份实现，所以宁可引用私有件，也不在快照里
    另抄一遍数字分组逻辑。
    """
    if pg is None:
        return {}
    counts: dict[str, int] = {}
    for _start, _end, digits in pg._digit_runs(text):
        if pg.is_id_card(digits):
            counts["idCard"] = counts.get("idCard", 0) + 1
        elif pg.is_phone(digits):
            counts["phone"] = counts.get("phone", 0) + 1
        elif pg.is_bank_card(digits):
            counts["bankCard"] = counts.get("bankCard", 0) + 1
    if any(pg.is_email(word) for _s, _e, word in pg._word_runs(text, pg._is_email_char)):
        counts["email"] = counts.get("email", 0) + 1
    secrets = sum(1 for _s, _e, word in pg._word_runs(text, pg._is_secret_char) if pg.is_secret(word))
    if secrets:
        counts["secret"] = secrets
    return counts


def classify_text(text: str) -> Classification:
    """单份文本分级（空文本按 public，不报错）。判定顺序与置信度取值照 Rust ``classify``。"""
    body = str(text or "")
    findings: list[dict] = []
    counts = _pii_hits(body)
    pii = any(counts.get(key) for key in PII_PATTERNS)
    secret_hits = int(counts.get("secret") or 0)

    for key, (pattern, description) in PII_PATTERNS.items():
        if counts.get(key):
            _push(findings, KIND_PII, pattern, "high", description)

    for keyword in CERT_KEYWORDS:
        if keyword in body:
            _push(findings, KIND_KEYWORD, keyword, "high", "个人证件/资质信息")
    for keyword in COMMERCIAL_KEYWORDS:
        if keyword in body:
            _push(findings, KIND_KEYWORD, keyword, "medium", "商业机密文件")
    for keyword in INTERNAL_KEYWORDS:
        if keyword in body:
            _push(findings, KIND_KEYWORD, keyword, "low", "内部敏感标记")
    if secret_hits:
        _push(findings, KIND_SECRET, "明文密钥（sk- / sk_）", "high", "一旦进入模型上下文无法收回")

    has = lambda severity: any(item["severity"] == severity for item in findings)  # noqa: E731
    if pii:
        sensitivity, confidence = SENSITIVITY_CONFIDENTIAL, 0.95
    elif has("high"):
        sensitivity, confidence = SENSITIVITY_CONFIDENTIAL, 0.9
    elif has("medium"):
        sensitivity, confidence = SENSITIVITY_CONFIDENTIAL, 0.85
    elif has("low"):
        sensitivity, confidence = SENSITIVITY_INTERNAL, 0.7
    else:
        sensitivity, confidence = SENSITIVITY_PUBLIC, 0.5

    if sensitivity == SENSITIVITY_CONFIDENTIAL:
        summary = "检测到高度敏感信息，建议仅使用本地模型处理"
    elif sensitivity == SENSITIVITY_INTERNAL:
        summary = "检测到内部敏感内容，建议限制使用范围"
    else:
        summary = "未检测到敏感信息，可正常使用"

    return Classification(
        sensitivity=sensitivity,
        confidence=confidence,
        findings=findings,
        summary=summary,
        pii=pii,
        # Rust 口径：PII 或机密命中即要求本地通道（PRD §4.2.3 按数据敏感度自动切换）
        force_local=pii or sensitivity == SENSITIVITY_CONFIDENTIAL,
        secret_hits=secret_hits,
    )


def privacy_mode_from_runtime(runtime: dict | None) -> str:
    """上送正文的脱敏档位，直接取 ``privacy_guard`` 那一份读数，不另立档位。"""
    if pg is None:
        return "off"
    return pg.from_runtime(runtime)


def build_uplink_text(text: str, mode: str) -> tuple[str, dict[str, int]]:
    """预览与上送**共用**的正文产出：先按脱敏档位处理，再截到上送上限。

    返回 (将进入模型的正文, 脱敏命中数)。截断这件事必须在预览里看见，
    否则用户以为模型读到了整屏。
    """
    body = str(text or "")
    counts: dict[str, int] = {}
    if pg is not None and mode == "mask":
        body, counts = pg.redact_with_counts(body)
    if len(body) > MAX_UPLINK_TEXT_CHARS:
        body = body[:MAX_UPLINK_TEXT_CHARS]
    return body, counts


# ── 草稿 ────────────────────────────────────────────────────────


@dataclass
class SnapshotDraft:
    """一次采集的现场。正文字节只在内存里，审计只记它的长度与指纹。"""

    draft_id: str
    task_id: str = ""
    app_label: str = ""
    origin: str = ""
    text: str = ""
    image_bytes: bytes = b""
    image_mime: str = ""
    classification: Classification = field(default_factory=Classification)
    status: str = STATUS_PENDING
    created_at: float = 0.0
    expires_at: float = 0.0
    confirmed_at: float = 0.0
    include_image: bool = False
    acknowledged_internal: bool = False
    uplink_mode: str = "off"
    #: 终态原因（被拦下的 code / 取消 / 过期），界面按它选文案
    outcome_code: str = ""
    outcome_note: str = ""

    @property
    def has_image(self) -> bool:
        return bool(self.image_bytes)

    def text_fingerprint(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", errors="replace")).hexdigest()[:16]

    def is_expired(self, now: float) -> bool:
        return self.status == STATUS_PENDING and now >= self.expires_at

    def base_row(self) -> dict:
        """给人看的元数据（不含正文，也不含图片字节）。"""
        return {
            "draftId": self.draft_id,
            "taskId": self.task_id,
            "appLabel": self.app_label or "未标注来源应用",
            "origin": self.origin,
            "status": self.status,
            "statusLabel": STATUS_LABELS.get(self.status, self.status),
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "hasImage": self.has_image,
            "imageMime": self.image_mime,
            "imageBytes": len(self.image_bytes),
            "textChars": len(self.text),
            "classification": self.classification.to_dict(),
            "outcomeCode": self.outcome_code,
            "outcomeNote": self.outcome_note,
        }


# ── 注册表 ──────────────────────────────────────────────────────


class SnapshotRegistry:
    """进程内的快照草稿库 + 决策审计。

    审计走 JSONL 且**永不写正文**（对齐 ``browser_use`` 那句「输入正文、页面正文与截图
    内容不写入审计」）；上送条目记字节数与内容指纹，被拦条目记 code 与命中的模式名。
    """

    def __init__(self, *, clock: Callable[[], float] | None = None, ttl_minutes: int = DEFAULT_TTL_MINUTES) -> None:
        self._drafts: dict[str, SnapshotDraft] = {}
        self._clock = clock or time.time
        self.ttl_minutes = max(1, int(ttl_minutes))
        self._decisions: list[dict] = []

    # 采集

    def capture(
        self,
        *,
        text: object = "",
        image_b64: object = "",
        image_mime: object = "",
        app_label: object = "",
        task_id: object = "",
        origin: object = "",
        privacy_runtime: dict | None = None,
    ) -> tuple[SnapshotDraft | None, str, str]:
        """用户主动触发一次采集。返回 (草稿, code, 中文说明)；code 为空表示成功。"""
        now = self._clock()
        body = str(text or "")
        image_raw = str(image_b64 or "")
        if not body.strip() and not image_raw.strip():
            return None, CODE_NOTHING_CAPTURED, (
                "这次触发既没有可读文本也没有截图：应用快照需要调用方把采集物交上来 "
                "（本模块不截屏，OS 采集半边在桌面壳里，桌面半边未接线 ⇒ 真机未验收）"
            )
        if len(body) > MAX_CAPTURE_TEXT_CHARS:
            return None, CODE_TEXT_TOO_LARGE, (
                f"采集正文 {len(body)} 个字符，超过单份快照上限 {MAX_CAPTURE_TEXT_CHARS}；"
                "整本日志请先用文档工具读，别塞进快照"
            )
        mime = str(image_mime or "").strip().lower()
        image_bytes = b""
        if image_raw.strip():
            if mime not in IMAGE_MIME_WHITELIST:
                return None, CODE_IMAGE_FORMAT, (
                    f"快照图片格式「{_brief(mime, 60) or '未声明'}」不收：屏幕截图只会是 "
                    + "/".join(IMAGE_MIME_WHITELIST)
                )
            payload = image_raw.split(",", 1)[-1]
            try:
                # validate=True：非 base64 字符必须炸，而不是被静默丢掉后当成「一张空图片」收下；
                # 折行与空格先去掉（76 列包装的 base64 是合法输入，不是坏输入）
                image_bytes = base64.b64decode("".join(payload.split()), validate=True)
            except Exception as exc:  # noqa: BLE001
                return None, CODE_BAD_BASE64, f"快照图片不是合法 base64：{_brief(exc, 120)}"
            if not image_bytes:
                return None, CODE_BAD_BASE64, "快照图片解出来是 0 字节：要么真传了空图，要么编码不对，两种都不当成功"
            if len(image_bytes) > MAX_IMAGE_BYTES:
                return None, CODE_IMAGE_TOO_LARGE, (
                    f"快照图片 {len(image_bytes)} 字节，超过 {MAX_IMAGE_BYTES} 字节上限"
                )

        classification = classify_text(body)
        draft = SnapshotDraft(
            draft_id=uuid.uuid4().hex[:12],
            task_id=str(task_id or "")[:64],
            app_label=_brief(app_label, 120),
            origin=_brief(origin, 120),
            text=body,
            image_bytes=image_bytes,
            image_mime=mime,
            classification=classification,
            status=STATUS_PENDING,
            created_at=now,
            expires_at=now + self.ttl_minutes * 60.0,
            uplink_mode=privacy_mode_from_runtime(privacy_runtime),
        )
        self._drafts[draft.draft_id] = draft
        self._evict_overflow(now)
        self._record(
            "capture",
            draft,
            ok=True,
            extra={"withheld": classification.is_confidential, "privacyMode": draft.uplink_mode},
        )
        return draft, "", ""

    # 读取

    def get(self, draft_id: str) -> SnapshotDraft | None:
        return self._drafts.get(str(draft_id or "").strip())

    def list(self, *, task_id: str = "", status: str = "") -> list[SnapshotDraft]:
        rows = []
        for draft in self._drafts.values():
            if task_id and draft.task_id != task_id:
                continue
            if status and draft.status != status:
                continue
            rows.append(draft)
        return sorted(rows, key=lambda item: item.created_at, reverse=True)

    def preview(self, draft_id: str, *, include_image: bool | None = None) -> tuple[dict | None, str, str, int]:
        """上送前可见的那一眼：正文按**将要真正发出的那份**产出，并说清什么会被拦下。"""
        draft = self.get(draft_id)
        if draft is None:
            return None, CODE_NOT_FOUND, "没有这份快照草稿：可能已取消、已过期，或从没触发成功", 404
        if draft.is_expired(self._clock()):
            self._retire(draft, CODE_EXPIRED, "预览未在存活期内确认，草稿已销毁")
        if draft.status == STATUS_EXPIRED:
            return None, CODE_EXPIRED, (
                f"这份快照已过期（存活 {self.ttl_minutes} 分钟），草稿已销毁，需要重新触发"
            ), 410
        if draft.status == STATUS_CANCELLED:
            return None, CODE_NOT_FOUND, "这份快照已被取消，草稿已销毁，预览没有内容可给", 404
        body, redacted = build_uplink_text(draft.text, draft.uplink_mode)
        want_image = draft.include_image if include_image is None else bool(include_image)
        row = draft.base_row()
        row.update(
            {
                "preview": body,
                "previewChars": len(body),
                "truncated": len(draft.text) > len(body),
                "uplinkLimit": MAX_UPLINK_TEXT_CHARS,
                "redactionMode": draft.uplink_mode,
                "redactedCounts": redacted,
                "imageIncluded": want_image and draft.has_image,
                "imageNote": self._image_note(draft, want_image),
                "wouldUplink": not draft.classification.is_confidential and draft.classification.secret_hits == 0,
                "needsAck": draft.classification.sensitivity == SENSITIVITY_INTERNAL,
                "ttlMinutes": self.ttl_minutes,
                "storageNote": (
                    "正文只在本引擎进程内存里，不落盘；引擎重启后未确认的草稿会失效，需要重新触发"
                ),
            }
        )
        return row, "", "", 200

    def _image_note(self, draft: SnapshotDraft, want_image: bool) -> str:
        if not draft.has_image:
            return "这次触发没有图片。"
        if not want_image:
            return (
                "截图默认不上送：图片正文本地无法检测或脱敏（与出站脱敏同一条边界），"
                "勾选后才随载荷发出，届时分级判断由确认这一下的人承担。"
            )
        return (
            "勾选了随载荷发出截图：图片内容无法本地检测，若屏上有机密内容，这条拦截挡不住它 —— "
            "这一步只由人主动勾一次，模块不会默认打开。"
        )

    # 终态

    def cancel(self, draft_id: str, *, reason: str = "") -> tuple[dict | None, str, str, int]:
        draft = self.get(draft_id)
        # 已销毁的草稿对这三件事就是「不在了」：第二次取消不能再写一条 retire，也不能回 200 假装又取消了一次
        if draft is None or draft.status in (STATUS_CANCELLED, STATUS_EXPIRED):
            return None, CODE_NOT_FOUND, "没有这份快照草稿：可能已取消、已过期，或从没触发成功", 404
        had_uplink = draft.status == STATUS_CONFIRMED
        self._retire(
            draft,
            "USER_CANCELLED",
            reason
            or (
                "用户销毁了这份已确认的草稿：引擎内存里的副本清掉了，但载荷在上一次确认时就交给了调用方，撤不回来"
                if had_uplink
                else "用户取消，草稿已销毁"
            ),
            extra={"hadUplink": had_uplink},
        )
        row = draft.base_row()
        row["destroyed"] = True
        # 界面必须能说清「这次取消做到了什么、没做到什么」，不能把「已上送」粉饰成「已撤回」
        row["hadUplink"] = had_uplink
        row["cancelNote"] = (
            "取消销毁的是引擎内存里的这份草稿；上一次确认交出去的载荷不在这次取消的范围内（撤不回）"
            if had_uplink
            else "这份还没上送：取消即销毁，没有任何内容离开本机"
        )
        return row, "", "", 200

    def confirm(
        self,
        draft_id: str,
        *,
        include_image: bool = False,
        acknowledge_internal: bool = False,
        task_id: str = "",
    ) -> tuple[dict | None, str, str, int]:
        """确认后产出上送载荷。机密级（含明文密钥）**一律不上送，没有绕过开关**。"""
        draft = self.get(draft_id)
        if draft is None:
            return None, CODE_NOT_FOUND, "没有这份快照草稿：可能已取消、已过期，或从没触发成功", 404
        now = self._clock()
        if draft.is_expired(now):
            self._retire(draft, CODE_EXPIRED, "确认前已过存活期，草稿已销毁")
        if draft.status == STATUS_EXPIRED:
            return None, CODE_EXPIRED, (
                f"这份快照已过期（存活 {self.ttl_minutes} 分钟），需要重新触发一次采集"
            ), 410
        if draft.status == STATUS_CANCELLED:
            return None, CODE_NOT_FOUND, "这份快照已被取消，草稿已销毁，确认不会再把它捞回来", 404

        codes = self._gate(draft, acknowledge_internal=acknowledge_internal)
        if codes:
            code, message = codes
            if draft.status != STATUS_WITHHELD or draft.outcome_code != code:
                draft.status = STATUS_WITHHELD
                draft.outcome_code = code
                draft.outcome_note = message
                self._record("withheld", draft, ok=False, extra={"code": code})
            return None, code, message, 403 if code in _WITHHELD_CODES else 400

        draft.include_image = bool(include_image) and draft.has_image
        draft.acknowledged_internal = bool(acknowledge_internal)
        if task_id:
            draft.task_id = str(task_id)[:64]
        body, redacted = build_uplink_text(draft.text, draft.uplink_mode)
        blocks: list[dict] = [{"type": "text", "text": body}]
        if draft.include_image:
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{draft.image_mime};base64,"
                        + base64.b64encode(draft.image_bytes).decode("ascii")
                    },
                }
            )
        first_confirm = draft.status != STATUS_CONFIRMED
        draft.status = STATUS_CONFIRMED
        draft.confirmed_at = draft.confirmed_at or now
        draft.outcome_code = ""
        draft.outcome_note = ""
        payload = {
            "ok": True,
            "draftId": draft.draft_id,
            "taskId": draft.task_id,
            "appLabel": draft.app_label or "未标注来源应用",
            "contextBlocks": blocks,
            "textChars": len(body),
            "truncated": len(draft.text) > len(body),
            "redactionMode": draft.uplink_mode,
            "redactedCounts": redacted,
            "imageIncluded": draft.include_image,
            "forceLocal": draft.classification.force_local,
            "sensitivity": draft.classification.sensitivity,
            "routeNote": (
                "载荷标了 forceLocal：本模块不替你选模型，通道选择由路由/档位那一段决定"
                "（引擎不承诺自动切本地）"
            )
            if draft.classification.force_local
            else "",
            "repeatConfirm": not first_confirm,
            "note": (
                "重复确认返回同一份载荷，不会二次上送；要改「带不带截图」就再确认一次"
                if not first_confirm
                else ""
            ),
            "destroyed": False,
        }
        if first_confirm:
            self._record("confirm_uplink", draft, ok=True, extra={"imageIncluded": draft.include_image})
        return payload, "", "", 200

    def _gate(self, draft: SnapshotDraft, *, acknowledge_internal: bool) -> tuple[str, str] | None:
        """拦下判定。机密（含明文密钥）无绕过；内部敏感要一次显式勾选。"""
        hit_names = "、".join(item["pattern"] for item in draft.classification.findings[:6]) or "（未列出）"
        if draft.classification.is_confidential:
            return CODE_CONFIDENTIAL, (
                f"这份快照判为机密（命中：{hit_names}），一律不上送——验收口径里的"
                "「机密级内容不上送」是一条硬闸，没有开关可以绕过它；"
                "要用的话请先在来源应用里把敏感段去掉再重新触发一次。"
            )
        if draft.uplink_mode == "block" and (draft.classification.pii or draft.classification.secret_hits):
            marks = "与".join(
                name
                for name, hit in (("PII", draft.classification.pii), ("明文密钥", bool(draft.classification.secret_hits)))
                if hit
            )
            return CODE_REDACTION_BLOCKED, (
                f"出站脱敏档位是「命中即中止」，而这份快照含 {marks}；"
                "本次不生成上送载荷，可在「设置 → 系统设置 → 数据共享」改为脱敏后发送。"
            )
        if draft.classification.sensitivity == SENSITIVITY_INTERNAL and not acknowledge_internal:
            return CODE_INTERNAL_NEEDS_ACK, (
                f"这份快照含内部敏感标记（命中："
                + "、".join(item["pattern"] for item in draft.classification.findings[:6])
                + "），需要一次显式勾选「我知道这是内部内容」才上送；"
                "它不会被判成机密，所以这道闸是确认而不是禁止。"
            )
        return None

    # 存活与审计

    def _retire(self, draft: SnapshotDraft, code: str, note: str, *, extra: dict | None = None) -> None:
        # 先取回销毁前的量度：正文销毁了，但这条审计还得指得回它销毁的是哪一份内容
        destroyed_bytes = len(draft.text.encode("utf-8", errors="replace"))
        destroyed_fingerprint = draft.text_fingerprint()
        destroyed_image_bytes = len(draft.image_bytes)
        draft.status = STATUS_EXPIRED if code == CODE_EXPIRED else (
            STATUS_CANCELLED if code == "USER_CANCELLED" else draft.status
        )
        draft.outcome_code = code
        draft.outcome_note = note
        draft.text = ""
        draft.image_bytes = b""
        self._record(
            "retire",
            draft,
            ok=False,
            extra={
                "code": code,
                "destroyedTextBytes": destroyed_bytes,
                "destroyedTextFingerprint": destroyed_fingerprint,
                "destroyedImageBytes": destroyed_image_bytes,
                **(extra or {}),
            },
        )

    def _evict_overflow(self, now: float) -> None:
        pending = [d for d in self._drafts.values() if d.status == STATUS_PENDING]
        if len(pending) <= MAX_PENDING_DRAFTS:
            return
        ordered = sorted(pending, key=lambda item: item.created_at)
        for draft in ordered[: len(pending) - MAX_PENDING_DRAFTS]:
            draft.expires_at = now  # 让状态机走「过期」这一条，不另造第六种状态
            self._retire(draft, CODE_EXPIRED, f"待确认草稿超过 {MAX_PENDING_DRAFTS} 份，最旧的一份按过期处理")

    def _audit_file(self) -> Path | None:
        if dp is None:
            return None
        try:
            folder = Path(dp.engine_home()) / SNAPSHOTS_DIRNAME
            folder.mkdir(parents=True, exist_ok=True)
            return folder / DECISIONS_FILENAME
        except Exception:  # noqa: BLE001 - 数据根不可写时退回内存缓冲，不影响判定
            return None

    def _record(self, action: str, draft: SnapshotDraft, *, ok: bool, extra: dict | None = None) -> None:
        """决策审计：**只有元数据**。正文、图片、密钥一律不进这条账。"""
        row = {
            "ts": round(self._clock(), 3),
            "action": action,
            "ok": ok,
            "draftId": draft.draft_id,
            "taskId": draft.task_id,
            "appLabel": draft.app_label,
            "origin": draft.origin,
            "status": draft.status,
            "sensitivity": draft.classification.sensitivity,
            "hitPatterns": [item["pattern"] for item in draft.classification.findings],
            "pii": draft.classification.pii,
            "forceLocal": draft.classification.force_local,
            "textBytes": len(draft.text.encode("utf-8", errors="replace")),
            "textFingerprint": draft.text_fingerprint(),
            "imageBytes": len(draft.image_bytes),
            "redactionMode": draft.uplink_mode,
        }
        row.update(extra or {})
        self._decisions.append(row)
        target = self._audit_file()
        if target is not None:
            try:
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001
                pass

    def decisions(
        self, *, draft_id: str = "", action: str = "", limit: int = 50, offset: int = 0
    ) -> dict:
        rows = [
            row
            for row in self._decisions
            if (not draft_id or row.get("draftId") == draft_id) and (not action or row.get("action") == action)
        ]
        total = len(rows)
        page = rows[offset : offset + limit]
        return {
            "total": total,
            "items": page,
            "hasMore": offset + len(page) < total,
            "note": (
                ""
                if page
                else ("审计里还没有任何决策" if total == 0 else "这一页没有条目（过滤后为空，或 offset 超过了总数）")
            ),
        }

    def status(self) -> dict:
        pending = [d for d in self._drafts.values() if d.status == STATUS_PENDING]
        return {
            "drafts": len(self._drafts),
            "pending": len(pending),
            "ttlMinutes": self.ttl_minutes,
            "maxPendingDrafts": MAX_PENDING_DRAFTS,
            "maxUplinkTextChars": MAX_UPLINK_TEXT_CHARS,
            "maxImageBytes": MAX_IMAGE_BYTES,
            "imageMimeWhitelist": list(IMAGE_MIME_WHITELIST),
            "sensitivityLevels": [
                {"value": key, "label": value} for key, value in SENSITIVITY_LABELS.items()
            ],
            "statuses": [{"value": key, "label": value} for key, value in STATUS_LABELS.items()],
            "codes": [
                CODE_NOTHING_CAPTURED,
                CODE_TEXT_TOO_LARGE,
                CODE_IMAGE_TOO_LARGE,
                CODE_IMAGE_FORMAT,
                CODE_NOT_FOUND,
                CODE_EXPIRED,
                CODE_CONFIDENTIAL,
                CODE_INTERNAL_NEEDS_ACK,
                CODE_REDACTION_BLOCKED,
                CODE_BAD_BASE64,
                CODE_LIMIT_INVALID,
                CODE_PARAM_MISSING,
            ],
            "unverified": [
                "本模块不截屏：真实前台应用识别、OS 屏幕捕获、全局快捷键都在桌面壳里，"
                "workspace 车道的 IPC 未接线 ⇒ 真机采集未验收",
                "截图内容无法本地检测，勾选上送后由确认人承担分级判断",
                "上送载荷只产出 contextBlocks，真正发给模型那一段不经过本模块，"
                "「机密不上送」这条闸只覆盖快照这条入口",
            ],
            "note": (
                "触发 → 预览 → 确认 → 上送 四步都在本模块闭环：预览文本与上送文本"
                "由同一个函数产出（自测按 preview == payload 钉）"
            ),
        }

    def clear(self) -> None:
        self._drafts.clear()
        self._decisions.clear()


REGISTRY = SnapshotRegistry()


def reset_registry(**kwargs: Any) -> SnapshotRegistry:
    """测试与装配用的整体重置（与 workspace_backend 的 ``reset_registry`` 同一口径）。"""
    global REGISTRY
    REGISTRY = SnapshotRegistry(**kwargs)
    return REGISTRY
