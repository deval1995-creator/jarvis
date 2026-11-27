"""The voice pipeline's own pieces, with a fake or a real (FakeBackend) Core.

No speech models load here: STT/TTS are Pipecat's, and exercised live.
"""

import asyncio

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InputTransportMessageFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame, run_test

from tests.test_live_server import TOKEN, server  # noqa: F401  (fixture)
from voice.core_client import CoreClient
from voice.pipeline import ERROR_REPLY, CoreLLM, PushToTalkGate, TurnTimer, last_user_text


def context_frame(text: str) -> LLMContextFrame:
    return LLMContextFrame(context=LLMContext(messages=[{"role": "user", "content": text}]))


def audio(n=320) -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=b"\x01\x02" * (n // 2), sample_rate=16000, num_channels=1)


class FakeCore:
    """Answers each ask with the given events, as the Core would broadcast them."""

    def __init__(self, reply=("start", "Hi", " there.", "done")):
        self.reply = reply
        self.asked: list[str] = []
        self.interrupts = 0
        self.queue: asyncio.Queue = asyncio.Queue()

    async def ask(self, text):
        self.asked.append(text)
        for item in self.reply:
            if item == "start":
                self.queue.put_nowait({"type": "start", "source": "voice", "text": text})
            elif item in ("done", "interrupted", "error"):
                self.queue.put_nowait({"type": item, "first_token_ms": 5, "total_ms": 9})
            elif isinstance(item, dict):
                self.queue.put_nowait(item)
            else:
                self.queue.put_nowait({"type": "delta", "text": item})

    async def interrupt(self):
        self.interrupts += 1

    async def events(self):
        while True:
            yield await self.queue.get()


# ----- push-to-talk -----


def test_ptt_gate_one_segment_and_turn_per_key_hold():
    down, _ = asyncio.run(run_test(
        PushToTalkGate(enabled=True, grace_secs=0.05),
        frames_to_send=[
            audio(),
            InputTransportMessageFrame(message={"type": "ptt", "down": True}),
            audio(),
            InputTransportMessageFrame(message={"type": "ptt", "down": False}),
            audio(),  # inside the release grace: still heard
            SleepFrame(sleep=0.2),
            audio(),  # after: silence
        ],
    ))
    markers = [type(f) for f in down if not isinstance(f, InputAudioRawFrame)]
    assert markers == [ProposedUserStartedSpeakingFrame, VADUserStartedSpeakingFrame,
                       VADUserStoppedSpeakingFrame, ProposedUserStoppedSpeakingFrame]
    sounds = [f.audio != bytes(320) for f in down if isinstance(f, InputAudioRawFrame)]
    assert sounds == [False, True, True, False]


class FakeVAD:
    """Hears speech in any non-silent audio."""

    sample_rate = 16000

    def set_sample_rate(self, rate):
        self.sample_rate = rate

    async def analyze_audio(self, buffer):
        return VADState.SPEAKING if any(buffer) else VADState.QUIET


def test_ptt_press_without_speech_sends_nothing_to_whisper():
    silent = InputAudioRawFrame(audio=bytes(320), sample_rate=16000, num_channels=1)
    down, _ = asyncio.run(run_test(
        PushToTalkGate(enabled=True, vad=FakeVAD(), grace_secs=0.05),
        frames_to_send=[
            InputTransportMessageFrame(message={"type": "ptt", "down": True}),  # just to interrupt
            silent,
            InputTransportMessageFrame(message={"type": "ptt", "down": False}),
            SleepFrame(sleep=0.2),
        ],
    ))
    markers = [type(f) for f in down if not isinstance(f, InputAudioRawFrame)]
    assert markers == [ProposedUserStartedSpeakingFrame, ProposedUserStoppedSpeakingFrame]


def test_ptt_segment_opens_when_speech_is_heard():
    down, _ = asyncio.run(run_test(
        PushToTalkGate(enabled=True, vad=FakeVAD(), grace_secs=0.05),
        frames_to_send=[
            InputTransportMessageFrame(message={"type": "ptt", "down": True}),
            audio(),
            InputTransportMessageFrame(message={"type": "ptt", "down": False}),
            SleepFrame(sleep=0.2),
        ],
    ))
    markers = [type(f) for f in down if not isinstance(f, InputAudioRawFrame)]
    assert markers == [ProposedUserStartedSpeakingFrame, VADUserStartedSpeakingFrame,
                       VADUserStoppedSpeakingFrame, ProposedUserStoppedSpeakingFrame]


def test_ptt_gate_off_in_hands_free():
    down, _ = asyncio.run(run_test(PushToTalkGate(enabled=False), frames_to_send=[audio()]))
    assert down[0].audio != bytes(320)


# ----- the Core as the LLM -----


def test_last_user_text():
    ctx = LLMContext(messages=[{"role": "user", "content": "first"}, {"role": "assistant", "content": "x"},
                               {"role": "user", "content": [{"type": "text", "text": "second"}]}])
    assert last_user_text(ctx) == "second"


def test_core_llm_streams_reply_to_tts():
    core = FakeCore()
    down, _ = asyncio.run(run_test(
        CoreLLM(core),
        frames_to_send=[context_frame("What's on today?"), SleepFrame(sleep=0.2)],
    ))
    assert core.asked == ["What's on today?"]
    texts = [f.text for f in down if isinstance(f, LLMTextFrame)]
    assert texts == ["Hi", " there."]
    kinds = [type(f) for f in down if not isinstance(f, LLMTextFrame)]
    assert kinds == [LLMFullResponseStartFrame, LLMFullResponseEndFrame]


def test_core_llm_never_reasks_an_old_question():
    core = FakeCore()
    frame = context_frame("What's on today?")
    again = LLMContextFrame(context=frame.context)  # e.g. a run after an empty turn
    asyncio.run(run_test(CoreLLM(core), frames_to_send=[frame, SleepFrame(sleep=0.2), again, SleepFrame(sleep=0.1)]))
    assert core.asked == ["What's on today?"]


def test_core_llm_ignores_typed_turns():
    core = FakeCore()
    core.queue.put_nowait({"type": "start", "source": "typed", "text": "hello"})
    core.queue.put_nowait({"type": "delta", "text": "Not for voice."})
    core.queue.put_nowait({"type": "done"})
    down, _ = asyncio.run(run_test(CoreLLM(core), frames_to_send=[SleepFrame(sleep=0.2)]))
    assert not [f for f in down if isinstance(f, (LLMTextFrame, LLMFullResponseStartFrame))]


def test_core_error_is_spoken():
    core = FakeCore(reply=("start", "error"))
    down, _ = asyncio.run(run_test(CoreLLM(core), frames_to_send=[context_frame("hi"), SleepFrame(sleep=0.2)]))
    assert ERROR_REPLY in "".join(f.text for f in down if isinstance(f, LLMTextFrame))


def test_talking_over_jarvis_interrupts_the_core():
    core = FakeCore(reply=("start", "A long answer"))  # no "done": still replying
    asyncio.run(run_test(
        CoreLLM(core),
        frames_to_send=[context_frame("Tell me a story"), SleepFrame(sleep=0.2), InterruptionFrame()],
    ))
    assert core.interrupts == 1


def test_interruption_with_nothing_playing_leaves_the_core_alone():
    core = FakeCore()
    asyncio.run(run_test(CoreLLM(core), frames_to_send=[InterruptionFrame()]))
    assert core.interrupts == 0


def test_tool_and_approval_events_reach_the_page():
    sent = []

    async def notify(m):
        sent.append(m)

    approval = {"type": "approval_request", "approval": {"tool": "mcp__claude_ai_Gmail__send_message"}}
    core = FakeCore(reply=("start", {"type": "tool", "name": "mcp__memory__recall"}, approval, "Done.", "done"))
    asyncio.run(run_test(CoreLLM(core, notify), frames_to_send=[context_frame("hi"), SleepFrame(sleep=0.2)]))
    kinds = [m["type"] for m in sent]
    assert kinds == ["tool", "approval_request", "core_turn"]


# ----- timings -----


def test_turn_timer_report():
    async def send(m):
        pass

    timer = TurnTimer(send, "ptt", 0.2)
    timer.speech_end = 10.0
    timer.marks = {"detect": 10.1, "transcribe": 10.3, "sent": 10.35, "first_token": 11.35,
                   "first_audio": 11.9, "total": 14.0}
    assert timer.report() == {"detect": 100, "transcribe": 300, "first_audio": 1900, "total": 4000,
                              "first_token": 1000}


# ----- against the real Core API (FakeBackend, no Claude) -----


def test_core_llm_through_real_core(server):  # noqa: F811
    async def run():
        core = CoreClient(f"ws://127.0.0.1:{server}/ws", TOKEN)
        await core.connect()
        try:
            return await run_test(CoreLLM(core), frames_to_send=[context_frame("hi"), SleepFrame(sleep=0.5)])
        finally:
            await core.close()

    down, _ = asyncio.run(run())
    assert "".join(f.text for f in down if isinstance(f, LLMTextFrame)) == "Hello, Deval."


def test_core_client_rejects_bad_token(server):  # noqa: F811
    with pytest.raises(Exception):
        asyncio.run(CoreClient(f"ws://127.0.0.1:{server}/ws", "wrong").connect())


# ----- the voice service's HTTP surface (no models, no WebRTC) -----


def voice_client():
    from fastapi.testclient import TestClient

    from voice.server import create_app

    return TestClient(create_app(preload=False), base_url="http://127.0.0.1:8766")


def test_chat_page_may_call_the_voice_service():
    with voice_client() as c:
        r = c.options("/api/offer", headers={"Origin": "http://127.0.0.1:8765",
                                             "Access-Control-Request-Method": "POST",
                                             "Access-Control-Request-Headers": "content-type"})
        assert r.status_code == 200
        assert r.headers["access-control-allow-origin"] == "http://127.0.0.1:8765"


def test_other_sites_may_not():
    with voice_client() as c:
        r = c.post("/api/offer", json={"sdp": "x", "type": "offer"}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = c.options("/api/offer", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert "access-control-allow-origin" not in r.headers


def test_say_needs_jarvis_token(tmp_path, monkeypatch):
    import voice.server as vs

    monkeypatch.setattr(vs, "read_token", lambda d: "secret-token")
    with voice_client() as c:
        assert c.post("/api/say", json={"text": "hi"}).status_code == 401
        assert c.post("/api/say", json={"text": "hi"}, headers={"X-Jarvis-Token": "wrong"}).status_code == 401
        r = c.post("/api/say", json={"text": "hi"}, headers={"X-Jarvis-Token": "secret-token"})
        assert r.status_code == 200 and r.json() == {"spoken": False, "listeners": 0}  # nobody connected


def test_tts_returns_audio_for_jarvis_page_only(monkeypatch):
    import voice.models as vm

    monkeypatch.setattr(vm, "synthesize_wav", lambda text: b"RIFF....WAVE" + text.encode())
    with voice_client() as c:
        assert c.post("/api/tts", json={"text": "hi"}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.post("/api/tts", json={"text": "  "}).status_code == 422
        r = c.post("/api/tts", json={"text": "Good evening, sir."}, headers={"Origin": "http://127.0.0.1:8765"})
        assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content.endswith(b"sir.")
