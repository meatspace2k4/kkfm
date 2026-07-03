import { validateSource, decodeToBuffers, BwavIntegrityError } from '/Users/johusha/kkfm-jukebox/jukebox-web/lib/bwav.js';
import { readFileSync } from 'node:fs';

const clean = readFileSync('/Users/johusha/kkfm-jukebox/bwav/1.bwav');
const abOf = (buf) => buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);

function expectPass(name, ab) {
  try { validateSource(ab); decodeToBuffers(ab); console.log('PASS-OK  ', name); return true; }
  catch (e) { console.log('FAIL     ', name, '->', e.message); return false; }
}
function expectReject(name, ab) {
  try { decodeToBuffers(ab); console.log('LEAK!!   ', name, '-> accepted corrupt input'); return false; }
  catch (e) {
    const ok = e instanceof BwavIntegrityError;
    console.log(ok ? 'REJECT-OK' : 'WRONGERR ', name, '->', e.name + ': ' + e.message.slice(0,60));
    return ok;
  }
}

let allGood = true;

// 1. clean file must pass
allGood &= expectPass('clean Hifi file', abOf(clean));

// 2. one flipped body byte (offset 0x200, well inside audio) -> CRC fail
{ const c = Buffer.from(clean); c[0x200] ^= 0xFF; allGood &= expectReject('1 flipped body byte', abOf(c)); }

// 3. flip a byte near the very end -> CRC fail
{ const c = Buffer.from(clean); c[c.length-10] ^= 0x01; allGood &= expectReject('flipped tail byte', abOf(c)); }

// 4. truncated (drop last 2000 bytes) -> truncated or CRC
{ const c = Buffer.from(clean.subarray(0, clean.length-2000)); allGood &= expectReject('truncated -2000B', abOf(c)); }

// 5. bad magic
{ const c = Buffer.from(clean); c[0]=0x42;c[1]=0x41;c[2]=0x44; allGood &= expectReject('bad magic', abOf(c)); }

// 6. corrupt a header field (codec id -> 5)
{ const c = Buffer.from(clean); c.writeUInt16LE(5, 16+0); allGood &= expectReject('bad codec_id', abOf(c)); }

// 7. zero out the stored CRC -> mismatch
{ const c = Buffer.from(clean); c.writeUInt32LE(0, 8); allGood &= expectReject('zeroed stored CRC', abOf(c)); }

// 8. single bit flip mid-file
{ const c = Buffer.from(clean); c[Math.floor(c.length/2)] ^= 0x08; allGood &= expectReject('mid-file bit flip', abOf(c)); }

console.log('\n' + (allGood ? 'ALL SAFETY GATES OK ✓' : 'SAFETY GATE FAILURE ✗'));
process.exit(allGood ? 0 : 1);
