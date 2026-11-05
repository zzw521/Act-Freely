"""实时语音的打断状态机（缺陷 D-11 / 会话 #160，可离线半边）

「用户一开口，助手就闭嘴」听起来一句话，实际是一台状态机，而且它有三个会咬人的坑：

1. **不能把所有出声都当打断**。咳嗽、背景人声、本机扬声器回到麦克风的回声，都会触发
   「检测到语音」。判据只看持续时长与能量，所以阈值必须集中在这里、由调用方递观测值进来，
   而不是散在播放代码里各判各的。
2. **附和不是打断**。中文里「嗯」「对」「好的」是「你继续说」，把它当打断会让助手
   每被附和一次就停一次，体验比不打断更差。带文本时按附和词表放行。
3. **打断之后要有得接**。停播时记下停在哪一段、该段有没有念完，用户这句说完后
   才能决定是「接着念剩下的」还是「整段作废重来」。

状态与事件都是纯数据：`apply(state, event)` 返回新状态、动作列表与一句理由。
不碰线程、不碰播放器、不睡时间 —— 所以这台机器可以完全离线自测。

诚实边界（很重要）：下面这些阈值是本层的**配置缺省**，不是实测出来的。
真麦克风的回声路径、各家 VAD 的触发延迟、播报到停播的实际打断时延，
在本仓库里没有真机证据之前一律**未验收**；自测证明的是状态转移与守卫条件成立，
不是「打断够快够准」。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


# ── 状态 ────────────────────────────────────────────────────────

STATE_IDLE = "idle"              # 没在听也没在说
STATE_LISTENING = "listening"    # 麦克风在收，用户在说
STATE_THINKING = "thinking"      # 已交给模型，还没有可播的音频
STATE_SPEAKING = "speaking"      # 正在播朗读
STATE_INTERRUPTING = "interrupting"  # 播的时候检测到语音，还没判定成不成立
STATE_STOPPED = "stopped"        # 用户按停（不是打断，是整轮结束）

STATES = (STATE_IDLE, STATE_LISTENING, STATE_THINKING, STATE_SPEAKING,
          STATE_INTERRUPTING, STATE_STOPPED)

#: 只有这两个状态里检测到语音才需要考虑打断；其它状态下出声就是正常输入
BARGE_IN_CAPABLE_STATES = (STATE_SPEAKING, STATE_INTERRUPTING)

# ── 事件 ────────────────────────────────────────────────────────

EVENT_SPEECH_START = "speech_start"
EVENT_SPEECH_END = "speech_end"
EVENT_SEND = "send"
EVENT_AUDIO_READY = "audio_ready"
EVENT_PLAYBACK_START = "playback_start"
EVENT_PLAYBACK_PROGRESS = "playback_progress"
EVENT_PLAYBACK_END = "playback_end"
EVENT_BARGE_CONFIRM = "barge_confirm"
EVENT_BARGE_REJECT = "barge_reject"
EVENT_STOP = "stop"
EVENT_ERROR = "error"
EVENT_RESET = "reset"

EVENTS = (EVENT_SPEECH_START, EVENT_SPEECH_END, EVENT_SEND, EVENT_AUDIO_READY,
          EVENT_PLAYBACK_START, EVENT_PLAYBACK_PROGRESS, EVENT_PLAYBACK_END,
          EVENT_BARGE_CONFIRM, EVENT_BARGE_REJECT, EVENT_STOP, EVENT_ERROR, EVENT_RESET)

# ── 阈值（配置缺省，非实测；实机打断时序未验收）──────────────────

#: 播报头一段留白里检测到的语音先当作回声/串扰，不判打断
ECHO_GUARD_MS = 350
#: 语音要持续这么久才算「真的要插话」，短于此只记一次观察
MIN_SPEECH_MS_FOR_BARGE = 280
#: 打断成立所需的最低相对能量（0–1）。真机阈值需要按麦克风重标
MIN_BARGE_ENERGY = 0.18
#: 一次打断最多允许「检测到」到「停播」隔多久；超了就记一条超时，不再假装还在听
MAX_BARGE_LATENCY_MS = 1200
#: 被打断那一段只念了不到这个比例就中断时，接回时重念整段而不是从半句接
MIN_SEGMENT_RATIO_FOR_RESUME = 0.7

#: 中文/英文里常见的「你继续说」式附和：带文本且命中就不打断
BACKCHANNEL_TERMS = (
    "嗯", "嗯嗯", "哦", "噢", "啊", "对", "对对", "是的", "好", "好的", "行", "可以",
    "继续", "明白", "懂了", "原来如此", "ok", "okay", "yeah", "yes", "uh-huh", "mhm",
    "right", "got it", "I see",
)

#: 明确要停的词（与附和相对，带文本时优先判它）
STOP_TERMS = ("停", "停下", "停止", "别说了", "闭嘴", "等一下", "先停", "stop", "pause")


# ── 状态对象 ────────────────────────────────────────────────────

@dataclass
class VoiceSession:
    """一条实时语音会话此刻的形状。

    时间一律由调用方递进来（`now_ms`），本层不调 `time.monotonic()` ——
    一来状态机要能在自测里被精确驱动，二来 vendor 解释器的 monotonic 粒度是 15.6 ms，
    贴着阈值判定会把抖动读成行为差异。
    """

    state: str = STATE_IDLE
    task_id: str = ""
    #: 正在播的分段序号（与 `audio_tts_runtime` 的 segments[i].index 同一条编号）
    segment_index: int = 0
    segment_count: int = 1
    #: 当前段已播时长与整段预估时长（毫秒），用于「接回时重念还是续念」
    segment_played_ms: int = 0
    segment_total_ms: int = 0
    #: 本轮播报开始时刻，用于回声留白判定
    playback_started_ms: int = 0
    #: 检测到语音的时刻，用于算打断时延
    speech_detected_ms: int = 0
    #: 停播时该段是否已念完（念完了就不存在接回）
    segment_completed: bool = True
    #: 停播后剩下的段是否还打算念
    resume_after_turn: bool = False
    #: 会话被显式停止（用户按停），除 reset 外不再接受任何转移
    halted: bool = False
    #: 每一段被打断过几次：同一轮反复插话要在界面上看得出来
    barge_count: int = 0
    log: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "taskId": self.task_id,
            "segmentIndex": self.segment_index,
            "segmentCount": self.segment_count,
            "segmentPlayedMs": self.segment_played_ms,
            "segmentTotalMs": self.segment_total_ms,
            "segmentCompleted": self.segment_completed,
            "resumeAfterTurn": self.resume_after_turn,
            "bargeCount": self.barge_count,
            "halted": self.halted,
        }


@dataclass
class Transition:
    """一次事件的结果：新状态、要执行的动作、以及一句永远说得出口的理由。"""

    session: VoiceSession
    changed: bool
    reason: str
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "changed": self.changed,
            "reason": self.reason,
            "actions": list(self.actions),
            "session": self.session.snapshot(),
        }


def _noop(session: VoiceSession, reason: str) -> Transition:
    return Transition(session=session, changed=False, reason=reason)


def _record(session: VoiceSession, event: str, reason: str, actions: list[str],
            **patch) -> Transition:
    """唯一的状态落地点：转移、动作、日志一起写，避免「状态改了但日志没记」。"""
    before = session.state
    for key, value in patch.items():
        setattr(session, key, value)
    session.log.append({
        "event": event,
        "from": before,
        "to": session.state,
        "reason": reason,
        "actions": list(actions),
    })
    return Transition(session=session, changed=True, reason=reason, actions=actions)


# ── 打断判定：守卫条件都集中在这里 ────────────────────────────────

def classify_barge_request(session: VoiceSession, *, now_ms: int, speech_ms: int = 0,
                           energy: float = 0.0, heard_text: str = "") -> tuple[bool, str]:
    """出声要不要打断播报 → (成立吗, 原因)。

    判定顺序是有意的：先排除「不是打断的那几类」（回声、附和、太短太轻），
    再认打断。反过来写会让「停」这种明确指令被时长条件误伤。
    """
    text = (heard_text or "").strip().lower()
    if any(term in text for term in (t.lower() for t in STOP_TERMS)):
        return True, f"用户明确说了要停（命中「{heard_text.strip()[:12]}」）"
    if text in {t.lower() for t in BACKCHANNEL_TERMS} or (
            text and all(word in BACKCHANNEL_TERMS for word in text.replace(",", " ").split())):
        return False, f"「{heard_text.strip()[:12]}」是附和，继续念而不是停下"
    if session.state not in BARGE_IN_CAPABLE_STATES:
        return False, f"当前状态是 {session.state}，不在播报中，出声按正常输入处理而不是打断"
    if session.halted:
        return False, "会话已被显式停止，不再接受打断"
    elapsed = max(0, int(now_ms) - int(session.playback_started_ms or 0))
    if elapsed < ECHO_GUARD_MS:
        return False, (
            f"播报开始 {elapsed} ms 内检测到的语音先按回声处理"
            f"（回声留白 {ECHO_GUARD_MS} ms）"
        )
    if speech_ms < MIN_SPEECH_MS_FOR_BARGE:
        return False, (
            f"出声只持续 {speech_ms} ms，短于打断判定下限 {MIN_SPEECH_MS_FOR_BARGE} ms"
            "（咳嗽与背景噪声不该停播）"
        )
    if energy < MIN_BARGE_ENERGY:
        return False, (
            f"语音能量 {energy:.2f} 低于打断判定下限 {MIN_BARGE_ENERGY:.2f}，先不停播"
        )
    return True, (
        f"用户在播报中说了 {speech_ms} ms（能量 {energy:.2f}），判定为打断"
    )


# ── 事件转移 ────────────────────────────────────────────────────

def apply(session: VoiceSession, event: str, *, now_ms: int = 0, speech_ms: int = 0,
          energy: float = 0.0, heard_text: str = "", segment_index: int | None = None,
          segment_count: int | None = None, played_ms: int = 0, total_ms: int = 0) -> Transition:
    """把一次事件喂进状态机。未知事件、非法状态一律「不变 + 说清为什么」。"""
    if event not in EVENTS:
        return _noop(session, f"未知语音事件 {event}（可用：{'/'.join(EVENTS)}）")
    if session.state not in STATES:
        return _noop(session, f"会话处于未知状态 {session.state}，先 reset 再说")
    if event == EVENT_RESET:
        return _record(session, event, "会话复位", ["flush_playback_queue"],
                       state=STATE_IDLE, halted=False, barge_count=0, resume_after_turn=False,
                       segment_index=0, segment_completed=True, speech_detected_ms=0)
    if session.halted and event not in (EVENT_STOP,):
        return _noop(session, "会话已停止，只接受 reset 重新开始")

    if event == EVENT_SPEECH_START:
        if session.state in (STATE_SPEAKING, STATE_INTERRUPTING):
            confirm, reason = classify_barge_request(
                session, now_ms=now_ms, speech_ms=speech_ms, energy=energy,
                heard_text=heard_text)
            if confirm:
                latency = max(0, int(now_ms) - int(session.speech_detected_ms or now_ms))
                actions = ["stop_playback", "flush_playback_queue"]
                resume = not session.segment_completed
                if resume:
                    actions.append("keep_remaining_segments")
                return _record(
                    session, event, reason, actions,
                    state=STATE_INTERRUPTING, speech_detected_ms=int(now_ms),
                    barge_count=session.barge_count + 1,
                    resume_after_turn=resume and latency <= MAX_BARGE_LATENCY_MS,
                )
            # 不算打断：继续念，但把观察到的东西留在 notes 里，别装作没听见
            if reason not in session.notes:
                session.notes.append(reason)
            return _noop(session, reason)
        if session.state == STATE_LISTENING:
            return _noop(session, "已经在听了，重复的语音起始事件忽略")
        return _record(session, event, "开始收用户的话", ["open_microphone_stream"],
                       state=STATE_LISTENING, speech_detected_ms=int(now_ms))

    if event == EVENT_BARGE_CONFIRM:
        if session.state != STATE_INTERRUPTING:
            return _noop(session, f"只有「播报中检测到语音」才需要确认打断，当前是 {session.state}")
        gap = int(now_ms) - int(session.speech_detected_ms or now_ms)
        if gap > MAX_BARGE_LATENCY_MS:
            # 从检测到语音到确认停播隔太久：这一刻的音频早就不是用户说的那句了
            return _record(
                session, event,
                f"打断确认隔了 {gap} ms，超过上限 {MAX_BARGE_LATENCY_MS} ms，"
                "按停播但丢掉这段音频重来",
                ["stop_playback", "discard_buffered_audio", "restart_turn"],
                state=STATE_LISTENING, resume_after_turn=False,
            )
        return _record(session, event, "打断成立，转入 listening",
                       ["stop_playback", "commit_turn"], state=STATE_LISTENING)

    if event == EVENT_BARGE_REJECT:
        if session.state != STATE_INTERRUPTING:
            return _noop(session, f"当前是 {session.state}，没有待判定的打断")
        return _record(
            session, event,
            "判定为回声或噪声，恢复播报" + (f"：{heard_text[:12]}" if heard_text else ""),
            ["resume_playback_from_pause", "keep_segment_index"],
            state=STATE_SPEAKING,
        )

    if event == EVENT_SPEECH_END:
        if session.state == STATE_LISTENING:
            actions = ["close_microphone_stream"]
            if session.resume_after_turn:
                actions.append("note_pending_remainder")
            return _record(session, event, "用户说完，交给模型", actions,
                           state=STATE_THINKING)
        if session.state == STATE_INTERRUPTING:
            # 检测到出声、也已经停播，但一直没走到 barge_confirm 用户就说完了：
            # 这句没构成新一轮问题，把被打断的播报接回去
            return _record(session, event, "插话未确认成新问题，恢复被打断的播报",
                           ["resume_playback_from_pause", "keep_segment_index"],
                           state=STATE_SPEAKING)
        return _noop(session, f"当前是 {session.state}，没有正在收的语音")

    if event == EVENT_SEND:
        if session.state not in (STATE_LISTENING, STATE_THINKING):
            return _noop(session, f"只有说完话或已在思考时才需要送模型，当前是 {session.state}")
        return _record(session, event, "本轮文本已提交", ["submit_turn"],
                       state=STATE_THINKING)

    if event == EVENT_AUDIO_READY:
        if session.state != STATE_THINKING:
            return _noop(session, f"当前是 {session.state}，没有等待首段音频的思考轮")
        index = int(segment_index or 0)
        count = int(segment_count or 1) if segment_count else 1
        return _record(
            session, event, f"首段音频就绪（共 {count} 段），开始播第 {index + 1} 段",
            ["start_playback"], state=STATE_SPEAKING, playback_started_ms=int(now_ms),
            segment_index=index, segment_count=count, segment_completed=count <= 1,
            segment_played_ms=0, segment_total_ms=int(total_ms or 0),
        )

    if event == EVENT_PLAYBACK_PROGRESS:
        if session.state != STATE_SPEAKING:
            return _noop(session, f"当前是 {session.state}，不在播放中，进度事件忽略")
        index = session.segment_index if segment_index is None else int(segment_index)
        done = index >= session.segment_count - 1
        return _record(
            session, event, f"进度：第 {index + 1}/{session.segment_count} 段，已播 {played_ms} ms",
            ["update_playhead"], state=STATE_SPEAKING, playback_started_ms=session.playback_started_ms,
            segment_index=index, segment_completed=done,
            segment_played_ms=int(played_ms), segment_total_ms=int(total_ms or session.segment_total_ms),
        )

    if event == EVENT_PLAYBACK_END:
        if session.state not in (STATE_SPEAKING, STATE_INTERRUPTING):
            return _noop(session, f"当前是 {session.state}，没有正在播的音频")
        index = session.segment_index
        more = index + 1 < session.segment_count
        if session.state == STATE_INTERRUPTING:
            return _record(
                session, event, "被打断的这段已停播，等待用户这句说完",
                [], state=STATE_INTERRUPTING, segment_completed=False,
            )
        if more:
            return _record(
                session, event, f"第 {index + 1} 段念完，接第 {index + 2} 段",
                ["play_next_segment"], state=STATE_SPEAKING, segment_index=index + 1,
                segment_completed=index + 2 >= session.segment_count,
                playback_started_ms=int(now_ms), segment_played_ms=0,
            )
        return _record(session, event, "全部念完", ["close_playback"],
                       state=STATE_IDLE, segment_completed=True, resume_after_turn=False)

    if event == EVENT_STOP:
        return _record(
            session, event, "用户按停，本轮结束",
            ["stop_playback", "flush_playback_queue", "close_microphone_stream"],
            state=STATE_STOPPED, halted=True, resume_after_turn=False,
        )

    if event == EVENT_ERROR:
        why = heard_text or "上游或设备报错"
        return _record(
            session, event, f"出错收口：{why}",
            ["stop_playback", "surface_reason_to_user"], state=STATE_IDLE,
            resume_after_turn=False,
        )

    return _noop(session, f"事件 {event} 在状态 {session.state} 下没有可用转移")


# ── 接回：用户说完之后，剩下的段落还要不要念 ──────────────────────

def remainder_plan(session: VoiceSession) -> tuple[list[int], str]:
    """算出「这一轮该接念哪几段」→ (段序号, 一句说明)。

    只念了个开头的段直接重念整段：从半句接上听不出说了什么；
    已经念过大半的段就放过，剩下的段照常接。用户插话问的是新东西，
    所以接念只在「没有新一轮要回答」时由调用方决定，本层只把可用的段号交出去。
    """
    if not session.resume_after_turn:
        return [], "本轮不需要接念（没有被打断的剩余内容，或已被显式停止）"
    start = session.segment_index
    ratio = 0.0
    if session.segment_total_ms > 0:
        ratio = session.segment_played_ms / session.segment_total_ms
    if session.segment_completed:
        start = session.segment_index + 1
    elif ratio < MIN_SEGMENT_RATIO_FOR_RESUME:
        session.notes.append(
            f"被打断那一段只念了 {ratio:.0%}（不足 {MIN_SEGMENT_RATIO_FOR_RESUME:.0%}），"
            "接回时重念整段而不是从半句接")
    else:
        start = session.segment_index + 1
    remaining = [i for i in range(start, session.segment_count)]
    if not remaining:
        return [], "被打断时已经念到最后一段，没有可接的内容"
    return remaining, f"接回可念的段：{[i + 1 for i in remaining]}（共 {session.segment_count} 段）"


def new_session(task_id: str = "") -> VoiceSession:
    return VoiceSession(task_id=task_id)


def session_from_dict(raw: dict | None) -> VoiceSession:
    """从事件回放的载荷里恢复会话形状（只恢复本层认得的字段，未知字段忽略而不是猜）。"""
    data = raw if isinstance(raw, dict) else {}
    state = str(data.get("state") or STATE_IDLE)
    if state not in STATES:
        state = STATE_IDLE
    return VoiceSession(
        state=state,
        task_id=str(data.get("taskId") or ""),
        segment_index=max(0, int(data.get("segmentIndex") or 0)),
        segment_count=max(1, int(data.get("segmentCount") or 1)),
        segment_played_ms=max(0, int(data.get("segmentPlayedMs") or 0)),
        segment_total_ms=max(0, int(data.get("segmentTotalMs") or 0)),
        playback_started_ms=max(0, int(data.get("playbackStartedMs") or 0)),
        speech_detected_ms=max(0, int(data.get("speechDetectedMs") or 0)),
        segment_completed=bool(data.get("segmentCompleted", True)),
        resume_after_turn=bool(data.get("resumeAfterTurn", False)),
        halted=bool(data.get("halted", False)),
        barge_count=max(0, int(data.get("bargeCount") or 0)),
    )
