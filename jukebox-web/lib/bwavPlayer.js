// Web Audio player for raw BWAV songs served by the kkfm jukebox engine.
// Replaces the acnh-jukebox <audio> element: the browser can't decode BWAV, so
// we fetch the compressed bytes and decode them here with lib/bwav.js (the
// bit-exact port of the radio engine's GC-ADPCM decoder), then play mono ->
// stereo through Web Audio with sample-accurate intro + gapless native loop.
import { decodeToBuffers } from './bwav';

export class BwavPlayer {
  constructor() {
    this.ctx = null;
    this.introSrc = null;
    this.loopSrc = null;
    this.gain = null;
    this.token = 0; // bumps on every play/stop so stale async loads are ignored
    this.volume = 1;
    this._cache = new Map(); // url -> {rate, intro, loop} decoded buffers
  }

  _ensureContext() {
    if (!this.ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC({ sampleRate: 48000 });
      this.gain = this.ctx.createGain();
      this.gain.gain.value = this.volume;
      this.gain.connect(this.ctx.destination);
    }
    return this.ctx;
  }

  setVolume(v) {
    this.volume = v;
    if (this.gain) this.gain.gain.value = v;
  }

  _stopSources() {
    for (const s of [this.introSrc, this.loopSrc]) {
      if (s) { try { s.onended = null; s.stop(); } catch (_) {} }
    }
    this.introSrc = null;
    this.loopSrc = null;
  }

  pause() {
    this.token++;
    this._stopSources();
  }

  // Build a stereo AudioBuffer from a mono Float32Array (duplicate to L/R).
  _stereoBuffer(mono, rate) {
    const buf = this.ctx.createBuffer(2, mono.length, rate);
    buf.copyToChannel(mono, 0);
    buf.copyToChannel(mono, 1);
    return buf;
  }

  async play(url) {
    const ctx = this._ensureContext();
    if (ctx.state === 'suspended') await ctx.resume();

    const myToken = ++this.token;
    this._stopSources();

    let decoded = this._cache.get(url);
    if (!decoded) {
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`song fetch failed: ${resp.status}`);
      const ab = await resp.arrayBuffer();
      decoded = decodeToBuffers(ab);
      this._cache.set(url, decoded);
    }
    // A newer play()/pause() happened while we were fetching/decoding — abort.
    if (myToken !== this.token) return;

    const { rate, intro, loop } = decoded;
    const startAt = ctx.currentTime + 0.03; // small lead so both sources arm together

    if (intro.length > 0) {
      const introSrc = ctx.createBufferSource();
      introSrc.buffer = this._stereoBuffer(intro, rate);
      introSrc.connect(this.gain);
      introSrc.start(startAt);
      this.introSrc = introSrc;
    }

    if (loop) {
      const loopBuf = this._stereoBuffer(loop, rate);
      const loopSrc = ctx.createBufferSource();
      loopSrc.buffer = loopBuf;
      loopSrc.loop = true;
      loopSrc.loopStart = 0;
      loopSrc.loopEnd = loop.length / rate;
      loopSrc.connect(this.gain);
      // Start the looping body exactly when the intro ends.
      loopSrc.start(startAt + intro.length / rate);
      this.loopSrc = loopSrc;
    }
  }
}

let shared = null;
export function getBwavPlayer() {
  if (!shared) {
    shared = new BwavPlayer();
    // Debug handle so playback can be inspected from the console / e2e checks.
    if (typeof window !== 'undefined') window.__kkfmPlayer = shared;
  }
  return shared;
}
