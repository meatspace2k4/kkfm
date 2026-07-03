// Bit-exact JS port of the kkfm radio engine's BWAV parser + GC-ADPCM decoder
// (acnh_aircheck_pi.cpp: BWAVParser::parse + Decoder). Same integer math, so
// the browser produces the identical PCM the Pi's radio does. ADPCM is stateful
// (each sample depends on the two prior outputs), so the whole compressed stream
// must arrive intact; we decode it here rather than trusting a lossy transcode.
//
// Layout (mono BWAV, little-endian):
//   0x00 "BWAV"        0x04 bom=0xFEFF        0x0E channels=1
//   channel-info block at data+0x10 (ci):
//     ci+0x04 rate u32        ci+0x08 total_samples u32
//     ci+0x10 8x(coef1 s16, coef2 s16)   (32 bytes)
//     ci+0x34 start u32       ci+0x38 loops u32
//     ci+0x3C loop_end u32    ci+0x40 loop_start u32
//     ci+0x46 hist1 s16       ci+0x48 hist2 s16
//   audio data at data+start; 8-byte blocks -> 14 samples each.

export const BLK_SAMP = 14;
export const BLK_BYTES = 8;

// Raised by every integrity gate. The player treats any of these as fatal:
// it hard-locks (kills audio, refuses to play) rather than risk distortion.
export class BwavIntegrityError extends Error {
  constructor(reason) {
    super(reason);
    this.name = 'BwavIntegrityError';
  }
}

function clamp16(v) {
  if (v < -32768) return -32768;
  if (v > 32767) return 32767;
  return v;
}

// Standard IEEE (zlib/PNG) CRC-32 — matches the value ACNH stores at BWAV
// offset 0x08 over the raw sample data. Table built once on first use.
let CRC_TABLE = null;
function crc32(u8, start, end) {
  if (!CRC_TABLE) {
    CRC_TABLE = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      CRC_TABLE[n] = c >>> 0;
    }
  }
  let c = 0xffffffff;
  for (let i = start; i < end; i++) c = CRC_TABLE[(c ^ u8[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

// Bytes of DSP-ADPCM for `n` samples (14 samples per 8-byte frame; the last
// partial frame is 1 header byte + ceil(remaining/2) nibble bytes).
function adpcmByteLen(n) {
  const full = Math.floor(n / 14);
  const rem = n % 14;
  const last = rem === 0 ? 0 : 1 + ((rem + 1) >> 1);
  return full * 8 + last;
}

export function parseBwav(buffer) {
  const d = new DataView(buffer);
  const u8 = new Uint8Array(buffer);
  if (u8[0] !== 0x42 || u8[1] !== 0x57 || u8[2] !== 0x41 || u8[3] !== 0x56) {
    throw new BwavIntegrityError('not a BWAV (bad magic)');
  }
  const bom = d.getUint16(4, true);
  const channels = d.getUint16(14, true);
  if (bom !== 0xfeff || channels !== 1) {
    throw new BwavIntegrityError(`unsupported BWAV (bom=${bom.toString(16)} channels=${channels})`);
  }
  const storedCrc = d.getUint32(8, true);
  const ci = 16;
  const codecId = d.getUint16(ci + 0, true);
  const rate = d.getUint32(ci + 4, true);
  const totalSamples = d.getUint32(ci + 8, true);
  const samplesThis = d.getUint32(ci + 12, true);

  const coeffs = [];
  for (let i = 0; i < 8; i++) {
    const off = ci + 16 + i * 4;
    coeffs.push([d.getInt16(off, true), d.getInt16(off + 2, true)]);
  }

  const start = d.getUint32(ci + 52, true);
  const loops = d.getUint32(ci + 56, true);
  const loopEnd = d.getUint32(ci + 60, true);
  const loopStart = d.getUint32(ci + 64, true);
  const hist1 = d.getInt16(ci + 70, true);
  const hist2 = d.getInt16(ci + 72, true);

  return {
    rate,
    codecId,
    totalSamples,
    samplesThis,
    coeffs,
    start,
    loops,
    loopEnd,
    loopStart,
    hist1,
    hist2,
    storedCrc,
    bytes: u8,                 // whole file, for CRC over the body
    raw: u8.subarray(start),   // audio data only
    rawLen: buffer.byteLength - start,
  };
}

// HARD integrity gate. Parses + verifies a downloaded BWAV against its own
// self-declared CRC-32 (over the audio body) and structural invariants. Throws
// BwavIntegrityError on ANY discrepancy — corruption, truncation, wrong codec.
// A pass means the bytes are exactly the game's, so the deterministic, clamped
// decoder can only produce the exact game audio (never distortion).
export function validateSource(arrayBuffer) {
  if (arrayBuffer.byteLength < 0x80) {
    throw new BwavIntegrityError(`file too small (${arrayBuffer.byteLength} bytes)`);
  }
  const ctx = parseBwav(arrayBuffer); // throws on bad magic/bom/channels
  if (ctx.codecId !== 1) {
    throw new BwavIntegrityError(`unsupported codec_id ${ctx.codecId} (expected DSP-ADPCM=1)`);
  }
  if (ctx.rate !== 48000 && ctx.rate !== 24000 && ctx.rate !== 4000) {
    throw new BwavIntegrityError(`implausible sample_rate ${ctx.rate}`);
  }
  if (ctx.samplesThis === 0 || ctx.samplesThis > 0x7fffffff) {
    throw new BwavIntegrityError(`implausible sample count ${ctx.samplesThis}`);
  }
  // The file must actually contain the ADPCM bytes it claims — a short read
  // (dropped chunk, cut connection) fails here before any decode.
  const bodyLen = adpcmByteLen(ctx.samplesThis);
  const bodyEnd = ctx.start + bodyLen;
  if (bodyEnd > arrayBuffer.byteLength) {
    throw new BwavIntegrityError(
      `truncated: need ${bodyEnd} bytes, have ${arrayBuffer.byteLength}`);
  }
  // The linchpin: recomputed CRC-32 over the audio body must equal the value
  // the file declares. One flipped byte anywhere in transit breaks this.
  const got = crc32(ctx.bytes, ctx.start, bodyEnd);
  if (got !== ctx.storedCrc) {
    throw new BwavIntegrityError(
      `CRC mismatch: computed 0x${got.toString(16)} != stored 0x${ctx.storedCrc.toString(16)}`);
  }
  return ctx;
}

// Streaming decoder that mirrors Decoder::decode_block. Pull one 14-sample block
// at a time so a worklet can decode lazily. Handles loop jump (with mid-block
// skip) and the post-loop fade exactly as the engine does.
export class BwavDecoder {
  // fadePlaySamples/fadeSamples > 0 enables the engine's loop-twice-then-fade
  // (play through, jump to loopStart, play the loop body once more over
  // fadePlaySamples, then ramp to zero over fadeSamples). Pass 0/0 to loop
  // forever with no fade.
  constructor(ctx, { fadePlaySamples = 0, fadeSamples = 0 } = {}) {
    this.ctx = ctx;
    this.ofs = 0;
    this.h1 = ctx.hist1;
    this.h2 = ctx.hist2;
    this.samplePos = 0;
    this.looped = false;
    this.postLoopPos = 0;
    this.playLim = fadePlaySamples;
    this.fadeLim = fadeSamples;
    this.fadingEnabled = fadePlaySamples > 0;
    this.loopable = ctx.loopEnd > 0 && ctx.loopStart !== 0xffffffff && ctx.loopEnd > ctx.loopStart;
  }

  isExhausted() {
    const { ctx } = this;
    if (this.fadingEnabled && this.looped && this.postLoopPos >= this.playLim + this.fadeLim) return true;
    if (!this.looped && this.ofs >= ctx.rawLen) {
      if (!this.loopable) return true;
    }
    return false;
  }

  // Decode the next block into `out` (a Float32Array of length >= 14 will be
  // written at [0..count)). Returns the sample count produced (0 = end).
  decodeBlock(out) {
    const rw = this.ctx.raw;
    const coeffs = this.ctx.coeffs;

    let hitLoop = this.loopable && this.samplePos >= this.ctx.loopEnd;
    if (this.ofs >= this.ctx.rawLen && this.loopable) hitLoop = true;

    let skip = 0;
    if (hitLoop) {
      const blkIdx = Math.floor(this.ctx.loopStart / BLK_SAMP);
      this.ofs = blkIdx * BLK_BYTES;
      this.h1 = this.ctx.hist1;
      this.h2 = this.ctx.hist2;
      skip = this.ctx.loopStart % BLK_SAMP;
      if (!this.looped) {
        this.looped = true;
        this.postLoopPos = 0;
      }
      this.samplePos = this.ctx.loopStart;
    }

    if (this.ofs >= this.ctx.rawLen && !this.loopable) return 0;

    const hdr = rw[this.ofs++];
    const scale = 1 << (hdr & 0x0f);
    const cIdx = (hdr >> 4) & 0x7;
    const c1 = coeffs[cIdx][0];
    const c2 = coeffs[cIdx][1];

    let outIdx = 0;
    const processSample = (n, idxInBlock) => {
      if (idxInBlock < skip) return false;
      // 4-bit sign extend: (n & 8) ? n-16 : n
      const sn = n - ((n & 8) << 1);
      const pred = ((sn * scale) << 11) + (c1 * this.h1 + c2 * this.h2) + 1024;
      const samp = clamp16(pred >> 11);
      this.h2 = this.h1;
      this.h1 = samp;

      let outSamp = samp;
      if (this.looped) {
        if (this.fadingEnabled) {
          if (this.postLoopPos >= this.playLim + this.fadeLim) {
            while (outIdx < 14) out[outIdx++] = 0;
            return true;
          }
          if (this.postLoopPos >= this.playLim) {
            const fadeIdx = this.postLoopPos - this.playLim;
            const num = this.fadeLim - fadeIdx;
            if (this.fadeLim > 0) {
              outSamp = Math.trunc((outSamp * num) / this.fadeLim);
            }
          }
        }
        this.postLoopPos++;
      }
      out[outIdx++] = outSamp;
      this.samplePos++;
      return false;
    };

    for (let i = 0; i < 7; i++) {
      if (this.loopable && this.samplePos >= this.ctx.loopEnd) return outIdx;
      if (this.ofs >= this.ctx.rawLen) return outIdx;
      const b = rw[this.ofs++];
      if (processSample((b >> 4) & 0xf, i * 2)) return outIdx;
      if (this.loopable && this.samplePos >= this.ctx.loopEnd) return outIdx;
      if (processSample(b & 0xf, i * 2 + 1)) return outIdx;
    }
    return outIdx;
  }
}

// Decode a BWAV into ready-to-play mono Float32 buffers, split at the loop so
// the player can schedule intro-then-loop with gapless native looping.
//   intro: samples [0, loopStart)      (continuous history)
//   loop:  one loop body [loopStart, loopEnd) decoded with the engine's loop
//          reset (Nintendo's stored loop history), i.e. exactly what plays on
//          every wrap — reset history is what makes the seam click-free.
// Non-looping songs return {intro: whole song, loop: null}.
//
// Runs the source integrity gate (CRC etc.) first, then a post-decode PCM
// sanity check, and throws BwavIntegrityError on any failure — so a caller that
// awaits this either gets known-good buffers or an exception, never bad audio.
export function decodeToBuffers(arrayBuffer) {
  const ctx = validateSource(arrayBuffer); // CRC / structure gate (throws)
  const loopable = ctx.loopEnd > 0 && ctx.loopStart !== 0xffffffff && ctx.loopEnd > ctx.loopStart;
  const toF32 = (i16arr) => {
    const f = new Float32Array(i16arr.length);
    for (let i = 0; i < f.length; i++) f[i] = i16arr[i] / 32768;
    return f;
  };

  const block = new Float32Array(14);
  const dec = new BwavDecoder(ctx, {}); // loop enabled, no fade

  let out;
  if (!loopable) {
    const acc = new Int16Array(ctx.totalSamples);
    let idx = 0, n;
    while (idx < ctx.totalSamples && (n = dec.decodeBlock(block)) > 0) {
      for (let i = 0; i < n && idx < ctx.totalSamples; i++) acc[idx++] = block[i];
    }
    if (idx !== ctx.totalSamples) {
      throw new BwavIntegrityError(`short decode: got ${idx}/${ctx.totalSamples} samples`);
    }
    out = { rate: ctx.rate, intro: toF32(acc.subarray(0, idx)), loop: null,
            loopStart: 0, loopEnd: idx };
  } else {
    const bodyLen = ctx.loopEnd - ctx.loopStart;
    const need = ctx.loopEnd + bodyLen; // first pass + one reset-history loop body
    const acc = new Int16Array(need);
    let idx = 0, n;
    while (idx < need && (n = dec.decodeBlock(block)) > 0) {
      for (let i = 0; i < n && idx < need; i++) acc[idx++] = block[i];
    }
    if (idx !== need) {
      throw new BwavIntegrityError(`short decode: got ${idx}/${need} samples`);
    }
    out = {
      rate: ctx.rate,
      intro: toF32(acc.subarray(0, ctx.loopStart)),
      loop: toF32(acc.subarray(ctx.loopEnd, ctx.loopEnd + bodyLen)),
      loopStart: ctx.loopStart,
      loopEnd: ctx.loopEnd,
    };
  }

  // Secondary sanity: every produced sample must be finite and within the
  // valid PCM range. Integer decode + clamp16 guarantees this, so a violation
  // means memory/logic corruption — treat as fatal, never play it.
  for (const buf of [out.intro, out.loop]) {
    if (!buf) continue;
    for (let i = 0; i < buf.length; i++) {
      const v = buf[i];
      if (!Number.isFinite(v) || v < -1.0000001 || v > 1.0000001) {
        throw new BwavIntegrityError(`decoded sample out of range at ${i}: ${v}`);
      }
    }
  }
  return out;
}
