#!/usr/bin/env python3
"""
语音链路自测（D-11 / 卡 #160，可离线半边）

覆盖面：适配器登记与选型（隐私门）、上游地址解析、音频载荷校验、ASR 与 TTS 的
请求构造 + 假上游应答、朗读文本的排版剥离与分段、打断状态机的每一条转移。

**这里证明的只有分支、上限、原因与隐私门**。真机麦克风、各家厂商端点的真实报文、
打断时序与音色质量都**未验收**：假上游是刻意构造的应答形状，它不会比真端点更宽容。

离线运行，只用标准库。
用法: ./vendor/python/python.exe -X utf8 tests/integration/engine_audio_selftest.py
"""

import base64
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

import audio_adapters as aa
import audio_asr_runtime as asr
import audio_barge_in as bi
import audio_tts_runtime as tts

FAILURES: list[str] = []

WAV_BYTES = b"RIFF" + b"\x24\x00\x00\x00" + b"WAVEfmt " + bytes(range(48))
WAV_B64 = base64.b64encode(WAV_BYTES).decode("ascii")
FAKE_KEY = "sk-SECRET-DO-NOT-ECHO"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def make_transport(replies: list):
    """假上游：按脚本一条条回；抛 RuntimeError 模拟「连不上」（与真实 transport 同约定）"""
    state = {"calls": [], "index": 0}

    def transport(method, url, headers, body, timeout):
        state["calls"].append({
            "method": method, "url": url, "headers": dict(headers or {}),
            "body": body, "timeout": timeout,
        })
        reply = replies[min(state["index"], len(replies) - 1)]
        state["index"] += 1
        if isinstance(reply, Exception):
            raise reply
        status, payload = reply
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return status, {"content-type": "application/json"}, raw

    return transport, state


def endpoint(kind: str = "asr") -> aa.CloudEndpoint:
    return aa.CloudEndpoint(base_url="https://upstream.invalid/v1", api_key=FAKE_KEY,
                            model="whisper-1" if kind == "asr" else "tts-1",
                            timeout_seconds=7.0, voice="nova")


def audio_payload(**extra) -> dict:
    body = {"format": "wav", "data": WAV_B64, "durationMs": 3200}
    body.update(extra)
    return body


# ── 1. 适配器登记与选型 ────────────────────────────────────────

def registry_checks() -> None:
    print("--- 1. 通道登记与隐私门 ---")
    asr_ids = [a.id for a in aa.adapters_for("asr")]
    tts_ids = [a.id for a in aa.adapters_for("tts")]
    check("ASR 通道登记齐（含显式关闭）",
          asr_ids == ["host_system", "local_whisper_cpp", "openai_compatible", "none"], str(asr_ids))
    check("TTS 通道登记齐（含显式关闭）",
          tts_ids == ["host_player", "openai_compatible", "none"], str(tts_ids))
    check("未知种类直接报错而不是返回空表",
          _raises(lambda: aa.adapters_for("reverb")), "adapters_for 未抛异常")
    check("按 id 找得到、找不到的返回 None",
          aa.find_adapter("asr", "openai_compatible") is not None
          and aa.find_adapter("asr", "shazam") is None)

    uploaders = {a.id for a in aa.adapters_for("asr") if a.uploads_audio}
    check("ASR 里只有云端通道标了会上传", uploaders == {"openai_compatible"}, str(uploaders))
    uploaders = {a.id for a in aa.adapters_for("tts") if a.uploads_audio}
    check("TTS 里只有云端通道标了会上传", uploaders == {"openai_compatible"}, str(uploaders))
    described = aa.find_adapter("asr", "openai_compatible").describe()
    check("describe 用 camelCase（与 IPC 侧同一套键名）",
          set(described) == {"id", "label", "uploadsAudio", "capabilityKey", "implemented", "notes"},
          str(described))

    sel = aa.select_adapter("asr", {"cloudAsr": True})
    check("有云端能力且不隐私时选到云端识别",
          sel.ok() and sel.chosen == "openai_compatible" and sel.upload is True, sel.to_dict())
    sel = aa.select_adapter("asr", {"cloudAsr": True}, privacy_mode=True)
    check("隐私模式硬拒云端（不看模型配置、不看是否更方便）",
          not sel.ok() and sel.chosen == "", sel.to_dict())
    check("隐私模式拒的是上传，不是整条链路：原因里点得出来",
          any("隐私模式已开启，音频不送出本机" in reason for _id, reason in sel.skipped),
          str(sel.skipped))
    sel = aa.select_adapter("asr", {})
    check("没探测到能力时不选云端，并写明缺哪个前置条件",
          not sel.ok() and any("cloudAsr" in reason for _id, reason in sel.skipped), str(sel.skipped))
    sel = aa.select_adapter("asr", {}, preferred="none")
    check("登记为未接入的通道不会被选中去发请求（以显式关闭那条为证）",
          not sel.ok() and any(i == "none" and "尚未接入引擎侧" in r for i, r in sel.skipped),
          str(sel.skipped))
    # #165：host_system / local_whisper_cpp 的 implemented 由 False 改 True。
    # 改之前它们和 #160 抓到的 host_player 是同一个形状——隐私模式下一条本机通道都选不出，
    # 「隐私模式下不上传音频」这条验收实际上没有可走的通路。
    sel = aa.select_adapter("asr", {"hostAsr": True}, privacy_mode=True)
    check("隐私模式下系统识别能被选中（本机通道不再是空壳）",
          sel.chosen == "host_system" and sel.upload is False, sel.to_dict())
    sel = aa.select_adapter("asr", {"hostAsr": True, "localAsr": True})
    check("两条本机能力都在时按登记顺序取系统识别",
          sel.chosen == "host_system", sel.to_dict())
    sel = aa.select_adapter("asr", {"localAsr": True})
    check("只探测到本机模型时选到本机模型通道",
          sel.chosen == "local_whisper_cpp" and sel.upload is False, sel.to_dict())
    sel = aa.select_adapter("asr", {"cloudAsr": True}, preferred="siri")
    check("点名了不存在的通道：先说明再按默认顺序选",
          sel.chosen == "openai_compatible" and any("不存在" in r for r in sel.reasons), str(sel.reasons))
    sel = aa.select_adapter("asr", {"cloudAsr": True}, allow_upload=False)
    check("调用侧显式禁止上传时云端也不选",
          not sel.ok() and any("显式禁止上传音频" in r for _i, r in sel.skipped), str(sel.skipped))
    sel = aa.select_adapter("asr", {})
    check("一条都没有时给出兜底说明而不是空字符串",
          sel.chosen == "" and sel.reasons == [aa.NO_ADAPTER_NOTE], str(sel.reasons))

    sel = aa.select_adapter("tts", {"hostTts": True}, privacy_mode=True)
    check("隐私模式下系统朗读可用（这条不上传文本）",
          sel.chosen == "host_player" and sel.upload is False, sel.to_dict())
    sel = aa.select_adapter("tts", {})
    check("桌面端没声明朗读能力时不硬选 host_player", not sel.ok(), sel.to_dict())


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:  # noqa: BLE001
        return True
    return False


# ── 2. 上游地址解析 ────────────────────────────────────────────

def cloud_config_checks() -> None:
    print("--- 2. runtime.audio / 环境变量 / 模型端点的优先级 ---")
    runtime = {"audio": {"asr": {"baseUrl": "https://from-runtime.invalid/v1",
                                 "apiKey": "rt-key", "model": "whisper-large"}}}
    ep, notes = aa.resolve_cloud(runtime, {"baseUrl": "https://model.invalid/v1", "apiKey": "m-key"},
                                 env={}, kind="asr")
    check("runtime.audio 优先", ep is not None and ep.base_url == "https://from-runtime.invalid/v1"
          and ep.api_key == "rt-key", str(notes))
    check("模型端点没被当作语音配置覆盖 runtime", ep.model == "whisper-large", ep.model if ep else "")
    check("配置齐时不产生说明", notes == [], str(notes))

    ep, _ = aa.resolve_cloud(None, {"baseUrl": "https://model.invalid/v1/", "apiKey": "m-key"},
                             env={}, kind="asr")
    check("没有专用配置时复用模型端点", ep is not None
          and ep.base_url == "https://model.invalid/v1", ep.base_url if ep else "")
    check("地址末尾斜杠被去掉（否则拼出 //audio）",
          ep is not None and not ep.base_url.endswith("/"), ep.base_url if ep else "")

    ep, _ = aa.resolve_cloud(None, None,
                             env={"TT_ASR_BASE_URL": "https://env.invalid/v1", "TT_ASR_API_KEY": "e-key"},
                             kind="asr")
    check("环境变量在 runtime 缺、模型端点也缺时顶上",
          ep is not None and ep.api_key == "e-key", "")
    check("ASR 缺省模型名可用（whisper-1）", ep is not None and ep.model == "whisper-1",
          ep.model if ep else "")

    ep, notes = aa.resolve_cloud(None, None, env={"TT_TTS_BASE_URL": "https://t.invalid/v1"}, kind="tts")
    check("只配地址不配密钥：如实说缺密钥并给出可填位置",
          ep is None and any("未配置密钥" in n and "TT_TTS_API_KEY" in n for n in notes), str(notes))
    ep, notes = aa.resolve_cloud(None, None, env={"TT_ASR_API_KEY": "only-key"}, kind="asr")
    check("只配密钥不配地址：同样如实说明", ep is None and any("未配置地址" in n for n in notes), str(notes))
    ep, notes = aa.resolve_cloud({"audio": {"asr": {"enabled": "off", "baseUrl": "https://x.invalid/v1",
                                                    "apiKey": "k"}}}, None, env={}, kind="asr")
    check("设置里关掉时不再拼端点，并说明是被关的",
          ep is None and any("已在设置里关闭" in n for n in notes), str(notes))

    ep, _ = aa.resolve_cloud({"audio": {"tts": {"baseUrl": "https://x.invalid/v1", "apiKey": "k",
                                                "voice": "Cherry", "timeoutSeconds": "12"}}},
                             None, env={}, kind="tts")
    check("音色与超时按专用配置生效", ep is not None and ep.voice == "Cherry"
          and ep.timeout_seconds == 12.0, f"{ep.voice if ep else ''}/{ep.timeout_seconds if ep else ''}")
    ep, _ = aa.resolve_cloud({"audio": {"tts": {"baseUrl": "https://x.invalid/v1", "apiKey": "k",
                                                "timeoutSeconds": "abc"}}}, None, env={}, kind="tts")
    check("超时写成非数字时退回缺省而不是崩", ep is not None
          and ep.timeout_seconds == aa.DEFAULT_TTS_TIMEOUT_SECONDS, str(ep))
    ep, _ = aa.resolve_cloud({"audio": {"tts": {"baseUrl": "https://x.invalid/v1", "apiKey": "k"}}},
                             None, env={}, kind="tts")
    check("TTS 缺省模型名可用（tts-1）", ep is not None and ep.model == "tts-1",
          ep.model if ep else "未拼出端点")
    check("缺省音色与超时有兜底（不交给上游一个空 voice）",
          ep is not None and ep.voice == "alloy"
          and ep.timeout_seconds == aa.DEFAULT_TTS_TIMEOUT_SECONDS,
          f"{ep.voice if ep else ''}/{ep.timeout_seconds if ep else ''}")

    _ep, notes = aa.resolve_cloud({"audio": {"asr": {"baseUrl": "https://x.invalid/v1"}}},
                                  None, env={}, kind="asr")
    check("缺密钥的说明里不回显任何已配好的值", not any("sk-" in n for n in notes), str(notes))


# ── 3. 载荷校验与报文洗白 ──────────────────────────────────────

def payload_checks() -> None:
    print("--- 3. 音频载荷与错误洗白 ---")
    fmt, data, err = aa.validate_audio(None)
    check("载荷缺失时直接说缺什么", err.startswith("音频载荷缺失"), f"{fmt}/{err}")
    fmt, data, err = aa.validate_audio({"data": WAV_B64})
    check("未声明格式时列出支持的格式", "音频格式未声明" in err and "opus" in err, err)
    fmt, data, err = aa.validate_audio({"format": "amr", "data": WAV_B64})
    check("不支持的格式被拒并说明支持哪些", "不支持的音频格式 amr" in err and "flac" in err, err)
    fmt, _data, err = aa.validate_audio({"format": ".WAV", "data": WAV_B64})
    check("格式大小写与前导点能归一", fmt == "wav" and err == "", f"{fmt}/{err}")
    fmt, _data, err = aa.validate_audio({"format": "pcm_s16le", "data": WAV_B64})
    check("PCM 别名归一到 pcm16", fmt == "pcm16" and err == "", fmt)
    fmt, _data, err = aa.validate_audio({"format": "wav", "rawBytes": WAV_BYTES})
    check("测试用原始字节会转成 base64", err == "" and _data == WAV_B64, err)
    fmt, _d, err = aa.validate_audio({"format": "wav", "path": "C:/tmp/a.wav"})
    check("本层不读磁盘路径（避免越界与路径歧义）", "不读磁盘路径" in err, err)
    fmt, _d, err = aa.validate_audio({"format": "wav", "data": ""})
    check("空音频如实说是取消或没采到", "音频数据为空" in err, err)
    fmt, _d, err = aa.validate_audio({"format": "wav", "data": "A" * 100}, limit=50)
    check("超限音频被拒并写明上限", "超出上限 50" in err, err)
    # #165 口径修正：上限比的是**解码后的字节**，不是 base64 字符数。
    # 100 个 base64 字符 ≈ 75 字节：按字符比会把 80 字节的上限判成超限。
    _f, _d, err = aa.validate_audio({"format": "wav", "data": "A" * 100}, limit=80)
    check("体积上限按解码后字节判（base64 只是运输编码）", err == "", err)
    _f, _d, err = aa.validate_audio({"format": "wav", "data": "A" * 100}, limit=74)
    check("刚好越过解码后字节下界时被拒", "超出上限 74" in err, err)
    _f, _d, err = aa.validate_audio({"format": "wav", "data": "A" * 100, "durationMs": 999999})
    check("时长超限单独说一句，并给出「分段说」的建议",
          "超出单次上限" in err and "分段说" in err, err)
    _f, _d, err = aa.validate_audio({"format": "wav", "data": "A" * 100, "durationMs": 4000})
    check("4 秒录音在时长上限内不误伤", err == "", err)

    cleaned = aa.sanitize_error(f"upstream rejected key {FAKE_KEY} at host", FAKE_KEY)
    check("上游报文里的密钥被洗成 ***", FAKE_KEY not in cleaned and "***" in cleaned, cleaned)
    check("超长报文截到 300 字并加省略号",
          len(aa.sanitize_error("长" * 400)) == 301, str(len(aa.sanitize_error("长" * 400))))
    check("空报文不返回空字符串（界面得有一句话可显示）",
          aa.sanitize_error("") == "上游未给出原因", aa.sanitize_error(""))

    parsed, err = aa.parse_json_object(b'{"text":"hi"}')
    check("JSON 对象解析成功", parsed == {"text": "hi"} and err == "", err)
    parsed, err = aa.parse_json_object(b"")
    check("空响应说空响应", parsed == {} and "空响应" in err, err)
    parsed, err = aa.parse_json_object("not json")
    check("非 JSON 响应给出中文原因", parsed == {} and "不是合法 JSON" in err, err)
    parsed, err = aa.parse_json_object(b"[1,2]")
    check("顶层不是对象时如实说结构", parsed == {} and "不是对象" in err, err)
    parsed, err = aa.parse_json_object(b"\xff\xfe bad")
    check("非 UTF-8 响应不抛异常", isinstance(parsed, dict), str(err))


# ── 4. ASR：请求构造 + 假上游 ──────────────────────────────────

def asr_request_checks() -> None:
    print("--- 4. ASR 请求构造 ---")
    method, path, headers, body = asr.build_transcription_request(
        endpoint(), "wav", WAV_BYTES, language="zh", prompt="术语表：通天")
    check("方法与路径按 OpenAI 兼容口径", method == "POST" and path == "/audio/transcriptions", path)
    check("密钥只进请求头", headers["Authorization"] == f"Bearer {FAKE_KEY}"
          and FAKE_KEY.encode() not in body, list(headers))
    check("multipart 的 boundary 与 Content-Type 一致",
          headers["Content-Type"].split("boundary=")[1].encode() in body, headers["Content-Type"])
    check("音频作为 file 字段带文件名与 MIME",
          b'name="file"; filename="speech.wav"' in body and b"Content-Type: audio/wav" in body,
          body[:200].decode("utf-8", "replace"))
    check("multipart 以结束边界收尾", body.endswith(b"--\r\n") and b"--\r\n" in body[-40:], body[-40:])
    check("模型名进了表单", b'name="model"' in body and b"whisper-1" in body,
          body[:200].decode("utf-8", "replace"))
    check("语言提示与续识别提示都进表单",
          b'name="language"' in body and b"zh" in body and "术语表".encode() in body,
          body[:300].decode("utf-8", "replace"))
    _m, _p, _h, bare = asr.build_transcription_request(endpoint(), "mp3", WAV_BYTES)
    check("未声明语言时不把 None 写成字符串（上游会当成合法语言码）",
          b'name="language"' not in bare, bare[:300].decode("utf-8", "replace"))
    check("prompt 缺省时该字段整条不出现", b'name="prompt"' not in bare,
          bare[:300].decode("utf-8", "replace"))
    check("格式决定文件名", asr._mime_of("mp3") == "audio/mpeg"
          and asr._mime_of("pcm16") == "application/octet-stream",
          f'{asr._mime_of("mp3")}/{asr._mime_of("pcm16")}')


def asr_run_checks() -> None:
    print("--- 4b. ASR 识别与失败原因 ---")
    transport, state = make_transport([(200, {"text": "帮我把这周的销量整理成表"})])
    result = asr.transcribe(audio_payload(), endpoint=endpoint(), transport=transport)
    check("成功识别返回文本", result.ok and result.text == "帮我把这周的销量整理成表", result.reason)
    check("成功时如实标注这次上传了音频", result.uploads is True and result.attempts == 1, str(result))
    check("一次成功不多打请求", len(state["calls"]) == 1, str(len(state["calls"])))
    payload = result.to_dict()
    check("结果给渲染层的是 text + 光标插入位置",
          payload["insert"] == {"text": result.text, "position": "cursor"}
          and "error" not in payload, str(payload)[:160])

    transport, _ = make_transport([(200, {"choices": [{"message": {"content": "网关形状一"}}]})])
    check("choices 形状的响应也认", asr.transcribe(audio_payload(), endpoint=endpoint(),
                                                   transport=transport).text == "网关形状一")
    transport, _ = make_transport([(200, {"segments": [{"text": "第一句"}, {"text": "第二句"}]})])
    check("segments 形状的响应拼成一句", asr.transcribe(audio_payload(), endpoint=endpoint(),
                                                       transport=transport).text == "第一句 第二句")
    transport, _ = make_transport([(200, {"result": "看不懂的结构"})])
    result = asr.transcribe(audio_payload(), endpoint=endpoint(), transport=transport)
    check("认不出的响应结构如实说读不懂，不猜字段",
          not result.ok and "没有可懂的转写文本字段" in result.reason, result.reason)

    transport, _ = make_transport([(200, {"error": {"message": "audio too short"}})])
    result = asr.transcribe(audio_payload(), endpoint=endpoint(), transport=transport)
    check("上游带 error 时把原因摊出来", not result.ok and "上游拒绝本次识别" in result.reason
          and "audio too short" in result.reason, result.reason)

    transport, _ = make_transport([(200, {"text": "   "})])
    result = asr.transcribe(audio_payload(), endpoint=endpoint(), transport=transport)
    check("识别为空不算成功（不冒充「识别到空白」）",
          not result.ok and "识别结果为空" in result.reason and "insert" not in result.to_dict(),
          result.reason)

    transport, _ = make_transport([(200, {"text": "长" * 9000})])
    result = asr.transcribe(audio_payload(), endpoint=endpoint(), transport=transport)
    check("超长转写截到上限并留说明",
          len(result.text) == asr.MAX_TRANSCRIPT_CHARS
          and any("超出 8000 字" in n for n in result.notes), str(result.notes))

    transport, state = make_transport([(500, b'{"error":"boom"}'), (200, {"text": "重试成功"})])
    result = asr.transcribe(audio_payload(), endpoint=endpoint(), transport=transport)
    check("5xx 重试一次并说明重试过",
          result.ok and result.attempts == 2 and any("重试第 1 次" in n for n in result.notes),
          str(result.notes))
    check("重试确实又打了一次", len(state["calls"]) == 2, str(len(state["calls"])))

    transport, state = make_transport([(500, {"error": f"bad {FAKE_KEY}"})])
    result = asr.transcribe(audio_payload(), endpoint=endpoint(), transport=transport)
    check("一直 500 时失败并把状态码带回来",
          not result.ok and result.http_status == 500 and "返回 500" in result.reason, result.reason)
    check("失败原因里的密钥已被洗白", FAKE_KEY not in result.reason, result.reason)
    check("重试有上限，不打成无休请求", len(state["calls"]) == aa.MAX_ASR_RETRIES + 1,
          str(len(state["calls"])))

    transport, state = make_transport([RuntimeError("语音上游连不上：refused")])
    result = asr.transcribe(audio_payload(), endpoint=endpoint(), transport=transport)
    check("连不上时重试后如实报不可达",
          not result.ok and "连不上" in result.reason and len(state["calls"]) == 2, result.reason)

    transport, state = make_transport([(401, {"error": {"message": "invalid api key"}})])
    result = asr.transcribe(audio_payload(), endpoint=endpoint(), transport=transport)
    check("4xx 不重试（重试也不会变成 200）",
          not result.ok and len(state["calls"]) == 1 and result.http_status == 401,
          str(len(state["calls"])))

    transport, state = make_transport([(200, {"text": "不该发生"})])
    result = asr.transcribe({"format": "wav", "data": ""}, endpoint=endpoint(), transport=transport)
    check("校验没过时一次请求都不发", not result.ok and "音频数据为空" in result.reason
          and state["calls"] == [], str(state["calls"]))
    result = asr.transcribe(audio_payload(durationMs=0), endpoint=endpoint(), transport=transport)
    check("时长为 0 的录音不白花一次上传",
          not result.ok and "录音时长为 0" in result.reason and state["calls"] == [], str(state["calls"]))
    result = asr.transcribe(audio_payload(), endpoint=endpoint(), transport=transport,
                            privacy_mode=True)
    check("隐私模式下即使已经选好云端也不发请求",
          not result.ok and "隐私模式已开启" in result.reason and state["calls"] == [], str(state["calls"]))
    result = asr.transcribe(audio_payload(format="not-base64!!"), endpoint=None, transport=transport)
    check("格式不合法时报错先于上游", not result.ok and state["calls"] == [], result.reason)

    result = asr.transcribe(audio_payload(data="!!!not base64!!!"), endpoint=endpoint(),
                            transport=transport)
    check("base64 解码失败给出原因", not result.ok and "解码失败" in result.reason, result.reason)
    result = asr.transcribe(audio_payload(), endpoint=None, transport=transport)
    check("没有端点时说清本层只会打哪个接口",
          not result.ok and "/audio/transcriptions" in result.reason, result.reason)
    result = asr.transcribe(audio_payload(), endpoint=endpoint(), transport=None)
    check("没注入 transport 时拒发真实请求（自测永不联网）",
          not result.ok and "注入 transport" in result.reason, result.reason)


def asr_entry_checks() -> None:
    print("--- 4c. 端点入口：hostResult、隐私与开关 ---")
    result = asr.accept_host_result({"text": "系统识别出的话"}, caps={"hostAsr": True})
    check("采纳桌面端已识别的结果（不上传）",
          result.ok and result.text == "系统识别出的话" and result.uploads is False, str(result))
    result = asr.accept_host_result({"text": "来路不明"}, caps={})
    check("未探测到系统识别能力时不采纳回传文本",
          not result.ok and "hostAsr" in result.reason, result.reason)
    result = asr.accept_host_result({"error": "麦克风被占用"}, caps={"hostAsr": True})
    check("桌面端报了失败就照实转达", not result.ok and "麦克风被占用" in result.reason, result.reason)
    result = asr.accept_host_result({"text": ""}, caps={"hostAsr": True})
    check("桌面端回空文本不等于识别成功", not result.ok and "结果为空" in result.reason, result.reason)

    transport, state = make_transport([(200, {"text": "端点入口走通"})])
    result = asr.run_asr({"audio": audio_payload()}, runtime=None,
                         model_config={"baseUrl": "https://m.invalid/v1", "apiKey": "m-key"},
                         caps={"cloudAsr": True}, env={}, transport=transport)
    check("入口把选型、配置与识别串起来", result.ok and result.text == "端点入口走通", str(result))
    check("选型说明与识别说明合并（调用方只摊一份）", isinstance(result.notes, list), str(result.notes))

    privacy_transport, privacy_calls = make_transport([(200, {"text": "不该走到这里"})])
    result = asr.run_asr({"audio": audio_payload()}, caps={"cloudAsr": True}, privacy_mode=True,
                         env={}, transport=privacy_transport)
    check("隐私模式在入口就被挡住：一次请求也没多发",
          not result.ok and privacy_calls["calls"] == [] and any("隐私" in n for n in result.notes),
          f"{result.notes} 请求数={len(privacy_calls['calls'])}")

    result = asr.run_asr({"hostResult": {"text": "桌面端已经识别完"}, "audio": audio_payload()},
                         caps={"hostAsr": True}, privacy_mode=True)
    check("隐私模式优先用桌面端结果，一个字也不上传",
          result.ok and result.adapter == "host_system" and result.uploads is False, str(result))

    result = asr.run_asr({"hostResult": {"text": "本机模型识别完", "adapter": "local_whisper_cpp"}},
                         caps={"localAsr": True}, privacy_mode=True)
    check("本机模型的回传按本机模型通道采纳",
          result.ok and result.adapter == "local_whisper_cpp" and result.uploads is False, str(result))
    result = asr.run_asr({"hostResult": {"text": "模型说自己是云端", "adapter": "openai_compatible"}},
                         caps={"hostAsr": True}, privacy_mode=True)
    check("回传里声明云端通道时按系统识别处理并留说明",
          result.ok and result.adapter == "host_system"
          and any("不是本机通道" in n for n in result.notes), str(result.notes))
    result = asr.run_asr({"hostResult": {"text": "没探测到的通道", "adapter": "local_whisper_cpp"},
                          "audio": audio_payload()},
                         caps={"hostAsr": True}, privacy_mode=True)
    check("声明的通道没被探测到时不采纳（能力表是唯一入口）",
          not result.ok and "localAsr" in result.reason, str(result))

    # 选到本机通道但结果还没回来：既不能上传，也不能报成「识别失败」
    host_transport, host_state = make_transport([(200, {"text": "不该走到这里"})])
    result = asr.run_asr({"audio": audio_payload()}, caps={"hostAsr": True},
                         privacy_mode=True, env={}, transport=host_transport)
    check("隐私模式选到系统识别时交回指令而不是报错",
          not result.ok and result.requires_host and result.adapter == "host_system", str(result))
    check("指令里写着要调哪个方法与要带的字段",
          any("voice:recognize" in n and "hostResult" in n for n in result.notes), str(result.notes))
    check("交回指令这一趟一次请求也没发",
          host_state["calls"] == [], str(host_state["calls"]))
    payload = result.to_dict()
    check("requiresHost 的载荷带方法名且不带 error（界面不该把它显示成失败）",
          payload.get("requiresHost") is True and "error" not in payload
          and payload["hostAction"]["method"] == "voice:recognize", str(payload)[:200])
    result = asr.run_asr({"audio": audio_payload(format="amr")}, caps={"hostAsr": True},
                         privacy_mode=True, env={}, transport=host_transport)
    check("交回指令之前先验音频：坏格式不骗桌面端跑一趟",
          not result.ok and not result.requires_host and "不支持的音频格式" in result.reason,
          str(result))

    # 云端配好且被探测到时，默认仍是云端：本机识别器差一档，不许无声降级
    cloud_transport, cloud_state = make_transport([(200, {"text": "云端识别"})])
    result = asr.run_asr({"audio": audio_payload()}, runtime=None,
                         model_config={"baseUrl": "https://m.invalid/v1", "apiKey": "m-key"},
                         caps={"cloudAsr": True, "hostAsr": True}, env={},
                         transport=cloud_transport)
    check("云端可用时默认走云端（有系统识别也不无声换通道）",
          result.ok and result.adapter == "openai_compatible" and result.uploads is True,
          str(result))
    check("这条路径确实打了一次上游", len(cloud_state["calls"]) == 1, str(cloud_state["calls"]))

    result = asr.run_asr({"audio": audio_payload()},
                         runtime={"audio": {"asr": {"enabled": "off"}}}, caps={"cloudAsr": True})
    check("设置里关闭语音输入时如实关闭", not result.ok and "已在设置里关闭" in result.reason, result.reason)

    result = asr.run_asr({"audio": audio_payload(), "localOnly": "1"}, caps={})
    check("点名本机识别时说清要怎么接上并且未验收",
          not result.ok and any("真机麦克风与本地模型均未验收" in n for n in result.notes),
          str(result.notes))
    check("本机通道未被当成可用（不假装识别过）", result.text == "" and not result.ok, str(result))

    result = asr.run_asr(None)
    check("空请求体不抛异常，给一句原因", not result.ok and result.reason != "", str(result))


# ── 5. TTS：排版剥离与分段 ─────────────────────────────────────

def tts_prepare_checks() -> None:
    print("--- 5. 朗读文本的准备 ---")
    chunks, notes = tts.prepare_read_aloud("")
    check("空文本不返回空分段假装能念", chunks == [] and notes == [tts.EMPTY_SPEECH_REASON], str(notes))
    chunks, notes = tts.prepare_read_aloud("```\nprint('hi')\n```")
    check("只有代码块时如实说没的可念", chunks == [] and notes[0] == tts.EMPTY_SPEECH_REASON, str(notes))

    text = ("## 结论\n"
            "- 本周销量上升 12%\n"
            "- 明细见 [报表](https://example.invalid/r.csv)\n\n"
            "```python\nprint(1)\n```\n\n"
            "| 日期 | 销量 |\n|---|---|\n| 周一 | 12 |\n\n"
            "参考 https://example.invalid/doc 与 `sales.py`\n")
    chunks, notes = tts.prepare_read_aloud(text)
    joined = "".join(chunks)
    check("标题符号与列表符号不进朗读", "##" not in joined and "\n" not in joined, joined[:120])
    check("代码块整块省略并留说明", "print(1)" not in joined and any("已省略 1 处代码块" in n for n in notes),
          str(notes))
    check("表格按行省略并留说明", "| 周一 | 12 |" not in joined and any("表格" in n for n in notes), str(notes))
    check("链接只念标题不念地址", "example.invalid/r.csv" not in joined and "报表" in joined
          and any("只念标题" in n for n in notes), joined[:120])
    check("裸链接地址省略", "https://example.invalid/doc" not in joined and any("裸链接" in n for n in notes),
          str(notes))
    check("行内代码留文字去反引号", "sales.py" in joined and "`" not in joined
          and any("行内代码" in n for n in notes), joined[:200])
    check("事实内容仍在（去掉的是排版不是信息）", "本周销量上升 12%" in joined, joined[:200])
    check("分段里没有换行符（念出来不卡顿）", "\n" not in joined, repr(joined[:120]))
    exact, _ = tts.prepare_read_aloud("第一句话讲完了。第二句话也讲完了。")
    check("分段拼回来等于整句，不丢也不劈", "".join(exact) == "第一句话讲完了。第二句话也讲完了。",
          str(exact))

    long_sentence = "这一句特别长" * 60 + "。"
    chunks, _ = tts.prepare_read_aloud(long_sentence)
    check("没有句末标点的长句不机械定长切割（整句交给上游）",
          chunks == [long_sentence], str([len(c) for c in chunks]))
    comma_text = "这段里有很多小分句，" * 40
    chunks, _ = tts.prepare_read_aloud(comma_text)
    check("长句在逗号处再切，段长不超过目标",
          len(chunks) >= 2 and all(len(c) <= aa.TTS_CHUNK_TARGET_CHARS for c in chunks),
          str([len(c) for c in chunks]))
    many = "。".join(f"第{i}句内容" for i in range(60)) + "。"
    chunks, _ = tts.prepare_read_aloud(many)
    check("分段数随内容增加但不出现空段", len(chunks) > 1 and all(c.strip() for c in chunks), str(chunks)[:120])
    check("每段不超过目标长度（不给播放侧塞超长段）",
          all(len(c) <= aa.TTS_CHUNK_TARGET_CHARS for c in chunks),
          str([len(c) for c in chunks]))
    huge = "很长的一句话。" * 2000
    chunks, notes = tts.prepare_read_aloud(huge)
    check("超过单轮上限只念前面并说明其余看着读",
          sum(len(c) for c in chunks) <= aa.MAX_TTS_TEXT_CHARS
          and any(f"超出单轮上限 {aa.MAX_TTS_TEXT_CHARS} 字" in n for n in notes), str(notes))


def _long_sentence(marker: str, chars: int = 200) -> str:
    """每句都够长的文本：分段目标是 240 字，短句会被凑进同一段，测不出分段行为"""
    return (marker * (chars // len(marker) + 1))[:chars] + "。"


def tts_run_checks() -> None:
    print("--- 5b. TTS 合成与分段失败语义 ---")
    two = _long_sentence("第一段内容") + _long_sentence("第二段内容")
    transport, state = make_transport([(200, b"FAKE-AUDIO-1"), (200, b"FAKE-AUDIO-2")])
    result = tts.synthesize(two, endpoint=endpoint("tts"), transport=transport)
    check("两句话合成两份分段计划", result.ok and len(result.segments) == 2, str(result.notes)[:200])
    check("每段带序号、文本与 base64 音频",
          result.segments[0]["index"] == 0 and result.segments[0]["audio"]
          and result.segments[1]["text"] != result.segments[0]["text"], str(result.segments)[:200])
    check("音频按上游原样编码，不二次加工",
          base64.b64decode(result.segments[0]["audio"]) == b"FAKE-AUDIO-1", result.segments[0]["audio"])
    check("spokenChars 累计的是真正要念的字数", result.spoken_chars == len(two), str(result.spoken_chars))
    check("云端合成如实标注上传了文本", result.uploads is True, str(result))
    check("每段一次请求", len(state["calls"]) == 2, str(len(state["calls"])))
    body0 = state["calls"][0]["body"]
    check("合成请求体是 JSON 且含 model/input/voice/response_format",
          all(k in json.loads(body0) for k in ("model", "input", "voice", "response_format")),
          body0[:120].decode("utf-8", "replace"))
    check("密钥只进请求头不进请求体", FAKE_KEY.encode() not in body0
          and state["calls"][0]["headers"]["Authorization"].endswith(FAKE_KEY), "")
    check("URL 由 baseUrl 与路径拼出（无重复斜杠）",
          state["calls"][0]["url"] == "https://upstream.invalid/v1/audio/speech",
          state["calls"][0]["url"])
    check("端点配的超时真的传到 transport", state["calls"][0]["timeout"] == 7.0,
          str(state["calls"][0]["timeout"]))

    transport, _ = make_transport([(200, json.dumps({"audio_b64": "QUJD"}).encode())])
    result = tts.synthesize(_long_sentence("网关形状"), endpoint=endpoint("tts"), transport=transport)
    check("网关把音频包成 JSON 时也认", result.ok and result.segments[0]["audio"] == "QUJD",
          str(result.notes)[:160])

    transport, state = make_transport([(200, b"AUDIO"), (502, {"error": f"bad {FAKE_KEY}"})])
    result = tts.synthesize(two, endpoint=endpoint("tts"), transport=transport)
    check("后面某段失败仍交付前面各段", result.ok and len(result.segments) == 1, str(result.segments)[:160])
    check("失败段写明第几段与「前面已合成」",
          any("第 2 段合成失败" in n and "前面各段已合成" in n for n in result.notes), str(result.notes)[:240])
    check("失败原因里的密钥被洗白",
          all(FAKE_KEY not in n for n in result.notes), str(result.notes)[:240])
    check("失败段之后不多打请求", len(state["calls"]) == 2, str(len(state["calls"])))

    transport, _ = make_transport([(502, {"error": "坏了"})])
    result = tts.synthesize(_long_sentence("第一段就坏了"), endpoint=endpoint("tts"), transport=transport)
    check("第一段就失败时不返回「成功但空」",
          not result.ok and result.http_status == 502 and result.segments == [], str(result))

    transport, _ = make_transport([RuntimeError("语音上游连不上：dns")])
    result = tts.synthesize(_long_sentence("念这句话"), endpoint=endpoint("tts"), transport=transport)
    check("上游不可达时报失败并带上原因",
          not result.ok and any("上游不可达" in n for n in result.notes), str(result.notes)[:160])

    transport, state = make_transport([(200, b"")])
    result = tts.synthesize(_long_sentence("空音频"), endpoint=endpoint("tts"), transport=transport)
    check("200 但音频为空不算成功（不交出空段）",
          not result.ok and result.segments == [] and state["calls"] != [], str(result.notes)[:160])

    transport, _ = make_transport([(200, b"<html><body>502 Bad Gateway</body></html>")])
    result = tts.synthesize(_long_sentence("网关错误页"), endpoint=endpoint("tts"), transport=transport)
    check("网关把错误页当 200 回时不交出去当音频",
          not result.ok and "HTML 而不是音频" in result.reason, result.reason)

    transport, _ = make_transport([(200, b"AUDIO"), (200, b"<html>oops</html>")])
    result = tts.synthesize(two, endpoint=endpoint("tts"), transport=transport)
    check("后半段遇到 HTML 错误页时前面的段照样能播",
          result.ok and len(result.segments) == 1
          and any("HTML 而不是音频" in n and "前面各段已合成" in n for n in result.notes),
          str(result.notes)[:200])

    transport, state = make_transport([(200, b"AUDIO")])
    result = tts.synthesize(_long_sentence("隐私模式"), endpoint=endpoint("tts"), transport=transport,
                            privacy_mode=True)
    check("隐私模式拒绝上传待朗读文本", not result.ok and "隐私模式已开启" in result.reason
          and state["calls"] == [], str(state["calls"]))
    result = tts.synthesize(_long_sentence("系统朗读"), endpoint=endpoint("tts"), transport=transport,
                            adapter_id="host_player")
    check("系统朗读不在合成层上传文本", not result.ok and "run_tts" in result.reason
          and state["calls"] == [], result.reason)
    result = tts.synthesize(_long_sentence("这句话"), endpoint=endpoint("tts"), transport=None)
    check("没注入 transport 时拒发真实请求", not result.ok and "注入 transport" in result.reason,
          result.reason)

    many = "".join(_long_sentence(f"句子{i}") for i in range(15))
    transport, state = make_transport([(200, b"A")] * 15)
    result = tts.synthesize(many, endpoint=endpoint("tts"), transport=transport, max_segments=12)
    check("段数超上限时只合成前若干段并说明",
          len(result.segments) == 12 and any("超过单轮合成上限 12 段" in n for n in result.notes),
          str(result.notes)[:200])
    check("上限之外不多打请求", len(state["calls"]) == 12, str(len(state["calls"])))

    transport, state = make_transport([(200, b"A")] * 3)
    result = tts.synthesize("```python\nprint(1)\n```", endpoint=endpoint("tts"), transport=transport)
    check("去掉排版后没内容时不打上游", not result.ok and result.reason == tts.EMPTY_SPEECH_REASON
          and state["calls"] == [], str(state["calls"]))


def tts_entry_checks() -> None:
    print("--- 5c. 朗读入口与隐私通道 ---")
    result = tts.run_tts({"text": _long_sentence("本周结论")}, caps={"cloudTts": True},
                         model_config={"baseUrl": "https://m.invalid/v1", "apiKey": "m-key"},
                         env={},
                         transport=lambda m, u, h, b, t: (200, {}, b"AUDIO"))
    check("入口把选型、配置与合成串起来", result.ok and result.segments, str(result.notes)[:200])
    result = tts.run_tts({"text": _long_sentence("本周结论")},
                         runtime={"audio": {"tts": {"enabled": "off"}}},
                         caps={"cloudTts": True}, env={})
    check("设置里关闭朗读时如实关闭", not result.ok and "朗读已在设置里关闭" in result.reason, result.reason)

    result = tts.run_tts({"text": "# 标题\n结论是销量涨了。"}, caps={"hostTts": True},
                         privacy_mode=True, env={})
    check("隐私模式走系统朗读：交出的只有文本分段",
          result.ok and result.uploads is False and result.adapter == "host_player", str(result.notes)[:200])
    check("分段里没有音频字段（播放由桌面端完成）",
          all("audio" not in s for s in result.segments) and result.segments[0]["text"], str(result.segments))
    check("标题符号不进交出的文本", all("#" not in s["text"] for s in result.segments),
          str(result.segments)[:160])
    check("系统朗读这条路不回传任何上游地址",
          "upstream" not in json.dumps(result.to_dict(), ensure_ascii=False), str(result.to_dict())[:160])
    result = tts.run_tts({"text": "```py\nprint(1)\n```"}, caps={"hostTts": True}, privacy_mode=True,
                         env={})
    check("系统朗读也只念有内容的部分", not result.ok and result.reason == tts.EMPTY_SPEECH_REASON,
          result.reason)
    result = tts.run_tts({"text": _long_sentence("这句话")}, caps={}, privacy_mode=True, env={})
    check("隐私模式且桌面端没声明能力时不硬上传",
          not result.ok and not result.uploads and any("hostTts" in n for n in result.notes),
          str(result.notes)[:200])
    result = tts.run_tts(None, env={})
    check("空请求体不抛异常", not result.ok and (result.reason or result.notes), str(result))
    payload = tts.TtsResult(ok=False, adapter="x", reason="坏了").to_dict()
    check("失败结果带 error 字段供界面直接显示", payload["error"] == "坏了" and "segments" not in payload,
          str(payload))


# ── 6. 打断状态机 ──────────────────────────────────────────────

def _speaking(count: int = 3, index: int = 1, played: int = 2000, total: int = 4000):
    session = bi.new_session("t-voice")
    session.state = bi.STATE_SPEAKING
    session.segment_count = count
    session.segment_index = index
    session.segment_played_ms = played
    session.segment_total_ms = total
    session.segment_completed = False
    session.playback_started_ms = 10_000
    return session


def barge_classify_checks() -> None:
    print("--- 6. 打断判定：先排除不是打断的那几类 ---")
    session = _speaking()
    ok, reason = bi.classify_barge_request(session, now_ms=20_000, speech_ms=900, energy=0.5)
    check("播报中足够长足够响的出声判为打断", ok and "判定为打断" in reason, reason)
    ok, reason = bi.classify_barge_request(session, now_ms=20_000, speech_ms=900, energy=0.5,
                                           heard_text="嗯")
    check("附和「嗯」不打断", not ok and "是附和" in reason, reason)
    ok, reason = bi.classify_barge_request(session, now_ms=20_000, speech_ms=900, energy=0.5,
                                           heard_text="对对 继续")
    check("整句都是附和词也不打断", not ok and "是附和" in reason, reason)
    ok, reason = bi.classify_barge_request(session, now_ms=20_000, speech_ms=900, energy=0.5,
                                           heard_text="等一下")
    check("明确要停的词优先于附和判定", ok and "明确说了要停" in reason, reason)
    ok, reason = bi.classify_barge_request(session, now_ms=10_200, speech_ms=900, energy=0.5)
    check("播报开头的回声留白内先按回声处理",
          not ok and "回声" in reason, reason)
    ok, reason = bi.classify_barge_request(session, now_ms=20_000, speech_ms=120, energy=0.5)
    check("太短的出声不停播（咳嗽与键盘声）",
          not ok and f"短于打断判定下限 {bi.MIN_SPEECH_MS_FOR_BARGE}" in reason, reason)
    ok, reason = bi.classify_barge_request(session, now_ms=20_000, speech_ms=900, energy=0.05)
    check("太轻的出声不停播", not ok and "低于打断判定下限" in reason, reason)
    idle = bi.new_session()
    ok, reason = bi.classify_barge_request(idle, now_ms=20_000, speech_ms=900, energy=0.5)
    check("没在播报时出声按正常输入处理", not ok and "不在播报中" in reason, reason)
    stopped = _speaking()
    stopped.halted = True
    ok, reason = bi.classify_barge_request(stopped, now_ms=20_000, speech_ms=900, energy=0.5)
    check("会话已按停后不再判打断", not ok and "已被显式停止" in reason, reason)
    check("阈值写成配置缺省并在源码里注明未验收",
          "实机打断时序未验收" in (PROJECT_ROOT / "engine" / "audio_barge_in.py").read_text(encoding="utf-8"),
          "注释里找不到未验收声明")


def barge_apply_checks() -> None:
    print("--- 6b. 状态转移与动作 ---")
    session = bi.new_session("t-turn")
    tr = bi.apply(session, bi.EVENT_SPEECH_START, now_ms=1000)
    check("空闲时开始收话 → listening 并开麦",
          tr.changed and session.state == bi.STATE_LISTENING
          and tr.actions == ["open_microphone_stream"], tr.to_dict())
    tr = bi.apply(session, bi.EVENT_SPEECH_START, now_ms=1100)
    check("重复的语音起始事件不再开一次麦",
          not tr.changed and session.state == bi.STATE_LISTENING, tr.reason)
    tr = bi.apply(session, bi.EVENT_SPEECH_END, now_ms=3000)
    check("用户说完 → thinking（交给模型）",
          session.state == bi.STATE_THINKING and "close_microphone_stream" in tr.actions, str(tr.actions))
    tr = bi.apply(session, bi.EVENT_SEND, now_ms=3100)
    check("提交本轮文本是显式动作，不靠副作用",
          tr.changed and "submit_turn" in tr.actions and session.state == bi.STATE_THINKING, str(tr.actions))
    tr = bi.apply(session, bi.EVENT_AUDIO_READY, now_ms=5000, segment_index=0, segment_count=3,
                  total_ms=4000)
    check("首段音频就绪 → speaking，并记住段数",
          session.state == bi.STATE_SPEAKING and session.segment_count == 3
          and session.playback_started_ms == 5000, session.snapshot())
    check("多段时首段不算念完（接回判断要用）", session.segment_completed is False, str(session.snapshot()))
    tr = bi.apply(session, bi.EVENT_PLAYBACK_PROGRESS, now_ms=6000, played_ms=1500, total_ms=4000)
    check("进度事件只挪播放头，不改状态",
          session.state == bi.STATE_SPEAKING and session.segment_played_ms == 1500
          and tr.actions == ["update_playhead"], str(tr.actions))
    tr = bi.apply(session, bi.EVENT_PLAYBACK_END, now_ms=8000)
    check("一段念完接下一段", session.segment_index == 1 and "play_next_segment" in tr.actions,
          str(tr.actions))
    tr = bi.apply(session, bi.EVENT_PLAYBACK_END, now_ms=11_000)
    check("倒数第二段念完仍接下一段，段号与「念完」标记同步",
          session.segment_index == 2 and session.segment_completed is True, str(session.snapshot()))
    tr = bi.apply(session, bi.EVENT_PLAYBACK_END, now_ms=12_000)
    check("最后一段念完回到 idle 并关播放",
          session.state == bi.STATE_IDLE and "close_playback" in tr.actions, str(tr.actions))
    tr = bi.apply(session, bi.EVENT_PLAYBACK_END, now_ms=13_000)
    check("念完之后重复的结束事件是空操作", not tr.changed and session.state == bi.STATE_IDLE, tr.reason)

    session = _speaking()
    tr = bi.apply(session, bi.EVENT_SPEECH_START, now_ms=20_000, speech_ms=900, energy=0.6)
    check("播报中插话 → interrupting 并立刻停播",
          session.state == bi.STATE_INTERRUPTING and "stop_playback" in tr.actions, tr.to_dict())
    check("被打断时留下「接回剩余段」的动作",
          "keep_remaining_segments" in tr.actions and session.resume_after_turn is True, str(tr.actions))
    check("每次插话计数（同一轮反复打断要看得出来）", session.barge_count == 1, str(session.barge_count))
    tr = bi.apply(session, bi.EVENT_BARGE_CONFIRM, now_ms=20_400)
    check("打断确认后进入 listening 并提交这一轮",
          session.state == bi.STATE_LISTENING and "commit_turn" in tr.actions, str(tr.actions))
    tr = bi.apply(session, bi.EVENT_BARGE_CONFIRM, now_ms=21_000)
    check("非 interrupting 状态下再确认打断是空操作",
          not tr.changed and session.state == bi.STATE_LISTENING, tr.reason)

    session = _speaking()
    bi.apply(session, bi.EVENT_SPEECH_START, now_ms=20_000, speech_ms=900, energy=0.6)
    tr = bi.apply(session, bi.EVENT_BARGE_CONFIRM, now_ms=20_000 + bi.MAX_BARGE_LATENCY_MS + 1)
    check("检测到到确认超过时限时丢缓冲重来（不停在半路）",
          "discard_buffered_audio" in tr.actions and session.resume_after_turn is False
          and session.state == bi.STATE_LISTENING, str(tr.actions))
    check("超时那条把时长与上限都写进原因", "超过上限" in tr.reason, tr.reason)

    session = _speaking()
    bi.apply(session, bi.EVENT_SPEECH_START, now_ms=20_000, speech_ms=100, energy=0.6)
    check("判定不成立时继续念，但观察留在 notes 里（不装作没听见）",
          session.state == bi.STATE_SPEAKING and any("短于打断判定下限" in n for n in session.notes),
          str(session.notes))
    session = _speaking()
    bi.apply(session, bi.EVENT_SPEECH_START, now_ms=20_000, speech_ms=900, energy=0.6)
    tr = bi.apply(session, bi.EVENT_BARGE_REJECT, now_ms=20_500, heard_text="背景杂音")
    check("判定为回声/噪声时恢复播报且保持段号",
          session.state == bi.STATE_SPEAKING and "resume_playback_from_pause" in tr.actions
          and "keep_segment_index" in tr.actions, str(tr.actions))
    session = _speaking()
    bi.apply(session, bi.EVENT_SPEECH_START, now_ms=20_000, speech_ms=900, energy=0.6)
    tr = bi.apply(session, bi.EVENT_SPEECH_END, now_ms=21_000)
    check("插话未确认成新问题且用户说完 → 接回放音并保持段号",
          session.state == bi.STATE_SPEAKING
          and {"resume_playback_from_pause", "keep_segment_index"} <= set(tr.actions)
          and "插话未确认成新问题" in tr.reason, tr.to_dict())

    session = _speaking()
    tr = bi.apply(session, "sneeze", now_ms=1)
    check("未知事件返回原因并列出可用事件",
          not tr.changed and "未知语音事件" in tr.reason and bi.EVENT_PLAYBACK_END in tr.reason, tr.reason)
    tr = bi.apply(session, bi.EVENT_AUDIO_READY, now_ms=1)
    check("不在等待首段的状态时 audio_ready 是空操作", not tr.changed, tr.reason)
    tr = bi.apply(session, bi.EVENT_PLAYBACK_PROGRESS, now_ms=20_000, played_ms=800)
    check("播报中收到播放进度：只挪播放头，状态仍是 speaking",
          tr.changed and session.state == bi.STATE_SPEAKING
          and session.segment_played_ms == 800 and "update_playhead" in tr.actions,
          str(tr.actions))
    bi.apply(session, bi.EVENT_SPEECH_START, now_ms=21_000, speech_ms=900, energy=0.6)
    tr = bi.apply(session, bi.EVENT_PLAYBACK_PROGRESS, now_ms=21_500, played_ms=999)
    check("播放进度事件在 interrupting 态被忽略而不是改状态",
          not tr.changed and session.state == bi.STATE_INTERRUPTING
          and session.segment_played_ms == 800, tr.reason)

    session = _speaking()
    tr = bi.apply(session, bi.EVENT_STOP, now_ms=20_000)
    check("按停：停播 + 清队列 + 关麦，并标记 halted",
          session.state == bi.STATE_STOPPED and session.halted is True
          and {"stop_playback", "flush_playback_queue", "close_microphone_stream"} <= set(tr.actions),
          str(tr.actions))
    tr = bi.apply(session, bi.EVENT_SPEECH_START, now_ms=21_000)
    check("按停后只接受 reset，其它事件不再改状态",
          not tr.changed and session.state == bi.STATE_STOPPED, tr.reason)
    tr = bi.apply(session, bi.EVENT_RESET, now_ms=22_000)
    check("reset 清空 halted、打断计数与接回标记",
          session.state == bi.STATE_IDLE and session.halted is False
          and session.barge_count == 0 and session.resume_after_turn is False, session.snapshot())
    session = _speaking()
    tr = bi.apply(session, bi.EVENT_ERROR, now_ms=9_000, heard_text="上游 502")
    check("出错回到 idle 并把原因交给界面",
          session.state == bi.STATE_IDLE and "surface_reason_to_user" in tr.actions
          and "上游 502" in tr.reason, tr.to_dict())
    check("每一次转移都进了日志（状态改了日志必须也改）",
          all({"event", "from", "to", "reason", "actions"} <= set(row) for row in session.log)
          and session.log[-1]["from"] == bi.STATE_SPEAKING, str(session.log[-1]))


def barge_remainder_checks() -> None:
    print("--- 6c. 接回：剩下念哪几段 ---")
    session = _speaking(count=5, index=2, played=500, total=4000)
    session.resume_after_turn = True
    segments, note = bi.remainder_plan(session)
    check("只念了个开头的那段整段重念（不从半句接）",
          segments == [2, 3, 4] and any("重念整段" in n for n in session.notes), str(segments))
    check("说明里的段号给人看（从 1 数）", "接回可念的段：[3, 4, 5]" in note, note)

    session = _speaking(count=5, index=2, played=3600, total=4000)
    session.resume_after_turn = True
    segments, note = bi.remainder_plan(session)
    check("已念过大半的那段放过，接后面", segments == [3, 4], str(segments))

    session = _speaking(count=1, index=0, played=1000, total=4000)
    session.resume_after_turn = True
    session.segment_completed = True
    segments, note = bi.remainder_plan(session)
    check("打断时已到最后一段就不硬凑接念", segments == [] and "没有可接的内容" in note, note)

    session = _speaking(count=4, index=1)
    session.resume_after_turn = False
    segments, note = bi.remainder_plan(session)
    check("没标记接回就不念剩余（按停也算）", segments == [] and "不需要接念" in note, note)

    session = _speaking(count=3, index=0)
    bi.apply(session, bi.EVENT_SPEECH_START, now_ms=20_000, speech_ms=900, energy=0.6)
    bi.apply(session, bi.EVENT_BARGE_CONFIRM, now_ms=20_300)
    tr = bi.apply(session, bi.EVENT_SPEECH_END, now_ms=22_000)
    check("用户说完这一轮时把「还有剩余没念」标出来",
          session.state == bi.STATE_THINKING and "note_pending_remainder" in tr.actions,
          str(tr.actions))
    check("接回标记在整轮里不丢（listening 收尾时仍在）",
          session.resume_after_turn is True
          and session.log[-1]["event"] == bi.EVENT_SPEECH_END, str(session.log[-1]))
    segments, note = bi.remainder_plan(session)
    check("收尾后仍算得出该接念的段（剩余内容不是丢了就算完）",
          segments == [0, 1, 2] and "接回可念的段" in note, note)

    raw = session.snapshot()
    restored = bi.session_from_dict(raw)
    check("快照能还原成会话（字段同名）",
          restored.state == session.state and restored.segment_index == session.segment_index
          and restored.segment_count == session.segment_count
          and restored.barge_count == session.barge_count, restored.snapshot())
    odd = bi.session_from_dict({"state": "teleporting", "segmentIndex": -3, "segmentCount": 0,
                               "unknownField": 1})
    check("非法状态与越界段号被归一而不是猜",
          odd.state == bi.STATE_IDLE and odd.segment_index == 0 and odd.segment_count == 1,
          odd.snapshot())
    check("空载荷回到空闲会话而不是异常", bi.session_from_dict(None).state == bi.STATE_IDLE)


def main() -> None:
    registry_checks()
    cloud_config_checks()
    payload_checks()
    asr_request_checks()
    asr_run_checks()
    asr_entry_checks()
    tts_prepare_checks()
    tts_run_checks()
    tts_entry_checks()
    barge_classify_checks()
    barge_apply_checks()
    barge_remainder_checks()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("audio selftest: ALL PASS")


if __name__ == "__main__":
    main()
