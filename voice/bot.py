"""Assemble one connection's pipeline."""

from __future__ import annotations

import os

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import OutputTransportMessageUrgentFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_start import ExternalUserTurnStartStrategy
from pipecat.turns.user_stop import ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

from voice import models
from voice.pipeline import PTT, CoreLLM, PushToTalkGate, TurnTimer

VAD_STOP_SECS = 0.2
# Hands-free: how long you must speak before Jarvis treats it as talking over it.
# Lower is a quicker interruption, but its own voice from the speakers is more
# likely to cut it off (the echo test).
VAD_START_SECS = float(os.environ.get("JARVIS_VAD_START_SECS", "0.2"))


def turn_strategies(mode: str) -> UserTurnStrategies:
    if mode == PTT:
        # The talk key decides; once released, wait only briefly for Whisper's text.
        return UserTurnStrategies(
            start=[ExternalUserTurnStartStrategy()],
            stop=[ExternalUserTurnStopStrategy(timeout=0.1)],
        )
    return UserTurnStrategies()  # Silero VAD to start, Smart Turn v3 to stop


def build_task(connection, core, mode: str) -> PipelineTask:
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    task: PipelineTask | None = None

    async def notify(message: dict) -> None:
        if task is not None:
            await task.queue_frame(OutputTransportMessageUrgentFrame(message={"jarvis": True, **message}))

    pair = LLMContextAggregatorPair(
        LLMContext(),
        user_params=LLMUserAggregatorParams(
            # In push-to-talk the gate marks speech itself, one segment per key hold.
            vad_analyzer=None if mode == PTT else SileroVADAnalyzer(params=VADParams(start_secs=VAD_START_SECS, stop_secs=VAD_STOP_SECS)),
            user_turn_strategies=turn_strategies(mode),
            # A turn with no words (a cough, a press just to interrupt) gets no reply.
            empty_user_turn=None,
        ),
    )
    markdown = MarkdownTextFilter()

    async def plain_speech(text: str, _type) -> str:
        return await markdown.filter(text)

    pipeline = Pipeline([
        transport.input(),
        PushToTalkGate(
            enabled=mode == PTT,
            vad=SileroVADAnalyzer(params=VADParams(start_secs=0.1)) if mode == PTT else None,
        ),
        models.make_stt(),
        pair.user(),
        CoreLLM(core, notify),
        models.make_tts(text_transforms=[("*", plain_speech)]),
        transport.output(),
        pair.assistant(),
    ])
    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True),
        enable_rtvi=False,
        observers=[TurnTimer(notify, mode, VAD_STOP_SECS)],
    )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await task.cancel()

    return task
