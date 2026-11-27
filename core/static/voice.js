// Jarvis voice client: connects this page's mic and speaker to the voice service
// (WebRTC), push-to-talk or hands-free. Used by the chat page.
(() => {
  const VOICE_URL = "http://127.0.0.1:8766";
  const listeners = { state: [], timings: [], log: [], mic: [] };
  let pc = null, dc = null, mic = null, ping = null, held = false;
  let mode = localStorage.getItem("voiceMode") || "ptt";
  let state = "off";  // off | connecting | idle | listening | thinking | speaking
  let audioCtx = null, analyser = null, micAnalyser = null, tickTimer = null;
  const audioEl = new Audio();
  audioEl.autoplay = true;
  // Chosen devices ("" = the system default), remembered in this browser.
  let micId = localStorage.getItem("micDevice") || "";
  let speakerId = localStorage.getItem("speakerDevice") || "";

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
    audioCtx = audioCtx || newAudioContext();
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
    if (pc) disconnect(true);
    clearTimeout(retry);
    setState("connecting");
    try {
      mic = await openMic();
      audioCtx = audioCtx || newAudioContext();
      micAnalyser = audioCtx.createAnalyser(); micAnalyser.fftSize = 512;
      audioCtx.createMediaStreamSource(mic).connect(micAnalyser);
      pc = new RTCPeerConnection();
      pc.addTransceiver(mic.getAudioTracks()[0], { direction: "sendrecv" });
      pc.addTransceiver("video", { direction: "sendrecv" });  // the server expects these slots
      pc.addTransceiver("video", { direction: "sendrecv" });
      pc.ontrack = (e) => {
        if (e.track.kind !== "audio") return;
        const stream = new MediaStream([e.track]);
        audioEl.srcObject = stream;
        applySpeaker();
        audioCtx = audioCtx || newAudioContext();
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
        lastError = null;
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
      const msg = "voice: " + (e.name === "TypeError" ? "the voice service isn't running; retrying"
        : e.name === "NotFoundError" ? "no microphone found; connect one and it will retry"
        : e.name === "NotAllowedError" ? "microphone blocked for this page (allow it in Chrome's site settings)"
        : e.message);
      if (msg !== lastError) log(msg);  // once per problem, not on every retry
      lastError = msg;
      disconnect();
    }
  }

  // ---- device selection ----
  const MIC_OPTS = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  async function openMic() {
    if (micId) {
      try {
        return await navigator.mediaDevices.getUserMedia({ audio: { ...MIC_OPTS, deviceId: { exact: micId } } });
      } catch (e) {
        if (e.name !== "OverconstrainedError" && e.name !== "NotFoundError") throw e;
        log("voice: the chosen microphone isn't connected; using the default one");
      }
    }
    return navigator.mediaDevices.getUserMedia({ audio: MIC_OPTS });
  }
  function newAudioContext() {
    const ctx = new AudioContext();
    if (speakerId && ctx.setSinkId) ctx.setSinkId(speakerId).catch(() => {});
    return ctx;
  }
  async function applySpeaker() {
    try {
      if (audioEl.setSinkId) await audioEl.setSinkId(speakerId);
      if (audioCtx?.setSinkId) await audioCtx.setSinkId(speakerId);
    } catch (e) {
      log("voice: the chosen speaker isn't available; using the default one");
      try { await audioEl.setSinkId(""); if (audioCtx?.setSinkId) await audioCtx.setSinkId(""); } catch {}
    }
  }
  async function devices() {
    const all = await navigator.mediaDevices.enumerateDevices();
    const pick = (kind) => all.filter((d) => d.kind === kind && d.deviceId !== "default" && d.deviceId !== "communications")
      .map((d, i) => ({ id: d.deviceId, label: d.label || `${kind === "audioinput" ? "Microphone" : "Speaker"} ${i + 1}` }));
    return { mics: pick("audioinput"), speakers: pick("audiooutput"), mic: micId, speaker: speakerId,
             canPickSpeaker: !!audioEl.setSinkId };
  }
  async function setMic(id) {
    micId = id || ""; localStorage.setItem("micDevice", micId);
    if (pc) connect();  // reconnect with the new mic (same mode)
  }
  async function setSpeaker(id) {
    speakerId = id || ""; localStorage.setItem("speakerDevice", speakerId);
    await applySpeaker();
  }
  function testSpeaker() {
    audioCtx = audioCtx || newAudioContext();
    audioCtx.resume();
    const t = audioCtx.currentTime;
    [660, 880].forEach((f, i) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain(), s = t + i * 0.18;
      o.frequency.value = f; g.gain.setValueAtTime(0.0001, s);
      g.gain.exponentialRampToValueAtTime(0.15, s + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, s + 0.16);
      o.connect(g).connect(audioCtx.destination); o.start(s); o.stop(s + 0.17);
    });
  }

  // If the link drops while voice is meant to be on (the voice service restarted,
  // or wasn't up yet), try again every few seconds.
  let retry = null, lastError = null;
  function scheduleRetry() {
    clearTimeout(retry);
    if (localStorage.getItem("voiceOn") === "1") retry = setTimeout(() => { if (!pc) connect(); }, 4000);
  }

  function disconnect(byUser = false) {
    clearTimeout(retry); retry = null;
    clearInterval(ping); ping = null;
    if (held) { held = false; send({ type: "ptt", down: false }); }
    try { dc?.close(); } catch {}
    try { pc?.close(); } catch {}
    mic?.getTracks().forEach((t) => t.stop());  // the mic light goes off
    pc = dc = mic = micAnalyser = null;
    setState("off");
    if (!byUser) scheduleRetry();
  }

  // Mute turns the microphone off completely (not just gated), and remembers it.
  function setMuted(muted) {
    localStorage.setItem("voiceOn", muted ? "0" : "1");
    if (muted) disconnect(true); else connect();
  }

  // Loudness 0..1 of your mic and of Jarvis's voice (for the orb).
  const buf = new Float32Array(512);
  function level(a) {
    if (!a) return 0;
    a.getFloatTimeDomainData(buf);
    let sum = 0; for (const x of buf) sum += x * x;
    return Math.min(1, Math.sqrt(sum / buf.length) * 6);
  }

  // Frequency bins 0..255 of your mic and of Jarvis's voice (for the orb's voice ring).
  const micBins = new Uint8Array(256), outBins = new Uint8Array(256);
  function spectrum() {
    if (micAnalyser && (held || mode === "hands-free")) micAnalyser.getByteFrequencyData(micBins); else micBins.fill(0);
    if (analyser) analyser.getByteFrequencyData(outBins); else outBins.fill(0);
    return { mic: micBins, out: outBins };
  }

  window.jarvisVoice = {
    spectrum,
    levels: () => ({ mic: held || mode === "hands-free" ? level(micAnalyser) : 0, out: level(analyser) }),
    connect, disconnect, ptt, setMuted,
    devices, setMic, setSpeaker, testSpeaker,
    onDevicesChanged: (f) => navigator.mediaDevices.addEventListener("devicechange", f),
    toggleMute: () => setMuted(micState() !== "off"),
    get mode() { return mode; },
    get state() { return state; },
    get micState() { return micState(); },
    get wantsOn() { return localStorage.getItem("voiceOn") !== "0"; },
    on: (kind, f) => listeners[kind].push(f),
  };
})();
