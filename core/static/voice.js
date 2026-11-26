// Jarvis voice client: connects this page's mic and speaker to the voice service
// (WebRTC), push-to-talk or hands-free. Used by the chat page, which the desktop
// app shows; the app drives it through window.jarvisVoice (hotkeys, tray mute).
(() => {
  const VOICE_URL = "http://127.0.0.1:8766";
  const listeners = { state: [], timings: [], log: [], mic: [] };
  let pc = null, dc = null, mic = null, ping = null, held = false;
  let mode = localStorage.getItem("voiceMode") || "ptt";
  let state = "off";  // off | connecting | idle | listening | thinking | speaking
  let audioCtx = null, analyser = null, tickTimer = null;
  const audioEl = new Audio();
  audioEl.autoplay = true;

  const emit = (kind, value) => listeners[kind].forEach((f) => f(value));
  const log = (text) => emit("log", text);

  function setState(s) {
    const wasSpeaking = state === "speaking";
    if (s === "listening" && wasSpeaking && mode !== "ptt") measureStop();  // talked over Jarvis
    state = s;
    thinking(s === "thinking");
    emit("state", s);
    emit("mic", micState());
  }

  // "off", "ptt" (mic open, counts only while the key is held) or "hands-free".
  const micState = () => (pc && state !== "connecting" ? mode : "off");

  // A soft tick while Claude is thinking, so silence doesn't feel like a hang.
  function thinking(on) {
    clearInterval(tickTimer); tickTimer = null;
    if (!on || localStorage.getItem("voiceTick") === "off") return;
    audioCtx = audioCtx || new AudioContext();
    const tick = () => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain(), t = audioCtx.currentTime;
      o.frequency.value = 880; g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.05, t + 0.01); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
      o.connect(g).connect(audioCtx.destination); o.start(t); o.stop(t + 0.13);
    };
    tick(); tickTimer = setInterval(tick, 900);
  }

  // How long Jarvis's voice keeps going after an interruption, as heard here.
  function measureStop() {
    if (!analyser) return;
    const t0 = performance.now(), buf = new Float32Array(analyser.fftSize);
    let quietSince = null;
    const timer = setInterval(() => {
      analyser.getFloatTimeDomainData(buf);
      const rms = Math.sqrt(buf.reduce((a, x) => a + x * x, 0) / buf.length);
      const now = performance.now();
      if (rms > 0.01) quietSince = null; else if (quietSince === null) quietSince = now;
      if (quietSince !== null && now - quietSince > 150) {
        clearInterval(timer);
        emit("timings", { heard_stop: Math.round(quietSince - t0) });
      } else if (now - t0 > 5000) clearInterval(timer);
    }, 10);
  }

  function onMessage(raw) {
    let m; try { m = JSON.parse(raw); } catch { return; }
    if (m.type === "signalling") { if (m.message?.type === "peerLeft") disconnect(); return; }
    if (!m.jarvis) return;
    if (m.type === "state") setState(m.state);
    else if (m.type === "timings") { const { jarvis, type, ...t } = m; emit("timings", t); }
    else if (m.type === "heard") log("heard: " + m.text.trim());
    else if (m.type === "core_disconnected") log("voice lost the Core; retrying");
  }

  function send(obj) { if (dc && dc.readyState === "open") dc.send(JSON.stringify(obj)); }

  function ptt(down) {
    if (!pc || mode !== "ptt" || down === held) return;
    held = down;
    if (down && state === "speaking") measureStop();
    send({ type: "ptt", down });
    emit("state", down ? "listening" : state);
  }

  async function connect(newMode) {
    if (newMode) { mode = newMode; localStorage.setItem("voiceMode", mode); }
    if (pc) disconnect();
    setState("connecting");
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      pc = new RTCPeerConnection();
      pc.addTransceiver(mic.getAudioTracks()[0], { direction: "sendrecv" });
      pc.addTransceiver("video", { direction: "sendrecv" });  // the server expects these slots
      pc.addTransceiver("video", { direction: "sendrecv" });
      pc.ontrack = (e) => {
        if (e.track.kind !== "audio") return;
        const stream = new MediaStream([e.track]);
        audioEl.srcObject = stream;
        audioCtx = audioCtx || new AudioContext();
        analyser = audioCtx.createAnalyser(); analyser.fftSize = 512;
        audioCtx.createMediaStreamSource(stream).connect(analyser);
      };
      pc.onconnectionstatechange = () => {
        if (["failed", "closed", "disconnected"].includes(pc?.connectionState)) disconnect();
      };
      dc = pc.createDataChannel("chat", { ordered: true });
      dc.onmessage = (e) => onMessage(e.data);
      dc.onopen = () => {
        ping = setInterval(() => dc?.readyState === "open" && dc.send("ping: " + Date.now()), 1000);
        setState("idle");
        localStorage.setItem("voiceOn", "1");
      };
      await pc.setLocalDescription(await pc.createOffer());
      await new Promise((resolve) => {
        if (pc.iceGatheringState === "complete") return resolve();
        pc.onicegatheringstatechange = () => pc.iceGatheringState === "complete" && resolve();
        setTimeout(resolve, 2000);
      });
      const r = await fetch(VOICE_URL + "/api/offer", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type,
                               request_data: { mode } }),
      });
      if (!r.ok) throw new Error("voice service refused: " + r.status);
      await pc.setRemoteDescription(await r.json());
    } catch (e) {
      log("voice: " + (e.name === "TypeError" ? "the voice service isn't running" : e.message));
      disconnect();
    }
  }

  function disconnect() {
    clearInterval(ping); ping = null;
    if (held) { held = false; send({ type: "ptt", down: false }); }
    try { dc?.close(); } catch {}
    try { pc?.close(); } catch {}
    mic?.getTracks().forEach((t) => t.stop());  // the mic light goes off
    pc = dc = mic = null;
    setState("off");
  }

  // Mute turns the microphone off completely (not just gated), and remembers it.
  function setMuted(muted) {
    localStorage.setItem("voiceOn", muted ? "0" : "1");
    if (muted) disconnect(); else connect();
  }

  window.jarvisVoice = {
    connect, disconnect, ptt, setMuted,
    toggleMute: () => setMuted(micState() !== "off"),
    get mode() { return mode; },
    get state() { return state; },
    get micState() { return micState(); },
    get wantsOn() { return localStorage.getItem("voiceOn") !== "0"; },
    on: (kind, f) => listeners[kind].push(f),
  };
})();
