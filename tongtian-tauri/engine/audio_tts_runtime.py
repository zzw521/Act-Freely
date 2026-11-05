"""回复朗读（TTS）链路（缺陷 D-11 / 会话 #160，可离线半边）

朗读不是「把整段回答丢给合成接口」。三个约束决定了这一层的形状：

1. **要念的是话，不是排版**。Markdown 的 `#`、代码块、表格、URL、工具调用 JSON 念出来
   只是噪声，而且代码块里全是符号，合成质量最差。`prepare_read_aloud()` 先把这些去掉。
2. **要能被打断**。整篇一次合成 = 用户说「停」的时候，音频已经在路上了。这里按句凑段，
   交出的是一份**分段播放计划**，播放侧可以只播到第 k 段（与 `audio_barge_in` 对接）。
3. **隐私模式不上传文本**。和 ASR 同一条硬门：待念的文本本身就是用户内容，
   送出本机与上传音频没有区别。

诚实边界：真机播放、各家合成端点的真实报文、音色与韵律质量**均未验收**；
这里的假上游只证明分支、上限、原因与隐私门成立。
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Callable

import audio_adapters as aa


#: 念给用户的文本里不允许出现的块（代码、表格、frontmatter、行内公式）
_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.M)
_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$", re.M)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s*([*+-]|\d+\.)\s+", re.M)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_)")
_LINK = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")

#: 句子边界：中日文句号/问号/感叹号/换行都算断点，逗号不断（断了韵律更碎）
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?\n；])")

#: 完全没有可念内容时的说明（不是失败：朗读本来就没东西可念）
EMPTY_SPEECH_REASON = "本轮没有可朗读的正文：回答里只有代码、表格或链接，去掉之后不剩可念的内容"


@dataclass
class TtsResult:
    ok: bool
    adapter: str = ""
    reason: str = ""
    notes: list[str] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)
    spoken_chars: int = 0
    uploads: bool = False
    http_status: int = 0

    def to_dict(self) -> dict:
        out = {
            "ok": self.ok,
            "adapter": self.adapter,
            "uploadsAudio": self.uploads,
            "notes": list(self.notes),
            "spokenChars": self.spoken_chars,
        }
        if self.ok:
            out["segments"] = list(self.segments)
        else:
            out["error"] = self.reason or "合成未成功，且上游没有给出原因"
        if self.http_status:
            out["upstreamStatus"] = self.http_status
        return out


def prepare_read_aloud(text: str, *, limit: int = aa.MAX_TTS_TEXT_CHARS,
                       target: int = aa.TTS_CHUNK_TARGET_CHARS) -> tuple[list[str], list[str]]:
    """回答原文 → (可念的分段列表, 说明列表)。

    去掉的是排版，不是事实：删掉的每一类都留一条说明，念到哪里、少了什么，播放侧与
    用户都能对得上。分段只按句子边界凑到 `target` 字左右，绝不把一句话劈成两半 ——
    半句念出来像卡带，比少念一段更糟。
    """
    notes: list[str] = []
    raw = str(text or "")
    if not raw.strip():
        return [], [EMPTY_SPEECH_REASON]

    body = raw
    fences = len(_FENCE.findall(body))
    if fences:
        notes.append(f"已省略 {fences} 处代码块（代码不适合朗读）")
    body = _FENCE.sub("", body)
    if _TABLE_SEP.search(body) or _TABLE_ROW.search(body):
        tables = len(_TABLE_ROW.findall(body))
        notes.append(f"已省略 {tables} 行表格（表格按行朗读读不出结构）")
        body = _TABLE_ROW.sub("", body)
    images = len(_IMAGE.findall(body))
    if images:
        notes.append(f"已省略 {images} 处图片引用")
    body = _IMAGE.sub("", body)
    links_named = len(_LINK.findall(body))
    if links_named:
        notes.append(f"{links_named} 处链接只念标题、不念地址")
    body = _LINK.sub(r"\1", body)
    urls = len(_URL.findall(body))
    if urls:
        notes.append(f"已省略 {urls} 个裸链接地址")
    body = _URL.sub("", body)
    inline = len(_INLINE_CODE.findall(body))
    if inline:
        notes.append(f"{inline} 处行内代码保留文字、去掉反引号")
    body = _INLINE_CODE.sub(r"\1", body)
    body = _HTML_TAG.sub("", body)
    body = _HEADING.sub("", body)
    body = _BULLET.sub("", body)
    body = _EMPHASIS.sub("", body)

    collapsed = re.sub(r"[ \t]+", " ", body)
    collapsed = re.sub(r"\n{2,}", "\n", collapsed).strip()
    if not collapsed:
        return [], [EMPTY_SPEECH_REASON]
    if len(collapsed) > limit:
        notes.append(
            f"待朗读文本超出单轮上限 {limit} 字，只朗读前 {limit} 字"
            f"（本轮回答共 {len(collapsed)} 字，其余请看着读）")
        collapsed = collapsed[:limit]

    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(collapsed) if s and s.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > target and not current:
            # 一句本身就很长：只在逗号/分号处再切，切不开就整句交给上游，
            # 不做机械定长切割（那会把词切碎）
            pieces = [p.strip() for p in re.split(r"(?<=[，,;；])", sentence) if p.strip()]
            if sum(len(p) for p in pieces) != len(sentence) or len(pieces) > 1:
                buffer = ""
                for piece in pieces:
                    if len(buffer) + len(piece) > target and buffer:
                        chunks.append(buffer)
                        buffer = piece
                    else:
                        buffer += piece
                if buffer:
                    chunks.append(buffer)
                continue
        if len(current) + len(sentence) + 1 > target and current:
            chunks.append(current)
            current = sentence
        else:
            current = (current + sentence) if current else sentence
    if current:
        chunks.append(current)
    return chunks, notes


def build_speech_request(endpoint: aa.CloudEndpoint, text: str,
                         *, response_format: str = "mp3") -> tuple[str, str, dict, bytes]:
    """构造一次合成请求 → (方法, 路径, 请求头, 请求体)。密钥只进请求头。"""
    body = {
        "model": endpoint.model,
        "input": text,
        "voice": endpoint.voice or "alloy",
        "response_format": response_format,
    }
    headers = {
        "Authorization": f"Bearer {endpoint.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/octet-stream",
    }
    return "POST", "/audio/speech", headers, json.dumps(body, ensure_ascii=False).encode("utf-8")


def synthesize(text: str, *, endpoint: aa.CloudEndpoint | None,
               transport: aa.Transport | None = None,
               adapter_id: str = "openai_compatible",
               privacy_mode: bool = False,
               response_format: str = "mp3",
               max_segments: int = 12) -> TtsResult:
    """按段合成。任一段失败就停下并说明是第几段失败 —— 前面的段已经可以播了。"""
    notes: list[str] = []
    if adapter_id == "host_player":
        # 系统朗读这条通道根本不经过这里：引擎只准备要念的文本分段，播放由桌面端完成。
        # 如果让它走进合成，就等于「桌面端说自己播」而实际把文本上传了出去。
        return TtsResult(
            ok=False, adapter=adapter_id, notes=notes,
            reason="系统朗读不在本层合成：请用 run_tts（它会直接返回文本分段）或 prepare_read_aloud",
        )
    if privacy_mode:
        return TtsResult(
            ok=False, adapter=adapter_id, notes=notes,
            reason="隐私模式已开启：待朗读文本不送出本机，本轮改由系统朗读或看着读",
        )
    if transport is None:
        return TtsResult(
            ok=False, adapter=adapter_id, notes=notes,
            reason="合成上游未接入：本层要求由调用方注入 transport，避免在自测里发真实请求",
        )
    if endpoint is None:
        return TtsResult(
            ok=False, adapter=adapter_id, notes=notes,
            reason=(
                "没有可用的朗读通道：本层只会打 OpenAI 兼容的 /audio/speech，"
                "系统朗读需要由桌面端播放（真机播放未验收）"
            ),
        )
    chunks, prep_notes = prepare_read_aloud(text)
    notes.extend(prep_notes)
    if not chunks:
        return TtsResult(ok=False, adapter=adapter_id, notes=notes,
                         reason=EMPTY_SPEECH_REASON)
    if len(chunks) > max_segments:
        notes.append(
            f"分段数 {len(chunks)} 超过单轮合成上限 {max_segments} 段，"
            f"只合成前 {max_segments} 段，其余看着读")
        chunks = chunks[:max_segments]

    segments: list[dict] = []
    spoken = 0
    for index, chunk in enumerate(chunks):
        method, path, headers, body = build_speech_request(
            endpoint, chunk, response_format=response_format)
        url = f"{endpoint.base_url}{path}"
        audio_b64 = ""
        status = 0
        try:
            status, resp_headers, resp_body = transport(
                method, url, headers, body, endpoint.timeout_seconds)
        except RuntimeError as exc:
            notes.append(f"第 {index + 1} 段合成时上游不可达：{exc}")
            break
        if status != 200:
            reason = (
                f"第 {index + 1} 段合成失败：上游返回 {status}，"
                f"{aa.sanitize_error(resp_body.decode('utf-8', 'replace'), endpoint.api_key)}"
            )
            if segments:
                notes.append(reason + "（前面各段已合成，可以先播到这里）")
                break
            return TtsResult(ok=False, adapter=adapter_id, notes=notes,
                             reason=reason, http_status=status)
        # OpenAI 兼容端点直接回二进制音频；有的网关包一层 {"audio_b64": ...}；
        # 还有的网关把错误页当 200 回（HTML）。后两种都要认出来，否则交出去的
        # 「音频」播不出声音，用户只会觉得按钮按了没反应。
        if resp_body[:1] == b"<":
            reason = (
                f"第 {index + 1} 段上游回的是 HTML 而不是音频（多为网关错误页）："
                f"{aa.sanitize_error(resp_body[:200].decode('utf-8', 'replace'), endpoint.api_key)}"
            )
            if segments:
                notes.append(reason + "（前面各段已合成，可以先播到这里）")
                break
            return TtsResult(ok=False, adapter=adapter_id, notes=notes,
                             reason=reason, http_status=status)
        if resp_body[:1] == b"{" or (resp_body[:1] == b"[" ):
            parsed, parse_err = aa.parse_json_object(resp_body)
            candidate = str(parsed.get("audio_b64") or parsed.get("data") or "") if not parse_err else ""
            if candidate:
                audio_b64 = candidate
            else:
                notes.append(f"第 {index + 1} 段上游回的是 JSON 而不是音频：{parse_err or '没有音频字段'}")
                if not segments:
                    return TtsResult(ok=False, adapter=adapter_id, notes=notes,
                                     reason=f"第 {index + 1} 段上游没有返回音频数据",
                                     http_status=status)
                break
        else:
            audio_b64 = base64.b64encode(resp_body).decode("ascii")
        if not audio_b64:
            notes.append(f"第 {index + 1} 段合成的音频为空")
            break
        spoken += len(chunk)
        segments.append({
            "index": index,
            "text": chunk,
            "audio": audio_b64,
            "format": response_format,
            "bytes": len(resp_body),
        })
    if not segments:
        return TtsResult(ok=False, adapter=adapter_id, notes=notes,
                         reason="一段都没有合成成功：" + (notes[-1] if notes else "上游未给出原因"))
    return TtsResult(
        ok=True, adapter=adapter_id, notes=notes, segments=segments,
        spoken_chars=spoken, uploads=True, http_status=200,
    )


def run_tts(payload: dict | None, *, runtime: dict | None = None,
            model_config: dict | None = None, privacy_mode: bool = False,
            transport: aa.Transport | None = None, caps: dict | None = None,
            env: dict | None = None) -> TtsResult:
    """端点入口：能力表 → 选型 → 隐私门 → 分段合成（或交给桌面端本机播放）。"""
    body = payload if isinstance(payload, dict) else {}
    caps = caps if isinstance(caps, dict) else {}
    notes: list[str] = []
    text = str(body.get("text") or "")

    lane = (runtime or {}).get("audio") if isinstance((runtime or {}).get("audio"), dict) else {}
    tts_cfg = lane.get("tts") if isinstance(lane.get("tts"), dict) else {}
    if str(tts_cfg.get("enabled") or "").strip().lower() in ("off", "false", "0"):
        return TtsResult(ok=False, reason="朗读已在设置里关闭")

    preferred = str(tts_cfg.get("adapter") or body.get("adapter") or "").strip()
    selection = aa.select_adapter("tts", caps, privacy_mode=privacy_mode, preferred=preferred)
    notes.extend(selection.reasons)
    for adapter_id, reason in selection.skipped:
        notes.append(f"{adapter_id}：{reason}")
    if not selection.ok():
        return TtsResult(ok=False, notes=notes,
                         reason="；".join(selection.reasons) or aa.NO_ADAPTER_NOTE)

    if selection.chosen == "host_player":
        # 桌面端自己播：引擎只负责把「该念什么」算清楚交下去，一个字也不上传
        chunks, prep_notes = prepare_read_aloud(text)
        notes.extend(prep_notes)
        if not chunks:
            return TtsResult(ok=False, adapter=selection.chosen, notes=notes,
                             reason=EMPTY_SPEECH_REASON)
        return TtsResult(
            ok=True, adapter=selection.chosen, notes=notes, uploads=False,
            spoken_chars=sum(len(c) for c in chunks),
            segments=[{"index": i, "text": c} for i, c in enumerate(chunks)],
        )

    endpoint, cfg_notes = aa.resolve_cloud(runtime, model_config, env, kind="tts")
    notes.extend(cfg_notes)
    result = synthesize(
        text, endpoint=endpoint, transport=transport, adapter_id=selection.chosen,
        privacy_mode=privacy_mode,
        response_format=str(body.get("format") or "mp3"),
        max_segments=int(tts_cfg.get("maxSegments") or 12))
    result.notes = notes + result.notes
    return result
