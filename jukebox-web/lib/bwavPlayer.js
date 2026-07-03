// Web Audio player for raw BWAV songs served by the kkfm jukebox engine.
// Replaces the acnh-jukebox <audio> element: the browser can't decode BWAV, so
// we fetch the compressed bytes and decode them here with lib/bwav.js (the
// bit-exact port of the radio engine's GC-ADPCM decoder), then play mono ->
// stereo through Web Audio with sample-accurate intro + gapless native loop.
//
// SAFETY — HARD LOCK. Corrupt or distorted audio must never reach the speaker.
// Defenses, in order:
//   1. Source integrity gate (lib/bwav validateSource): the downloaded bytes
//      must match the file's own CRC-32 and structural invariants, or nothing
//      decodes. A CRC pass + the deterministic clamped decoder == exact game
//      audio, by construction.
//   2. Post-decode PCM sanity (finite + in range, full sample count).
//   3. Runtime output watchdog: an analyser on the live output; any non-finite
//      or sustained rail-level sample instantly kills the graph.
// ANY failure calls hardLock(): all sources stop, the graph is disconnected,
// the AudioContext is closed, and the player latches — it will not play again
// until reset() (a deliberate user action), so a bad state can't "recover" into
// noise. The latch is the whole point: fail closed, silent, and stay there.
import { decodeToBuffers, BwavIntegrityError } from './bwav';

// Watchdog trips if this many consecutive output samples sit at/near full
// scale (square-wave / DC rail — the signature of decode corruption). Real
// music transients are far shorter. 4096 samples ≈ 85 ms at 48 kHz.
const RAIL_LEVEL = 0.999;
const RAIL_RUN_LIMIT = 4096;

export class BwavPlayer {
  constructor() {
    this.ctx = null;
    this.introSrc = null;
    this.loopSrc = null;
    this.gain = null;
    this.analyser = null;
    this.watchTimer = null;
    this.token = 0; // bumps on every play/stop so stale async loads are ignored
    this.volume = 1;
    this.locked = false;      // latched hard-lock; refuses playback while true
    this.lockReason = null;
    this._cache = new Map();  // url -> decoded buffers (only ever CRC-validated ones)
    this._railRun = 0;
    this._scratch = null;
  }

  _ensureContext() {
    if (!this.ctx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AC({ sampleRate: 48000 });
      this.gain = this.ctx.createGain();
      this.gain.gain.value = this.volume;
      // gain -> analyser -> destination, so the watchdog sees exactly what
      // would be heard.
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 2048;
      this.gain.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);
      this._scratch = new Float32Array(this.analyser.fftSize);
    }
    return this.ctx;
  }

  setVolume(v) {
    this.volume = v;
    if (this.gain && !this.locked) this.gain.gain.value = v;
  }

  _stopSources() {
    for (const s of [this.introSrc, this.loopSrc]) {
      if (s) { try { s.onended = null; s.stop(); } catch (_) {} }
    }
    this.introSrc = null;
    this.loopSrc = null;
  }

  _stopWatchdog() {
    if (this.watchTimer !== null) {
      clearInterval(this.watchTimer);
      this.watchTimer = null;
    }
  }

  // Irreversible (until reset) fail-closed stop. Silences and tears down
  // everything, then latches. Safe to call from anywhere, any number of times.
  hardLock(reason) {
    this.locked = true;
    this.lockReason = reason;
    this.token++;
    this._stopWatchdog();
    this._stopSources();
    try { if (this.gain) this.gain.gain.value = 0; } catch (_) {}
    try { if (this.gain) this.gain.disconnect(); } catch (_) {}
    try { if (this.analyser) this.analyser.disconnect(); } catch (_) {}
    try { if (this.ctx && this.ctx.state !== 'closed') this.ctx.close(); } catch (_) {}
    console.error('[bwav] HARD LOCK — audio killed:', reason);
    if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
      window.dispatchEvent(new CustomEvent('bwav-hardlock', { detail: { reason } }));
    }
  }

  // Deliberate recovery (e.g. a "reload" button). Drops the latch and rebuilds
  // a fresh context on the next play().
  reset() {
    this.locked = false;
    this.lockReason = null;
    this.ctx = null;
    this.gain = null;
    this.analyser = null;
    this.introSrc = null;
    this.loopSrc = null;
    this._railRun = 0;
  }

  pause() {
    if (this.locked) return;
    this.token++;
    this._stopWatchdog();
    this._stopSources();
  }

  _startWatchdog() {
    this._stopWatchdog();
    this._railRun = 0;
    // Poll the live output. Any non-finite sample, or a sustained rail-level
    // run, is corruption slipping past the pre-checks — kill instantly.
    this.watchTimer = setInterval(() => {
      if (!this.analyser || this.locked) return;
      const buf = this._scratch;
      this.analyser.getFloatTimeDomainData(buf);
      for (let i = 0; i < buf.length; i++) {
        const v = buf[i];
        if (!Number.isFinite(v) || v < -1.5 || v > 1.5) {
          this.hardLock(`runtime non-finite/out-of-range output (${v})`);
          return;
        }
        if (Math.abs(v) >= RAIL_LEVEL) {
          if (++this._railRun >= RAIL_RUN_LIMIT) {
            this.hardLock('runtime sustained rail-level output (distortion)');
            return;
          }
        } else {
          this._railRun = 0;
        }
      }
    }, 50);
  }

  _stereoBuffer(mono, rate) {
    const buf = this.ctx.createBuffer(2, mono.length, rate);
    buf.copyToChannel(mono, 0);
    buf.copyToChannel(mono, 1);
    return buf;
  }

  async play(url) {
    if (this.locked) {
      throw new BwavIntegrityError(`player hard-locked: ${this.lockReason}`);
    }

    const ctx = this._ensureContext();
    if (ctx.state === 'suspended') await ctx.resume();

    const myToken = ++this.token;
    this._stopWatchdog();
    this._stopSources();

    let decoded = this._cache.get(url);
    if (!decoded) {
      let ab;
      try {
        const resp = await fetch(url);
        if (!resp.ok) throw new BwavIntegrityError(`fetch ${url} -> ${resp.status}`);
        ab = await resp.arrayBuffer();
        // Guard against a truncated transfer even if the server lied about status.
        const declared = resp.headers.get('Content-Length');
        if (declared && Number(declared) !== ab.byteLength) {
          throw new BwavIntegrityError(
            `incomplete download: got ${ab.byteLength}, expected ${declared}`);
        }
        // decodeToBuffers runs the CRC + structural + PCM sanity gates and
        // throws BwavIntegrityError on any problem.
        decoded = decodeToBuffers(ab);
      } catch (err) {
        // Corruption/truncation/decoder fault -> fail closed. Never play.
        this.hardLock(err && err.message ? err.message : String(err));
        throw err;
      }
      this._cache.set(url, decoded);
    }
    // A newer play()/pause() happened while we were fetching/decoding — abort.
    if (myToken !== this.token || this.locked) return;

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
      loopSrc.start(startAt + intro.length / rate);
      this.loopSrc = loopSrc;
    }

    this._startWatchdog();
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
