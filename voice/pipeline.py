"""Jarvis's voice pipeline, one per connected client.

    mic -> push-to-talk gate -> faster-whisper -> turn taking (Silero VAD,
    Smart Turn v3) -> the Core (Claude Code) -> Kokoro, a sentence at a time
    -> speaker

Claude is reached only through the Core, so voice turns share the session,
memory, approvals and audit with typed ones. Starting to talk (or pressing the
talk key) interrupts Jarvis: the audio stops and the Core stops the reply.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    StartFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

log = logging.getLogger(__name__)

PTT = "ptt"
HANDS_FREE = "hands-free"
MODES = (PTT, HANDS_FREE)

# Audio keeps flowing this long after the talk key is released, so the last
# word (still in flight from the browser) isn't cut off.
RELEASE_GRACE_SECS = 0.2

ERROR_REPLY = "Sorry, something went wrong on my side."

Send = Callable[[dict], Awaitable[None]]


class PushToTalkGate(FrameProcessor):
    """In push-to-talk mode, the talk key alone decides what Jarvis hears.

    Outside the key, audio becomes silence. Each key hold is exactly one speech
    segment for Whisper (the gate stands in for the VAD, so a pause mid-sentence
    can't split it) and one user turn: pressing interrupts Jarvis, releasing
    ends the turn without waiting for the turn model.
    """

    def __init__(self, enabled: bool, vad=None, grace_secs: float = RELEASE_GRACE_SECS, **kwargs):
        """`vad` (a Pipecat VADAnalyzer) confirms there was speech in a hold, so a
        press that only interrupts Jarvis sends nothing to Whisper. Without one,
        every hold counts as speech."""
        super().__init__(**kwargs)
        self.enabled = enabled
        self.vad = vad
        self.grace_secs = grace_secs
        self.held = False
        self.speech = False  # this hold has speech (a segment is open)
        self._closing: asyncio.Task | None = None  # the release grace, then the turn's end

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputTransportMessageFrame) and self.enabled:
            msg = frame.message if isinstance(frame.message, dict) else {}
            if msg.get("type") == "ptt":
                await self._key(bool(msg.get("down")))
                return
        elif isinstance(frame, InputAudioRawFrame) and self.enabled:
            if not (self.held or self._closing is not None):
                frame.audio = bytes(len(frame.audio))
            if self.vad is not None:
                if self.vad.sample_rate != frame.sample_rate:
                    self.vad.set_sample_rate(frame.sample_rate)
                state = await self.vad.analyze_audio(frame.audio)
                if self.held and not self.speech and state == VADState.SPEAKING:
                    # Whisper's buffer keeps the second before this, so the onset isn't lost.
                    await self._start_segment()
        elif isinstance(frame, (EndFrame, CancelFrame)) and self._closing is not None:
            await self.cancel_task(self._closing)
            self._closing = None
        await self.push_frame(frame, direction)

    async def _key(self, down: bool) -> None:
        if down == self.held:
            return
        self.held = down
        if down:
            if self._closing is not None:  # pressed again within the grace: end that turn now
                await self.cancel_task(self._closing)
                await self._end_segment()
            await self.push_frame(ProposedUserStartedSpeakingFrame())  # interrupts Jarvis
            if self.vad is None:
                await self._start_segment()
        else:
            self._closing = self.create_task(self._release(), "ptt-release")

    async def _release(self) -> None:
        # The last word is still in flight from the client; let it in first.
        await asyncio.sleep(self.grace_secs)
        await self._end_segment()

    async def _start_segment(self) -> None:
        self.speech = True
        await self.push_frame(VADUserStartedSpeakingFrame())

    async def _end_segment(self) -> None:
        self._closing = None
        if self.speech:
            self.speech = False
            await self.push_frame(VADUserStoppedSpeakingFrame())
        await self.push_frame(ProposedUserStoppedSpeakingFrame())


def last_user_text(context) -> str:
    for message in reversed(context.get_messages()):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
    return ""


class CoreLLM(FrameProcessor):
    """Stands where an LLM service would: sends the finished user turn to the
    Core and streams the reply text on to the TTS.

    `core` needs ask(text), interrupt() and events() (an async iterator of the
    Core's broadcast events); see voice.core_client.CoreClient.
    """

    def __init__(self, core, notify: Send | None = None, **kwargs):
        super().__init__(**kwargs)
        self.core = core
        self.notify = notify
        self._expect: str | None = None  # asked, waiting for the Core's "start"
        self._active = False  # streaming a voice reply
        self._reader: asyncio.Task | None = None
        self._asked_len = 0  # context length when we last asked
        self.sent_at: float | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            if self._reader is None:
                self._reader = self.create_task(self._read_core(), "core-reader")
        elif isinstance(frame, (EndFrame, CancelFrame)):
            if self._reader is not None:
                await self.cancel_task(self._reader)
                self._reader = None
            await self.push_frame(frame, direction)
        elif isinstance(frame, LLMContextFrame):
            messages = frame.context.get_messages()
            # Only a new user message is a question; never re-ask an old one.
            new = len(messages) > self._asked_len and isinstance(messages[-1], dict)                 and messages[-1].get("role") == "user"
            if new and not frame.speculation:
                text = last_user_text(frame.context)
                if text:
                    self._asked_len = len(messages)
                    await self._ask(text)
        elif isinstance(frame, InterruptionFrame):
            await self._stop_reply()
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)

    async def _ask(self, text: str) -> None:
        if self._active:
            await self._stop_reply()
        self._expect = text
        self.sent_at = time.monotonic()
        await self.core.ask(text)

    async def _stop_reply(self) -> None:
        """Talking over Jarvis: tell the Core to stop the reply in progress."""
        if self._active or self._expect is not None:
            self._active = False
            self._expect = None
            await self.core.interrupt()

    async def _read_core(self) -> None:
        async for event in self.core.events():
            await self.handle_core_event(event)

    async def handle_core_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "start":
            if event.get("source") == "voice" and self._expect is not None and event.get("text") == self._expect:
                self._expect = None
                self._active = True
                await self.push_frame(LLMFullResponseStartFrame())
            elif self._active:  # someone typed: the Core cut this reply off
                await self._end_reply()
        elif kind == "delta" and self._active:
            await self.push_frame(LLMTextFrame(text=event.get("text", "")))
        elif kind == "tool" and self._active:
            await self._notify({"type": "tool", "name": event.get("name")})
        elif kind in ("done", "interrupted", "error") and self._active:
            if kind == "error":
                await self.push_frame(LLMTextFrame(text=" " + ERROR_REPLY))
            await self._end_reply()
            await self._notify({"type": "core_turn", "status": kind,
                                "first_token_ms": event.get("first_token_ms"),
                                "total_ms": event.get("total_ms")})
        elif kind == "approval_request":
            await self._notify({"type": "approval_request", "tool": event["approval"].get("tool")})
        elif kind == "core_disconnected":
            if self._active or self._expect is not None:
                self._expect = None
                if not self._active:
                    await self.push_frame(LLMFullResponseStartFrame())
                    self._active = True
                await self.push_frame(LLMTextFrame(text="I lost the connection to my core."))
                await self._end_reply()
            await self._notify({"type": "core_disconnected"})

    async def _end_reply(self) -> None:
        self._active = False
        await self.push_frame(LLMFullResponseEndFrame())

    async def _notify(self, message: dict) -> None:
        if self.notify is not None:
            await self.notify(message)


class TurnTimer(BaseObserver):
    """Per-turn timings and voice states, sent to the client's debug panel.

    All times are measured from the end of Deval's speech: the talk key's
    release in push-to-talk, or the VAD's estimate in hands-free mode.

    - detect: until the turn was judged finished
    - transcribe: until Whisper's text arrived
    - first_token: from sending to the Core until Claude's first text
    - first_audio: until Jarvis's voice started (the target is 2.5 s)
    - total: until Jarvis stopped speaking
    - interrupt_stop: from the interruption until the audio stopped (target 300 ms)
    """

    def __init__(self, send: Send, mode: str, vad_stop_secs: float, **kwargs):
        super().__init__(**kwargs)
        self.send = send
        self.mode = mode
        self.vad_stop_secs = vad_stop_secs
        self._seen: set[int] = set()
        self.bot_speaking = False
        self._reset()
        self.last: dict | None = None

    def _reset(self) -> None:
        self.speech_end: float | None = None
        self.marks: dict[str, float] = {}
        self.interrupt_at: float | None = None

    def _mark(self, name: str, t: float) -> None:
        if self.speech_end is not None and name not in self.marks:
            self.marks[name] = t

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        # Broadcast frames (speaking, VAD) are separate copies up and down; count one.
        if data.direction != FrameDirection.DOWNSTREAM or frame.id in self._seen:
            return
        self._seen.add(frame.id)
        if len(self._seen) > 5000:
            self._seen.clear()
        t = data.timestamp / 1e9

        if isinstance(frame, UserStartedSpeakingFrame):
            if self.bot_speaking:
                self.interrupt_at = t
            self.speech_end = None
            self.marks = {}
            await self.send({"type": "state", "state": "listening"})
        elif isinstance(frame, InputTransportMessageFrame) and self.mode == PTT:
            msg = frame.message if isinstance(frame.message, dict) else {}
            if msg.get("type") == "ptt" and not msg.get("down"):
                self.speech_end = t
        elif isinstance(frame, VADUserStoppedSpeakingFrame) and self.mode == HANDS_FREE:
            self.speech_end = t - self.vad_stop_secs
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._mark("detect", t)
            await self.send({"type": "state", "state": "thinking"})
        elif isinstance(frame, TranscriptionFrame):
            self._mark("transcribe", t)
            await self.send({"type": "heard", "text": frame.text})
        elif isinstance(frame, LLMContextFrame):
            self._mark("sent", t)
        elif isinstance(frame, LLMTextFrame):
            self._mark("first_token", t)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            self._mark("first_audio", t)
            await self.send({"type": "state", "state": "speaking"})
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
            report: dict = {}
            if self.interrupt_at is not None:
                report["interrupt_stop"] = round((t - self.interrupt_at) * 1000)
                self.interrupt_at = None
            elif self.speech_end is not None:
                self.marks["total"] = t
                report.update(self.report())
            if report:
                self.last = report
                await self.send({"type": "timings", **report})
            await self.send({"type": "state", "state": "idle"})

    def report(self) -> dict:
        out = {}
        for name in ("detect", "transcribe", "first_audio", "total"):
            if name in self.marks:
                out[name] = round((self.marks[name] - self.speech_end) * 1000)
        if "sent" in self.marks and "first_token" in self.marks:
            out["first_token"] = round((self.marks["first_token"] - self.marks["sent"]) * 1000)
        return out
