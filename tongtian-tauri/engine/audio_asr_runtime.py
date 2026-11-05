"""语音转文字（ASR）链路（缺陷 D-11 / 会话 #160，可离线半边）

一次「按住说话」在本层的完整形状：

    录音字节（由 Rust 或浏览器交进来）
      → validate_audio()          格式/大小/空内容，任一不过就带原因返回
      → select_adapter("asr")     能力表 + 隐私门选型，每条被跳过的通道都留原因
      → 构造上游请求 → transport() 注入点；自测喂假上游，真网络只在接线侧
      → 解析转写文本 → 结果

D-11 的三条验收项在这一层各自落点：

* **「语音输入可插入输入框」** —— 本层交出 `text` 与 `insertAt`/`replace` 语义所需的
  字段（见 `AsrResult.to_dict()`），插进输入框是渲染层的事，不在本层假装完成。
* **「失败有原因」** —— 每条出口都带 `reason`；不存在「返回空字符串且什么都不说」的通路。
* **「隐私模式下不上传音频」** —— `select_adapter` 里硬拒 uploads 通道，
  且 `transcribe()` 自己再查一次：选型正确不代表请求构造正确，重复一次比漏一次好。

诚实边界（没跑过真机就不写已实现）：
真麦克风采到的音频、各家厂商端点的真实报文、中文口音与噪声下的识别质量，
本仓库内**全部未验收**。这里的假上游只证明「本层的分支、上限、原因、隐私门成立」，
不证明任何一次真实识别成功过。
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from typing import Callable

import audio_adapters as aa


#: 回显给界面的转写文本上限：一段口述到不了这个长度，超了说明上游在吐别的东西
MAX_TRANSCRIPT_CHARS = 8000

#: 上游认这个字段名当「本轮说话的语言」；留空则让上游自己判
ASR_LANGUAGE_HINTS = ("zh", "en", "ja", "ko")


@dataclass
class AsrResult:
    """一次识别的结果。`ok` 为假时 reason 必填，notes 里是选型阶段就产生的说明。

    `requires_host` 是第三种出口，既不是成功也不是「失败了」：选到的是本机通道，
    而识别这个动作本身在桌面端。界面据此去调 `voice:recognize`，而不是显示一句报错。
    """

    ok: bool
    text: str = ""
    adapter: str = ""
    reason: str = ""
    notes: list[str] = field(default_factory=list)
    uploads: bool = False
    http_status: int = 0
    attempts: int = 0
    requires_host: bool = False

    def to_dict(self) -> dict:
        out = {
            "ok": self.ok,
            "adapter": self.adapter,
            "uploadsAudio": self.uploads,
            "notes": list(self.notes),
            "attempts": self.attempts,
        }
        if self.requires_host:
            # 不冒充识别结果，也不报成失败：把「下一步该做什么」交出去
            out["requiresHost"] = True
            out["hostAction"] = {"method": "voice:recognize", "adapter": self.adapter}
            out["reason"] = self.reason
        if self.ok:
            # 渲染层据此把文本落到输入框光标处；没识别成功就不给空串冒充「识别到空白」
            out["text"] = self.text
            out["insert"] = {"text": self.text, "position": "cursor"}
        elif not self.requires_host:
            out["error"] = self.reason or "识别未成功，且上游没有给出原因"
        if self.http_status:
            out["upstreamStatus"] = self.http_status
        return out


def encode_multipart(fields: dict, file_field: str, filename: str,
                     mime: str, content: bytes) -> tuple[str, bytes]:
    """OpenAI 兼容的 multipart/form-data 编码（只用标准库）。

    `fields` 里的 None 一律跳过而不是写成字符串 "None" —— 上游会把那种值当合法语言码
    收下去，然后在识别阶段报一句没人看得懂的错。
    """
    boundary = f"----ttAsrBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in (fields or {}).items():
        if value is None or str(value) == "":
            continue
        chunks.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
             f"{value}\r\n").encode("utf-8")
        )
    chunks.append(
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
         f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


def _mime_of(fmt: str) -> str:
    return {
        "wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg",
        "opus": "audio/opus", "m4a": "audio/mp4", "aac": "audio/aac",
        "flac": "audio/flac", "webm": "audio/webm",
        "pcm16": "application/octet-stream",
    }.get(fmt, "application/octet-stream")


def build_transcription_request(endpoint: aa.CloudEndpoint, fmt: str,
                                content: bytes, *, language: str = "",
                                prompt: str = "") -> tuple[str, str, dict, bytes]:
    """构造一次识别请求 → (方法, 路径, 请求头, 请求体)。密钥只进请求头。"""
    fields = {
        "model": endpoint.model,
        "file": None,
        "language": language if language in ASR_LANGUAGE_HINTS else None,
        "prompt": prompt[:1000] or None,
    }
    if language and language not in ASR_LANGUAGE_HINTS:
        fields["language"] = language
    content_type, body = encode_multipart(
        fields, "file", f"speech.{fmt}", _mime_of(fmt), content)
    headers = {
        "Authorization": f"Bearer {endpoint.api_key}",
        "Content-Type": content_type,
        "Accept": "application/json",
    }
    return "POST", "/audio/transcriptions", headers, body


def _decode_audio(audio: dict) -> tuple[bytes, str]:
    fmt, data, err = aa.validate_audio(audio)
    if err:
        return b"", err
    try:
        return base64.b64decode(data, validate=True), ""
    except Exception as exc:  # noqa: BLE001
        return b"", f"音频 base64 解码失败：{exc}"


def _transcript_of(parsed: dict) -> tuple[str, str]:
    """从上游响应里取转写文本 → (文本, 错误)。

    OpenAI 兼容端点是 `{"text": "..."}`；某些自建网关会把整段结果塞进 `choices[0].message.content`
    或 `segments[]`。这里只认这三种已知形状，别的形状如实说「读不懂」而不是猜一个字段。
    """
    if "error" in parsed and "text" not in parsed:
        err = parsed.get("error")
        detail = err.get("message") if isinstance(err, dict) else str(err)
        return "", f"上游拒绝本次识别：{detail or '未给出原因'}"
    text = parsed.get("text")
    if isinstance(text, str):
        return text.strip(), ""
    choices = parsed.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content") or first.get("text")
        if isinstance(content, str) and content.strip():
            return content.strip(), ""
    segments = parsed.get("segments")
    if isinstance(segments, list) and segments:
        parts = [str(s.get("text") or "") for s in segments if isinstance(s, dict)]
        joined = " ".join(p for p in parts if p).strip()
        if joined:
            return joined, ""
    return "", (
        "上游响应里没有可懂的转写文本字段"
        f"（拿到的是 {sorted(parsed.keys())[:8]}）"
    )


#: 走到本机通道时给界面的「下一步做什么」，按通道分开写：
#: 一句「识别失败」会让人以为麦克风坏了，而事实是这一层把动作交回桌面端。
HOST_ASR_HINTS = {
    "host_system": (
        "系统识别要由桌面端执行：调 voice:recognize（engine=system）把音频交给本机识别器，"
        "再用同一请求带 hostResult 提交一次"
    ),
    "local_whisper_cpp": (
        "本机模型识别要由桌面端执行：调 voice:recognize（engine=whisper 或 sherpa，"
        "需 TT_ASR_BIN 指向可执行文件且模型文件存在），再用同一请求带 hostResult 提交一次"
    ),
}


def transcribe(audio: dict, *, endpoint: aa.CloudEndpoint | None,
               transport: aa.Transport | None = None,
               adapter_id: str = "openai_compatible",
               privacy_mode: bool = False,
               allow_upload: bool | None = None,
               language: str = "", prompt: str = "") -> AsrResult:
    """跑一次识别。选型、上限、隐私门、重试与原因都在这里收口。"""
    notes: list[str] = []
    fmt, data_b64, err = aa.validate_audio(audio)
    if err:
        return AsrResult(ok=False, adapter=adapter_id, reason=err, notes=notes)
    duration = audio.get("durationMs")
    if isinstance(duration, (int, float)) and duration <= 0:
        return AsrResult(
            ok=False, adapter=adapter_id, notes=notes,
            reason="录音时长为 0：多半是按下即松开，没有真的采到声音",
        )
    if privacy_mode:
        # 第二道门：即便调用方已经选好了本地通道，这里也不允许带云端端点进来
        if adapter_id == "openai_compatible" or (allow_upload is False and endpoint is not None):
            return AsrResult(
                ok=False, adapter=adapter_id, notes=notes,
                reason="隐私模式已开启：本次不上传音频，且没有可用的本机识别通道",
            )
    if endpoint is None:
        return AsrResult(
            ok=False, adapter=adapter_id, notes=notes,
            reason=(
                "没有可用的识别通道：本层只会打 OpenAI 兼容的 /audio/transcriptions，"
                "本机识别（whisper.cpp / 系统识别）需要由桌面端探测并把结果回传"
            ),
        )
    if transport is None:
        return AsrResult(
            ok=False, adapter=adapter_id, notes=notes,
            reason="识别上游未接入：本层要求由调用方注入 transport，避免在自测里发真实请求",
        )
    try:
        content = base64.b64decode(data_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        return AsrResult(ok=False, adapter=adapter_id, notes=notes,
                         reason=f"音频 base64 解码失败：{exc}")

    method, path, headers, body = build_transcription_request(
        endpoint, fmt, content, language=language, prompt=prompt)
    url = f"{endpoint.base_url}{path}"
    last_reason = ""
    last_status = 0
    for attempt in range(1, aa.MAX_ASR_RETRIES + 2):
        try:
            status, _resp_headers, resp_body = transport(method, url, headers, body,
                                                          endpoint.timeout_seconds)
        except RuntimeError as exc:
            last_reason, last_status = str(exc), 0
            if attempt <= aa.MAX_ASR_RETRIES:
                notes.append(f"识别上游不可达，重试第 {attempt} 次")
                continue
            break
        last_status = status
        if status == 200:
            parsed, parse_err = aa.parse_json_object(resp_body)
            if parse_err:
                return AsrResult(ok=False, adapter=adapter_id, notes=notes,
                                 reason=parse_err, http_status=status, attempts=attempt)
            text, text_err = _transcript_of(parsed)
            if text_err:
                return AsrResult(ok=False, adapter=adapter_id, notes=notes,
                                 reason=aa.sanitize_error(text_err, endpoint.api_key),
                                 http_status=status, attempts=attempt)
            if not text:
                return AsrResult(
                    ok=False, adapter=adapter_id, notes=notes, http_status=status, attempts=attempt,
                    reason="识别结果为空：这段音频里可能只有环境噪声或没人说话",
                )
            if len(text) > MAX_TRANSCRIPT_CHARS:
                notes.append(
                    f"转写文本超出 {MAX_TRANSCRIPT_CHARS} 字，已截断后交回"
                    f"（上游回报共 {len(text)} 字）")
                text = text[:MAX_TRANSCRIPT_CHARS]
            return AsrResult(ok=True, text=text, adapter=adapter_id, notes=notes,
                             uploads=True, http_status=status, attempts=attempt)
        last_reason = (
            f"识别上游返回 {status}：{aa.sanitize_error(resp_body.decode('utf-8', 'replace'), endpoint.api_key)}"
        )
        if status in (429, 500, 502, 503, 504) and attempt <= aa.MAX_ASR_RETRIES:
            notes.append(f"识别上游 {status}，重试第 {attempt} 次")
            continue
        break
    return AsrResult(ok=False, adapter=adapter_id, notes=notes, reason=last_reason or
                     "识别上游未成功且没有给出原因", http_status=last_status,
                     attempts=aa.MAX_ASR_RETRIES + 1)


def accept_host_result(raw: dict | None, *, caps: dict | None = None) -> AsrResult:
    """桌面端已经在本机识别完，把结果交回引擎时使用（不上传任何音频）。

    这条路径是隐私模式下的主力：只有**声明的那条本机通道在能力表里为真**才认，
    否则如实说明没探测到该通道，而不是把一段来源不明的文本当成「用户说的话」塞进消息里。
    `adapter` 缺省按 `host_system` 处理；填了本机之外的名字会留一条说明并按缺省走，
    因为界面显示的通道名必须能在登记表里找到。
    """
    notes: list[str] = []
    caps = caps if isinstance(caps, dict) else {}
    block = raw if isinstance(raw, dict) else {}
    text = str(block.get("text") or "").strip()
    err = str(block.get("error") or "").strip()
    adapter = str(block.get("adapter") or "").strip()
    if adapter and not aa.is_local_asr(adapter):
        notes.append(f"桌面端声明的通道「{adapter}」不是本机通道，已按系统识别采纳")
        adapter = ""
    adapter = adapter or "host_system"
    capability_key = (aa.find_adapter("asr", adapter) or aa.ASR_ADAPTERS[0]).capability_key
    if caps.get(capability_key) is not True:
        return AsrResult(
            ok=False, adapter=adapter, notes=notes,
            reason=(f"未探测到本机识别能力（{capability_key}），"
                    "本层不接受未经确认的桌面端识别结果"),
        )
    if err:
        return AsrResult(ok=False, adapter=adapter, notes=notes,
                         reason=f"桌面端识别失败：{err}")
    if not text:
        return AsrResult(ok=False, adapter=adapter, notes=notes,
                         reason="桌面端识别结果为空：可能没有听到内容，或被用户取消")
    if len(text) > MAX_TRANSCRIPT_CHARS:
        notes.append(f"桌面端回传的文本超出 {MAX_TRANSCRIPT_CHARS} 字，已截断后使用")
        text = text[:MAX_TRANSCRIPT_CHARS]
    return AsrResult(ok=True, text=text, adapter=adapter, notes=notes, uploads=False)


def run_asr(payload: dict | None, *, runtime: dict | None = None,
            model_config: dict | None = None, privacy_mode: bool = False,
            transport: aa.Transport | None = None, caps: dict | None = None,
            env: dict | None = None) -> AsrResult:
    """端点入口：能力表 → 选型 → 隐私门 → 识别（或采纳桌面端已识别的结果）。

    选型与识别的原因合并成一份 notes，由调用方原样摊给用户；这里不做「失败了就退回
    空文本」那种看起来像成功的处理。
    """
    body = payload if isinstance(payload, dict) else {}
    caps = caps if isinstance(caps, dict) else {}
    notes: list[str] = []

    lane = (runtime or {}).get("audio") if isinstance((runtime or {}).get("audio"), dict) else {}
    asr_cfg = lane.get("asr") if isinstance(lane.get("asr"), dict) else {}
    if str(asr_cfg.get("enabled") or "").strip().lower() in ("off", "false", "0"):
        return AsrResult(ok=False, reason="语音输入已在设置里关闭")

    # 桌面端已经把音频识别完了：这条最省，也不上传任何东西
    if isinstance(body.get("hostResult"), dict):
        return accept_host_result(body["hostResult"], caps=caps)

    preferred = str(asr_cfg.get("adapter") or body.get("adapter") or "").strip()
    if not preferred and str(body.get("localOnly") or "").strip().lower() in ("1", "true", "on"):
        preferred = "local_whisper_cpp"

    # 没点名时先探一次云端：配好了就**沿用「默认走云端」的既有行为**（本机识别器普遍比
    # 云端 whisper 差一档，无声地把已配好云端的人换到本机通道是一次质量倒退）；
    # 没配好、或开了隐私模式，才让本机通道按登记表顺序顶上。
    # 点名云端不代表一定要用：`select_adapter` 仍按能力表与隐私门判，够不着就往下退。
    endpoint: aa.CloudEndpoint | None = None
    cloud_notes: list[str] = []
    if not preferred and not privacy_mode:
        endpoint, cloud_notes = aa.resolve_cloud(runtime, model_config, env, kind="asr")
        if endpoint is not None:
            preferred = "openai_compatible"

    selection = aa.select_adapter(
        "asr", caps, privacy_mode=privacy_mode, preferred=preferred)
    notes.extend(selection.reasons)
    for adapter_id, reason in selection.skipped:
        notes.append(f"{adapter_id}：{reason}")
    if not selection.ok():
        notes.extend(cloud_notes)
        # 用户点名本机通道却没通道可用时，把「要怎么接上」一并说明：
        # 只回一句「没有可用通道」会让人以为设置没生效，而事实是这一层还没接桌面端。
        if preferred in aa.LOCAL_ASR_ADAPTER_IDS:
            notes.append(
                "本机识别通道需要桌面端探测 whisper.cpp / 系统识别，并把识别结果经 "
                "audio.hostResult 回传；真机麦克风与本地模型均未验收"
            )
        return AsrResult(ok=False, notes=notes,
                         reason="；".join(selection.reasons) or aa.NO_ADAPTER_NOTE)

    if aa.is_local_asr(selection.chosen):
        # 选到本机通道不等于引擎能识别：这里一次上传都不做，也不返回一个空 text
        # 冒充「识别到空白」，而是把要做的事、要带的字段原样交回调用方。
        _fmt, _data, err = aa.validate_audio(body.get("audio") or {})
        if err:
            return AsrResult(ok=False, adapter=selection.chosen, notes=notes, reason=err)
        notes.extend(cloud_notes)
        notes.append(HOST_ASR_HINTS.get(selection.chosen, HOST_ASR_HINTS["host_system"]))
        notes.append("真机麦克风与本仓库内的本机识别结果均未验收")
        return AsrResult(
            ok=False, adapter=selection.chosen, notes=notes, requires_host=True,
            reason="本轮识别由桌面端在本机完成，引擎不代替它产出文本",
        )

    if endpoint is None:
        endpoint, cfg_notes = aa.resolve_cloud(runtime, model_config, env, kind="asr")
        notes.extend(cfg_notes)
    else:
        notes.extend(cloud_notes)
    result = transcribe(
        body.get("audio") or {}, endpoint=endpoint, transport=transport,
        adapter_id=selection.chosen, privacy_mode=privacy_mode,
        language=str(body.get("language") or ""), prompt=str(body.get("prompt") or ""))
    result.notes = notes + result.notes
    return result
