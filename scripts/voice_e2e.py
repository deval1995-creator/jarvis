"""End-to-end voice check without a microphone.

Joins the voice service over WebRTC like the browser does, "speaks" questions
made with Kokoro (a different voice from Jarvis's), holds the talk key while
they play, records Jarvis's reply audio and prints the timing reports.

    uv run python scripts/voice_e2e.py [--url http://127.0.0.1:8766] [--interrupt]

With --interrupt it presses the talk key again while Jarvis is speaking and
reports how fast the audio stopped. Each question is one real Claude turn.
"""

from __future__ import annotations

import argparse
import asyncio
import fractions
import json
import time
import wave
from pathlib import Path

import aiohttp
import av
import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription

RATE = 48000
FRAME = 960  # 20 ms


def speech(text: str, voice: str = "am_michael") -> np.ndarray:
    from pipecat.services.kokoro.tts import KOKORO_CACHE_DIR
    from kokoro_onnx import Kokoro

    k = Kokoro(str(KOKORO_CACHE_DIR / "kokoro-v1.0.onnx"), str(KOKORO_CACHE_DIR / "voices-v1.0.bin"))
    samples, sr = k.create(text, voice=voice, lang="en-us")
    t = np.arange(0, len(samples), sr / RATE)
    resampled = np.interp(t, np.arange(len(samples)), samples)
    return (np.clip(resampled, -1, 1) * 32767 * 0.8).astype(np.int16)


class Mic(MediaStreamTrack):
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.pending = np.zeros(0, dtype=np.int16)
        self.pts = 0
        self.start = None

    def play(self, samples: np.ndarray) -> float:
        self.pending = np.concatenate([self.pending, samples])
        return len(samples) / RATE

    async def recv(self):
        if self.start is None:
            self.start = time.monotonic()
        wait = self.start + self.pts / RATE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        elif wait < -0.1:  # fell behind (the loop was busy): skip ahead, never burst
            self.start -= wait
        chunk, self.pending = self.pending[:FRAME], self.pending[FRAME:]
        if len(chunk) < FRAME:
            chunk = np.concatenate([chunk, np.zeros(FRAME - len(chunk), dtype=np.int16)])
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self.pts
        frame.time_base = fractions.Fraction(1, RATE)
        self.pts += FRAME
        return frame


async def main(url: str, interrupt: bool, out: Path, mode: str = "ptt") -> None:
    # The first question has a pause in the middle: it must still arrive as one turn.
    questions = ["Hey Jarvis ... in one sentence, what can you help me with?"]
    if interrupt:
        questions.append("Tell me a story about a lighthouse keeper, in about eight sentences.")
    voices = [speech(q) for q in questions]
    stop_words = speech("Okay, stop. That's enough.")  # before connecting: it blocks
    voices[0] = np.concatenate([speech("Hey Jarvis."), np.zeros(int(RATE * 0.8), dtype=np.int16),
                                speech("In one sentence, what can you help me with?")])
    pressed_at = None

    pc = RTCPeerConnection()
    mic = Mic()
    pc.addTrack(mic)
    pc.addTransceiver("video", direction="sendrecv")
    pc.addTransceiver("video", direction="sendrecv")
    dc = pc.createDataChannel("chat", ordered=True)
    messages: list[dict] = []
    states: asyncio.Queue = asyncio.Queue()
    received: list[np.ndarray] = []
    arrivals: list[tuple[float, float]] = []  # (time, loudness) per received frame

    @dc.on("message")
    def on_message(raw):
        try:
            m = json.loads(raw)
        except ValueError:
            return
        if m.get("jarvis"):
            messages.append(m)
            if m["type"] == "state":
                states.put_nowait(m["state"])
            print("  <-", {k: v for k, v in m.items() if k != "jarvis"})

    @pc.on("track")
    def on_track(track):
        async def drain():
            while True:
                try:
                    f = await track.recv()
                except Exception:
                    return
                pcm = f.to_ndarray().reshape(-1)
                received.append(pcm)
                arrivals.append((time.monotonic(), float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))))
        if track.kind == "audio":
            asyncio.ensure_future(drain())

    await pc.setLocalDescription(await pc.createOffer())
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{url}/api/offer", json={
            "sdp": pc.localDescription.sdp, "type": pc.localDescription.type,
            "request_data": {"mode": mode},
        }) as r:
            r.raise_for_status()
            answer = await r.json()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    for _ in range(100):
        if dc.readyState == "open":
            break
        await asyncio.sleep(0.1)
    assert dc.readyState == "open", "data channel did not open"

    async def ping():
        while dc.readyState == "open":
            dc.send(f"ping: {time.time()}")
            await asyncio.sleep(1)
    asyncio.ensure_future(ping())
    await asyncio.sleep(1.0)

    async def wait_state(target: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while True:
            state = await asyncio.wait_for(states.get(), timeout=deadline - time.monotonic())
            if state == target:
                return

    def key(down: bool):
        if mode == "ptt":
            dc.send(json.dumps({"type": "ptt", "down": down}))


    for i, (q, v) in enumerate(zip(questions, voices)):
        print(f"\n-> saying: {q}")
        key(True)
        await asyncio.sleep(0.1)
        await asyncio.sleep(mic.play(v) + 0.05)
        key(False)
        await wait_state("speaking")
        if interrupt and i == len(questions) - 1:
            await asyncio.sleep(2.0)
            print("-> pressing talk to interrupt")
            pressed_at = time.monotonic()
            key(True)
            if mode != "ptt":  # hands-free: talk over Jarvis instead
                mic.play(stop_words)
            await wait_state("idle", 10)
            await asyncio.sleep(0.3 if mode == "ptt" else len(stop_words) / RATE)
            key(False)
            await asyncio.sleep(2.0)
            # The audio stopped where the first 100 ms of quiet after the press begins.
            after = [(t, rms) for t, rms in arrivals if t >= pressed_at]
            stop_ms = None
            for i in range(len(after) - 4):
                if all(rms < 200 for _, rms in after[i:i + 5]):
                    stop_ms = round((after[i][0] - pressed_at) * 1000)
                    break
            print(f"   heard audio stop {stop_ms} ms after the press (target 300)")
        else:
            await wait_state("idle")
            await asyncio.sleep(0.5)

    audio = np.concatenate(received) if received else np.zeros(0, dtype=np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.astype(np.int16).tobytes())
    await pc.close()
    print("\ntimings:")
    for m in messages:
        if m["type"] == "timings":
            print("  ", {k: v for k, v in m.items() if k not in ("jarvis", "type")})
    heard = [m["text"] for m in messages if m["type"] == "heard"]
    print("heard:", heard)
    print("reply audio saved to", out)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8766")
    p.add_argument("--interrupt", action="store_true")
    p.add_argument("--out", default="voice_e2e_reply.wav")
    p.add_argument("--mode", choices=["ptt", "hands-free"], default="ptt")
    a = p.parse_args()
    asyncio.run(main(a.url, a.interrupt, Path(a.out), a.mode))
