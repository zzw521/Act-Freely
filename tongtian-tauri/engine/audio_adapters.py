"""语音链路的适配器底座（缺陷 D-11 / 会话 #160，可离线半边）

这一层只管三件与「能不能真的听到/说出声音」无关的事，所以可以完全离线自测：

1. **适配器登记与选型**：ASR 与 TTS 各有一条可插拔的通道，谁 uploads、谁本地跑、
   缺什么前置条件，都在登记表里写明，由 `select_adapter()` 按事实筛一遍再决定用谁。
2. **隐私门**：`privacy_mode=True` 时，任何把音频送出本机的通道一律拒绝，
   拒绝理由要说得出是哪一条通道、为什么（D-11 验收项「隐私模式下不上传音频」）。
3. **失败必须有原因**：这一层没有「静默返回空」的出口。每条失败路径都带一句中文原因，
   由调用方（引擎端点 / Rust 界面）原样摊给用户，而不是让输入框少一段字。

刻意不做的事（写了就不算假装）：

* 不起进程、不开麦克风、不解码音频。真机录音是 Rust 侧 `ipc/voice.rs` 的事，
  本地模型能不能跑、麦克风能不能打开、厂商端到端通不通、打断时序准不准，
  在本仓库里没有真机证据之前**一律标未验收**。
* 不猜厂商协议。只实现一条 **OpenAI 兼容** 的 `audio/transcriptions` 与 `audio/speech`，
  其余通道留成登记表里的「未接入」，等真接的时候再补，不在这里凭想象写死。
* 密钥只进请求头，绝不进异常文本、说明列表或落盘快照（与 `ticket_model_channel` 同口径）。

依赖：只用标准库。上游调用是注入点（`transport`），自测喂假上游，真网络只在生产接线侧发生。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

# ── 上限（口径写在名字与说明里，不靠调用方猜） ──────────────────────

#: 单次送识别的音频字节上限（**按解码后的原始字节算**，不是 base64 字符数）。
#: 32 MiB 对 16k/16bit 单声道 PCM 是约 11 分钟 —— 这一档只防「把整段会议录音一次塞进
#: 请求体」这种事故，不代表允许说 11 分钟：真正的说话长度上限由下面的 MAX_ASR_DURATION_MS 管。
#: （#160 交付时这里写的是「约 60 秒」，但那时比较的是 base64 字符数，注释与实现差一个量级；
#: #165 把口径统一成「字节比字节、时长比时长」，两处各自说一句话，不再用一条上限冒充两件事。）
MAX_ASR_AUDIO_BYTES = 32_000_000

#: 单次「按住说话」允许的最长录音（毫秒）。上限的存在理由不是体积而是可用性：
#: 语音是在线的交互，说两分钟的人多半在念文档，那种内容直接粘贴比识别更准。
#: 超限时拒的是**时长**，给出的建议是「分段说」，而不是让人以为麦克风坏了。
MAX_ASR_DURATION_MS = 120_000

#: 单次合成回朗读的文本上限（字符）。一段回答通常远小于它；超了就是模型在吐长文，
#: 朗读按段切，不做整篇合成 —— 整篇合成一次失败就得全部重来。
MAX_TTS_TEXT_CHARS = 4000

#: 朗读分段的目标长度：按句号/问号/换行边界凑到这里就切一段，兼顾「早点开始播」
#: 与「别切得太碎导致韵律断裂」。
TTS_CHUNK_TARGET_CHARS = 240

#: 单条合成请求的超时（秒）。合成比文本生成慢，缺省给到 60，可由配置覆盖。
DEFAULT_TTS_TIMEOUT_SECONDS = 60.0
DEFAULT_ASR_TIMEOUT_SECONDS = 60.0

#: 一次识别最多允许的重试（上游 5xx / 网络错误）。重试是为了不让一次抖动吃掉用户整段话，
#: 但语音是在线的交互，用户在等，无限重试比失败更糟。
MAX_ASR_RETRIES = 1
MAX_TTS_RETRIES = 1

#: 上游响应体上限：语音接口正常返回是转写文本或一小段音频，超过这个量基本是在吐错误页
MAX_RESPONSE_BYTES = 48_000_000

#: 音频载荷的合法格式白名单。不在表里的直接拒，不猜：猜错格式的代价是上游 400 加一句
#: 没人看得懂的报文，而拒掉时我们能说清「支持的格式有哪些」。
SUPPORTED_AUDIO_FORMATS = ("wav", "mp3", "ogg", "opus", "m4a", "aac", "flac", "webm", "pcm16")

#: 没录音、也没配置任何云端通道时给用户的兜底说明
NO_ADAPTER_NOTE = "本轮没有可用的语音通道：本机未探测到可用录音或识别通道，云端通道未配置或缺少密钥"


# ── 适配器登记 ──────────────────────────────────────────────────

@dataclass(frozen=True)
class AudioAdapter:
    """一条语音通道的自述。

    * `uploads_audio` —— 会不会把音频/文本送出本机。这是隐私门的唯一判据，
      不看它叫什么名字、也不看它在哪家云上。
    * `capability_key` —— 由调用方（Rust 探测 / 引擎配置解析）填进 caps 的键名；
      缺这个能力就不候选，理由会写进 notes。
    * `implemented` —— 这一层是否真的有对应的请求构造。登记了但没实现的通道
      只用于「界面可以列出但如实说不支持」，不会被选中去发请求。
    """

    id: str
    label: str
    uploads_audio: bool
    capability_key: str
    implemented: bool = True
    notes: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "uploadsAudio": self.uploads_audio,
            "capabilityKey": self.capability_key,
            "implemented": self.implemented,
            "notes": list(self.notes),
        }


#: ASR 通道。云端那条走 OpenAI 兼容的 /audio/transcriptions；
#: 本机那两条**由桌面端执行**——引擎不起进程、不开麦克风，本层实现的是
#: 「选型、采纳回传结果、以及结果未回来时给出一条可执行的指令」。
#: `implemented` 在 #165 由 False 改 True，含义不是「引擎会识别了」，而是「这一条不再是空壳」：
#: 改之前它和 #160 抓到的 host_player 是同一个形状——隐私模式下一条本机通道都选不出来。
ASR_ADAPTERS: tuple[AudioAdapter, ...] = (
    AudioAdapter(
        id="host_system",
        label="系统语音识别（由桌面端在本机完成）",
        uploads_audio=False,
        capability_key="hostAsr",
        notes=("桌面端识别出的文本经 /api/audio/asr 的 hostResult 回传，引擎不重复识别；"
               "未回传时本层返回 requiresHost 指令，而不是冒充一个识别结果",),
    ),
    AudioAdapter(
        id="local_whisper_cpp",
        label="本机 whisper.cpp / sherpa-onnx 离线识别",
        uploads_audio=False,
        capability_key="localAsr",
        notes=("需要 TT_ASR_BIN 指向可执行文件且模型文件存在，由桌面端探测与执行；"
               "引擎不代起进程，结果未回传时同样返回 requiresHost",),
    ),
    AudioAdapter(
        id="openai_compatible",
        label="OpenAI 兼容云端识别",
        uploads_audio=True,
        capability_key="cloudAsr",
        notes=("音频会上传到配置的模型端点；隐私模式下这条一律不选",),
    ),
    AudioAdapter(
        id="none",
        label="不识别",
        uploads_audio=False,
        capability_key="",
        implemented=False,
        notes=("显式关闭语音输入时使用，界面据此把入口标为不可用而不是点了没反应",),
    ),
)

#: TTS 通道。云端那条走 OpenAI 兼容的 /audio/speech。
TTS_ADAPTERS: tuple[AudioAdapter, ...] = (
    AudioAdapter(
        id="host_player",
        label="系统朗读（由桌面端在本机播放）",
        uploads_audio=False,
        capability_key="hostTts",
        notes=("桌面端自己播放时引擎不再合成，只把要朗读的文本交给它；"
               "本层实现的是文本分段，真机播放未验收"),
    ),
    AudioAdapter(
        id="openai_compatible",
        label="OpenAI 兼容云端合成",
        uploads_audio=True,
        capability_key="cloudTts",
        notes=("待朗读文本会上传到配置的模型端点；隐私模式下这条一律不选",),
    ),
    AudioAdapter(
        id="none",
        label="不朗读",
        uploads_audio=False,
        capability_key="",
        implemented=False,
        notes=("显式关闭朗读时使用",),
    ),
)


def adapters_for(kind: str) -> tuple[AudioAdapter, ...]:
    if kind == "asr":
        return ASR_ADAPTERS
    if kind == "tts":
        return TTS_ADAPTERS
    raise ValueError(f"未知语音通道种类：{kind}（可用：asr / tts）")


def find_adapter(kind: str, adapter_id: str) -> AudioAdapter | None:
    for adapter in adapters_for(kind):
        if adapter.id == adapter_id:
            return adapter
    return None


#: 本机执行的 ASR 通道：选型可以选到它们，但**识别动作不在引擎里**。
#: 走到这些通道时 `run_asr` 返回 `requiresHost`，由桌面端识别后经 `hostResult` 再进来一次。
LOCAL_ASR_ADAPTER_IDS = ("host_system", "local_whisper_cpp")


def is_local_asr(adapter_id: str) -> bool:
    """这条 ASR 通道是否要求由桌面端完成识别（引擎不得对它构造上传请求）。"""
    return str(adapter_id or "") in LOCAL_ASR_ADAPTER_IDS


# ── 配置解析：runtime.audio + modelConfig + 环境变量 ────────────────

@dataclass(frozen=True)
class CloudEndpoint:
    """一条 OpenAI 兼容的语音上游地址。`api_key` 只在构造请求头时使用，不参与任何回显。"""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    voice: str = ""


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def resolve_cloud(runtime: dict | None, model_config: dict | None, env: dict | None = None,
                  *, kind: str = "asr") -> tuple[CloudEndpoint | None, list[str]]:
    """拼出语音上游地址，优先级：`runtime.audio.<kind>` > 专用环境变量 > 模型端点。

    拿不到就返回 None 并说明缺什么 —— 「配置没生效」必须是用户看得见的一句话，
    不能退化成按钮转两圈然后什么都没发生。
    """
    env = env if env is not None else os.environ
    runtime = runtime if isinstance(runtime, dict) else {}
    model_config = model_config if isinstance(model_config, dict) else {}
    block = runtime.get("audio")
    block = block if isinstance(block, dict) else {}
    lane = block.get(kind)
    lane = lane if isinstance(lane, dict) else {}

    prefix = "TT_ASR" if kind == "asr" else "TT_TTS"
    base_url = _first_nonempty(
        lane.get("baseUrl"), env.get(f"{prefix}_BASE_URL"),
        model_config.get("baseUrl"), env.get("TT_BASE_URL"),
    )
    api_key = _first_nonempty(
        lane.get("apiKey"), env.get(f"{prefix}_API_KEY"),
        model_config.get("apiKey"), env.get("TT_API_KEY"),
    )
    model = _first_nonempty(
        lane.get("model"), env.get(f"{prefix}_MODEL"),
        env.get("TT_ASR_MODEL" if kind == "asr" else "TT_TTS_MODEL"),
        "whisper-1" if kind == "asr" else "tts-1",
    )
    voice = _first_nonempty(lane.get("voice"), env.get(f"{prefix}_VOICE"), "alloy")
    timeout = _as_float(
        lane.get("timeoutSeconds") or env.get(f"{prefix}_TIMEOUT_SECONDS"),
        DEFAULT_ASR_TIMEOUT_SECONDS if kind == "asr" else DEFAULT_TTS_TIMEOUT_SECONDS,
    )
    if str(lane.get("enabled") or "").strip().lower() in ("off", "false", "0"):
        return None, [f"{kind.upper()} 云端通道已在设置里关闭"]
    missing = [name for name, value in (("地址", base_url), ("密钥", api_key)) if not value]
    if missing:
        return None, [
            f"{kind.upper()} 云端通道不可用：未配置" + "与".join(missing)
            + f"（可填 runtime.audio.{kind}、{prefix}_BASE_URL / {prefix}_API_KEY，或复用模型端点）"
        ]
    return (
        CloudEndpoint(
            base_url=base_url.rstrip("/"), api_key=api_key, model=model,
            timeout_seconds=timeout, voice=voice,
        ),
        [],
    )


# ── 选型：能力表 + 隐私门 ────────────────────────────────────────

@dataclass(frozen=True)
class Selection:
    """选型结果。`chosen` 为空时必须看 `reasons`，那里写着每条通道为什么没被用。"""

    kind: str
    chosen: str
    reasons: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    upload: bool = False

    def ok(self) -> bool:
        return bool(self.chosen)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "chosen": self.chosen,
            "uploadsAudio": self.upload,
            "reasons": list(self.reasons),
            "skipped": [{"id": i, "reason": r} for i, r in self.skipped],
        }


def select_adapter(kind: str, caps: dict | None, *, privacy_mode: bool = False,
                   preferred: str = "", allow_upload: bool | None = None) -> Selection:
    """按能力表挑一条语音通道，隐私模式是硬门。

    顺序：优先用户点名那条（`preferred`），否则按登记表顺序取第一条满足条件的。
    每条被跳过的通道都要留下原因 —— 用户问「为什么我点了没反应」时，
    答案必须是「系统知道原因」，而不是「试了一下没成功」。
    """
    caps = caps if isinstance(caps, dict) else {}
    adapters = adapters_for(kind)
    known = {a.id for a in adapters}
    reasons: list[str] = []
    skipped: list[tuple[str, str]] = []

    if preferred and preferred not in known:
        reasons.append(f"指定的{kind.upper()}通道「{preferred}」不存在，已改按默认可用顺序选择")
    ordered = ([a for a in adapters if a.id == preferred]
               + [a for a in adapters if a.id != preferred])
    for adapter in ordered:
        if not adapter.implemented:
            # 登记了但这一层没实现：说清楚是「没接入」，不假装它坏了或没配
            skipped.append((adapter.id, "该通道尚未接入引擎侧（本层不构造它的请求）"))
            continue
        if adapter.capability_key and caps.get(adapter.capability_key) is not True:
            skipped.append((adapter.id, f"未探测到该通道的前置条件（{adapter.capability_key}）"))
            continue
        if adapter.uploads_audio:
            if privacy_mode:
                # D-11 验收项：隐私模式下不上传音频。这里不看模型配置，硬拒。
                skipped.append((adapter.id, "隐私模式已开启，音频不送出本机"))
                continue
            if allow_upload is False:
                skipped.append((adapter.id, "本次调用显式禁止上传音频"))
                continue
        return Selection(
            kind=kind, chosen=adapter.id, reasons=reasons, skipped=skipped,
            upload=adapter.uploads_audio,
        )
    if not reasons:
        reasons.append(NO_ADAPTER_NOTE)
    return Selection(kind=kind, chosen="", reasons=reasons, skipped=skipped)


# ── 载荷校验 ────────────────────────────────────────────────────

def validate_audio(payload: dict | None, *, limit: int = MAX_ASR_AUDIO_BYTES,
                   max_duration_ms: int = MAX_ASR_DURATION_MS) -> tuple[str, str, str]:
    """校验音频载荷 → (格式, 数据, 错误)。错误非空时后两项不可信。

    只认三种形态：已 base64 的 `data`、`rawBytes`（测试用的原始字节）、或纯 `path`
    （由调用方读盘后重新递进来 —— 本层不读文件，避免越界与路径歧义）。

    两条上限各说各话：**体积**超限是「别把整段会议塞进一个请求」，
    **时长**超限是「按住说话不该说这么久」，建议分别是换格式和分段说。
    """
    if not isinstance(payload, dict):
        return "", "", "音频载荷缺失：请求体里没有 audio 对象"
    fmt = str(payload.get("format") or "").strip().lower().lstrip(".")
    if fmt in ("pcm", "pcm_s16le", "s16le"):
        fmt = "pcm16"
    if not fmt:
        return "", "", "音频格式未声明：请在 audio.format 里写明（可选：" + "/".join(SUPPORTED_AUDIO_FORMATS) + "）"
    if fmt not in SUPPORTED_AUDIO_FORMATS:
        return fmt, "", (
            f"不支持的音频格式 {fmt}（支持：" + "/".join(SUPPORTED_AUDIO_FORMATS) + "）"
        )
    data = _first_nonempty(payload.get("data"), payload.get("base64"))
    if not data:
        raw = payload.get("rawBytes")
        if isinstance(raw, (bytes, bytearray)):
            import base64
            data = base64.b64encode(bytes(raw)).decode("ascii")
    if not data:
        if str(payload.get("path") or "").strip():
            return fmt, "", "本层不读磁盘路径：请先把音频读成字节再放进 audio.data"
        return fmt, "", "音频数据为空：可能录音被取消，或麦克风没有采到内容"
    duration = payload.get("durationMs")
    if isinstance(duration, (int, float)) and max_duration_ms > 0 and duration > max_duration_ms:
        return fmt, "", (
            f"录音时长 {int(duration)} ms 超出单次上限 {max_duration_ms} ms："
            "请分段说，或改在设置里把语音输入用于短口述"
        )
    # 体积按**解码后的字节**比，与 MAX_ASR_AUDIO_BYTES 的名字同源；
    # base64 在这里只是运输编码，拿它跟字节上限比会让注释与实际差一个量级。
    raw_bytes = (len(data) // 4) * 3
    if raw_bytes > limit:
        return fmt, "", (
            f"音频过大（解码后约 {raw_bytes} 字节，超出上限 {limit}）："
            "请改用采样率更低的单声道，或缩短录音"
        )
    return fmt, data, ""


# ── 上游调用：注入点 + 真实现 ─────────────────────────────────────

Transport = Callable[[str, dict, bytes, float], tuple[int, dict, bytes]]


def http_post(method: str, url: str, headers: dict, body: bytes,
              timeout: float) -> tuple[int, dict, bytes]:
    """标准库实现的一次 HTTP 调用 → (状态码, 响应头, 响应体)。

    只在生产接线侧使用；自测把 `transport` 换成假上游，不联网也能把整条链路跑完。
    网络层的异常在这里收口成一句中文，绝不含密钥。
    """
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise urllib.error.HTTPError(url, 413, "响应体超出上限", None, None)  # type: ignore[arg-type]
            return int(response.status), dict(response.headers.items()), payload
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(4096)
        except Exception:  # noqa: BLE001
            detail = b""
        return int(exc.code), {}, detail
    except urllib.error.URLError as exc:
        raise RuntimeError(f"语音上游连不上：{exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("语音上游超时") from exc


def sanitize_error(text: str, api_key: str = "") -> str:
    """把可能带密钥/带地址的上游报文洗成可展示的一句话。

    上游的错误体经常整段回显请求信息。密钥只应该进请求头，一旦它出现在返回报文里，
    原样抛给界面就等于把密钥写进了日志和快照。
    """
    cleaned = str(text or "").strip().replace("\n", " ")
    if api_key:
        cleaned = cleaned.replace(api_key, "***")
    if len(cleaned) > 300:
        cleaned = cleaned[:300] + "…"
    return cleaned or "上游未给出原因"


def parse_json_object(raw: bytes | str) -> tuple[dict, str]:
    """解析上游 JSON → (对象, 错误)。失败时给出可展示的中文原因，不抛异常。"""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return {}, f"上游响应不是 UTF-8 文本：{exc}"
    text = str(raw or "").strip()
    if not text:
        return {}, "上游返回空响应"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"上游响应不是合法 JSON（{exc.msg}）：{sanitize_error(text[:120])}"
    if not isinstance(parsed, dict):
        return {}, f"上游响应结构不是对象（拿到的是 {type(parsed).__name__}）"
    return parsed, ""
