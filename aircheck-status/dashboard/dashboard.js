"use strict";
/* The KKFM dashboard, reproduced from the same raw material the station's own
   watchdog reads. Two plain-HTTP sources, same origin, no websockets:

     /logs         the aircheck server's journal, streamed (chunked fetch).
                   Parsed HERE, a faithful port of watchdog/log_parser.py +
                   the dashboard's own radio-logic state machine.
     /status.json  systemd unit states + tx_status + anomalies, polled 1/s
                   (the same snapshot the ssh dashboard's remote script sends).

   Rendering: this file draws NOTHING. Once a second it builds the exact
   28-row screen watchdog/dashboard.py draws for a 100x28 curses terminal
   (same column positions, same padding, same truncation, same color pairs)
   and serializes it as one full ANSI repaint into window.termFeed, the same
   entry the source page's SSE feed drove. Colors are the Gotham theme's
   (gotham-default.terminal); the 256-cube grays and orange are the fixed
   xterm values curses picks on a 256-color TERM (245/250/208). */

var COLS = 100, ROWS = 28;

/* ---- palette: gotham-default.terminal + fixed xterm-256 values ---- */
var GREEN = [42,168,137], RED = [194,49,39], YELLOW = [237,180,67],
    CYAN = [51,133,158], MAGENTA = [78,81,102], WHITE = [153,209,206],
    BLACK = [12,16,20], BODY = [138,138,138] /*245*/,
    TITLE = [188,188,188] /*250*/, ORANGE = [255,135,0] /*208*/;

/* ---- one screen = ROWS of segment lists; serialize -> ANSI repaint ---- */
function blankScreen() {
  var rows = [];
  for (var r = 0; r < ROWS; r++) rows.push([]);
  return rows;
}
function put(rows, y, x, text, attr) {
  /* attr: {fg:[r,g,b], bg:[r,g,b], bold, ul}; clipped like addnstr */
  if (y < 0 || y >= ROWS || x >= COLS || !text) return;
  text = String(text).slice(0, Math.max(0, COLS - x));
  if (text) rows[y].push({x: x, s: text, a: attr || {}});
}
function sgr(a) {
  var ps = ["0"];
  if (a.bold) ps.push("1");
  if (a.ul) ps.push("4");
  if (a.fg) ps.push("38;2;" + a.fg[0] + ";" + a.fg[1] + ";" + a.fg[2]);
  if (a.bg) ps.push("48;2;" + a.bg[0] + ";" + a.bg[1] + ";" + a.bg[2]);
  return "\x1b[" + ps.join(";") + "m";
}
function serialize(rows) {
  var out = "\x1b[H";
  for (var r = 0; r < ROWS; r++) {
    var segs = rows[r].slice().sort(function(p, q){ return p.x - q.x; });
    var col = 0, line = "";
    for (var i = 0; i < segs.length; i++) {
      var sg = segs[i];
      if (sg.x > col) { line += "\x1b[0m" + Array(sg.x - col + 1).join(" "); col = sg.x; }
      var s = sg.s;
      if (sg.x < col) { s = s.slice(col - sg.x); }   /* later put wins */
      if (!s) continue;
      line += sgr(sg.a) + s;
      col += s.length;
    }
    out += line + "\x1b[0m\x1b[K" + (r < ROWS - 1 ? "\n" : "");
  }
  return out;
}

/* ---- watchdog/log_parser.py, ported ---- */
var ANSI_RE = /\x1b\[[0-?]*[ -\/]*[@-~]/g;
var SHORT_ISO = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{4}))\s+(.*)$/;
var EV_RES = [
  ["jingle_picked",    /\[RADIO\]\s+Jingle picked:\s+(.+?)\s+\(size=/],
  ["jingle_start",     /\[RADIO\]\s+Jingle start:\s+(.+)$/],
  ["jingle_end",       /\[RADIO\]\s+Jingle end:\s+(.+)$/],
  ["jingle_failed",    /\[RADIO\]\s+Jingle failed to load:\s+(.+)$/],
  ["commercial_picked",/\[RADIO\]\s+Commercial picked:\s+(.+?)\s+\(size=/],
  ["commercial_start", /\[RADIO\]\s+Commercial start:\s+(.+)$/],
  ["commercial_end",   /\[RADIO\]\s+Commercial end:\s+(.+)$/],
  ["commercial_failed",/\[RADIO\]\s+Commercial failed to load:\s+(.+)$/],
  ["now_playing",      /Now Playing:\s+(.+)$/],
];
var BWAV_RE = /\[(CACHE|FILE|BWAV)\]\s+(\S+?\.bwav)/i;
var LEVELS = { now_playing:"INFO", unit_started:"INFO", jingle_picked:"INFO",
  jingle_start:"INFO", jingle_end:"INFO", commercial_picked:"INFO",
  commercial_start:"INFO", commercial_end:"INFO", jingle_failed:"ERROR",
  commercial_failed:"ERROR", dac_throttle:"WARN", dac_overflow:"ERROR",
  dac_underrun:"ERROR", unit_stopped:"CRIT", safe_shutdown:"WARN" };

function parseTs(iso) {
  var s = iso.endsWith("Z") ? iso.slice(0, -1) + "+00:00"
        : iso.replace(/([+-])(\d{2})(\d{2})$/, "$1$2:$3");
  var t = Date.parse(s);
  return isNaN(t) ? null : t / 1000;
}
function parseEvent(line, defaultTs) {
  var s = line.replace(ANSI_RE, "").trim();
  var ts = defaultTs, m = SHORT_ISO.exec(s);
  if (m) { var t = parseTs(m[1]); if (t !== null) ts = t; s = m[2]; }
  /* journalctl prefixes "host unit[pid]: " on short-iso lines */
  s = s.replace(/^\S+\s+\S+\[\d+\]:\s+/, "");
  var s2 = s.startsWith("ERROR: ") ? s.slice(7) : s;
  if (/^Started\s+.+/i.test(s2)) return {etype:"unit_started", ts:ts, raw:s, name:null};
  if (/^Stopped\s+.+/i.test(s2)) return {etype:"unit_stopped", ts:ts, raw:s, name:null};
  var sm = /shutdown\s+reason:\s*(.+)/i.exec(s2);
  if (sm) return {etype:"safe_shutdown", ts:ts, raw:s, name:(sm[1]||"").trim()||null};
  for (var i = 0; i < EV_RES.length; i++) {
    var em = EV_RES[i][1].exec(s2);
    if (em) return {etype:EV_RES[i][0], ts:ts, raw:s,
                    name:(em[1]||"").trim()||null};
  }
  if (/\[DAC\].*Ring buffer overflow/.test(s2)) return {etype:"dac_overflow", ts:ts, raw:s, name:null};
  if (/\[DAC\].*throttling producer/i.test(s2)) return {etype:"dac_throttle", ts:ts, raw:s, name:null};
  if (/\[DAC\].*Ring buffer underrun/i.test(s2)) return {etype:"dac_underrun", ts:ts, raw:s, name:null};
  var bm = BWAV_RE.exec(s2);
  if (bm) return {etype:"_bwav", ts:ts, raw:s, name:bm[2], src:bm[1].toUpperCase()};
  return null;
}

/* ---- radio-logic state (the dashboard's own machine, ported) ---- */
var START_TIMEOUT_S = 300, END_TIMEOUT_S = 45;
var lastTxLabel = "WAIT", lastTxColor = null, txSilenceSince = null;

var st = {
  lastEvent: null, lastBwav: null,
  jing: {picked: null, pickedTs: 0, started: null, startedTs: 0},
  comm: {picked: null, pickedTs: 0, started: null, startedTs: 0},
  lastSong: null,   /* last now_playing title, for the chip row */
  lastJingleName: null, lastJingleState: null,
  lastCommName: null, lastCommState: null,
};
function det(kind) { return kind === "JING" ? st.jing : st.comm; }
function feedEvent(ev) {
  if (ev.etype === "_bwav") { st.lastBwav = ev; return; }
  st.lastEvent = ev;
  var d, name = ev.name;
  if (ev.etype.indexOf("jingle") === 0) d = st.jing;
  if (ev.etype.indexOf("commercial") === 0) d = st.comm;
  if (d && name) {
    if (/_picked$/.test(ev.etype)) { d.picked = name; d.pickedTs = ev.ts; }
    else if (/_start$/.test(ev.etype)) { d.started = name; d.startedTs = ev.ts; d.picked = null; }
    else if (/_end$/.test(ev.etype) || /_failed$/.test(ev.etype)) { d.started = null; d.picked = null; }
  }
  if (ev.etype === "now_playing" && name) st.lastSong = name;
  if (name) {
    if (ev.etype === "jingle_picked") { st.lastJingleName = name; st.lastJingleState = "PICKED"; }
    else if (ev.etype === "jingle_start") { st.lastJingleName = name; st.lastJingleState = "PLAYING"; }
    else if (ev.etype === "jingle_end") { st.lastJingleName = name; st.lastJingleState = "ENDED"; }
    else if (ev.etype === "commercial_picked") { st.lastCommName = name; st.lastCommState = "PICKED"; }
    else if (ev.etype === "commercial_start") { st.lastCommName = name; st.lastCommState = "PLAYING"; }
    else if (ev.etype === "commercial_end") { st.lastCommName = name; st.lastCommState = "ENDED"; }
  }
}
function expireDet(now) {
  [st.jing, st.comm].forEach(function(d, i){
    if (d.started && now - d.startedTs > END_TIMEOUT_S) {
      d.started = null;
      if (i === 0 && st.lastJingleState === "PLAYING") st.lastJingleState = "ENDED";
      if (i === 1 && st.lastCommState === "PLAYING") st.lastCommState = "ENDED";
    }
    if (d.picked && now - d.pickedTs > START_TIMEOUT_S) d.picked = null;
  });
}

/* ---- the player: playback embedded in the TUI itself ----------------
   The chip on the last row is terminal cells like everything else; the renderer
   maps clicks back through the rotation to (row, col) and termClick lands
   here. The pipeline is the radio page's own (worklet PCM, 44-byte WAV
   header skip, byte-boundary carry), against /stream on this host. */
var player = { state: "idle", err: null, ctx: null, abort: null };
var playHit = null;

var WORKLET = "class P extends AudioWorkletProcessor{constructor(){super();" +
  "this.q=[];this.cur=null;this.o=0;this.port.onmessage=e=>{if(e.data)" +
  "this.q.push(e.data)}}process(i,o){var L=o[0][0],R=o[0][1];" +
  "for(var n=0;n<L.length;n++){if(!this.cur||this.o>=this.cur.length){" +
  "if(!this.q.length){L[n]=0;R[n]=0;continue}this.cur=this.q.shift();" +
  "this.o=0}L[n]=this.cur[this.o++];R[n]=this.cur[this.o++]}return true}}" +
  "registerProcessor('p',P);";

function startPlay() {
  player.state = "connecting"; player.err = null; refresh();
  (async function(){
    try {
      var ctx = new (window.AudioContext || window.webkitAudioContext)(
        {sampleRate: 48000});
      var blob = new Blob([WORKLET], {type: "application/javascript"});
      await ctx.audioWorklet.addModule(URL.createObjectURL(blob));
      var node = new AudioWorkletNode(ctx, "p", {outputChannelCount: [2]});
      node.connect(ctx.destination);
      var ab = new AbortController();
      player.ctx = ctx; player.abort = ab;
      /* RECONNECT LOOP: the tunnel recycles long-lived responses, and a
         clean upstream end used to read as "it just died". A tuner
         retunes; only the user's stop ends this loop. */
      while (player.abort === ab) {
        try {
          var resp = await fetch("/stream", {signal: ab.signal});
          if (!resp.ok || !resp.body)
            throw new Error("stream: HTTP " + resp.status);
          player.state = "live"; player.err = null; refresh();
          var reader = resp.body.getReader();
          var hdr = 44, leftover = new Uint8Array(0);
          for (;;) {
            var r = await reader.read();
            if (r.done) break;
            var chunk = r.value;
            if (hdr > 0) {
              if (chunk.byteLength <= hdr) { hdr -= chunk.byteLength; continue; }
              chunk = chunk.subarray(hdr); hdr = 0;
            }
            var buf = new Uint8Array(leftover.length + chunk.length);
            buf.set(leftover); buf.set(chunk, leftover.length);
            var usable = buf.length & ~3;   /* whole stereo frames only */
            leftover = buf.subarray(usable);
            if (!usable) continue;
            var i16 = new Int16Array(buf.buffer, 0, usable >> 1);
            var f = new Float32Array(usable >> 1);
            for (var i = 0; i < f.length; i++) f[i] = i16[i] / 32768;
            node.port.postMessage(f, [f.buffer]);
          }
        } catch (e) {
          if (e.name === "AbortError") break;
          player.err = e.message || String(e);
        }
        if (player.abort !== ab) break;   /* user stop */
        player.state = "connecting"; refresh();
        await new Promise(function(res){ setTimeout(res, 1000); });
      }
    } catch (e) {
      if (e.name !== "AbortError") player.err = e.message || String(e);
    }
    if (player.ctx) stopPlay();
  })();
}
function stopPlay() {
  var ab = player.abort, ctx = player.ctx;
  player.abort = null; player.ctx = null; player.state = "idle";
  try { if (ab) ab.abort(); } catch (e) {}
  try { if (ctx) ctx.close(); } catch (e) {}
  refresh();
}
window.termClick = function(r, c) {
  if (playHit && r === playHit.row && c >= playHit.c0 && c < playHit.c1) {
    if (player.state === "idle") startPlay(); else stopPlay();
  }
};

/* ---- transports ---- */
var link = { logsUp: false, lastRx: 0, retryAt: 0, state: "CONNECTING" };
var snap = null, lastStatusRx = 0;  /* NOT `status`: that is
   window.status, a string accessor, and assigning it stringifies */

function startLogStream() {
  link.state = link.lastRx ? link.state : "CONNECTING";
  fetch("logs").then(function(resp){
    if (!resp.ok || !resp.body) throw new Error("logs: HTTP " + resp.status);
    link.logsUp = true; link.state = "CONNECTED";
    var reader = resp.body.getReader(), dec = new TextDecoder(), buf = "";
    function pump() {
      return reader.read().then(function(r){
        if (r.done) throw new Error("logs: stream ended");
        buf += dec.decode(r.value, {stream: true});
        var lines = buf.split("\n"); buf = lines.pop();
        for (var i = 0; i < lines.length; i++) {
          var ev = parseEvent(lines[i], Date.now()/1000);
          if (ev) feedEvent(ev);
        }
        link.lastRx = Date.now()/1000;
        return pump();
      });
    }
    return pump();
  }).catch(function(){
    link.logsUp = false; link.state = "DISCONNECTED";
    setTimeout(startLogStream, 3000);
  });
}
function pollStatus() {
  fetch("status.json").then(function(r){ return r.json(); })
    .then(function(j){ snap = j; lastStatusRx = Date.now()/1000; })
    .catch(function(){})
    .finally(function(){ setTimeout(pollStatus, 1000); });
}

/* ---- formatting helpers from dashboard.py ---- */
function fmtTs(ts) {
  if (ts == null) return "-";
  var d = new Date(ts * 1000);
  function p(n){ return (n < 10 ? "0" : "") + n; }
  return d.getFullYear() + "-" + p(d.getMonth()+1) + "-" + p(d.getDate())
    + " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}
function fmtValue(v) {
  if (v === null || v === undefined) return "-";
  if (typeof v === "boolean") return v ? "1" : "0";
  if (typeof v === "number")
    return Number.isInteger(v) ? String(v) : v.toFixed(1);
  return String(v);
}
function fmtKv(label, val, width) {
  var s = label + "= " + fmtValue(val);
  return width <= 0 ? s : s.padEnd(width);
}
function prettyBwav(v) {
  if (typeof v !== "string" || !v.trim()) return "";
  var base = v.trim().split("/").pop();
  if (/\.bwav$/i.test(base)) base = base.slice(0, -5);
  return base.replace(/^(BGM_Radio_Jingle_|Radio_CM_|BGM_Radio_CM_)/, "");
}
function center(s, w) {
  if (s.length >= w) return s.slice(0, w);
  var left = Math.floor((w - s.length) / 2);
  return Array(left + 1).join(" ") + s + Array(w - s.length - left + 1).join(" ");
}

/* the link line: same states and format the source TUI prints for its
   stream; the transport here is the HTTP feed. CONNECTED means the log
   stream is open and fresh. */
function linkLine(now) {
  if (link.state === "CONNECTED") {
    var age = now - Math.max(link.lastRx, lastStatusRx);
    if (age > 8) return "SSH: DEGRADED (no updates for " + Math.round(age) + "s)";
    return "SSH: CONNECTED";
  }
  if (link.state === "CONNECTING") return "SSH: CONNECTING";
  return "SSH: DISCONNECTED";
}

/* ---- the draw, row for row as dashboard.py lays it out at 100x28 ---- */
function render() {
  var now = Date.now() / 1000;
  expireDet(now);
  var rows = blankScreen();
  if (lastTxColor === null) lastTxColor = YELLOW;
  var A = function(fg, bold, ul, bg){ return {fg:fg, bold:!!bold, ul:!!ul, bg:bg||null}; };
  var titleA = A(TITLE, true, true), bodyA = A(BODY), valA = A(TITLE);

  /* header: level -> pair 8/10/9 (black on green/yellow/red) */
  var level = 0;
  if (link.state !== "CONNECTED") level = 2;
  var services = (snap && snap.services) || {};
  var units = ["aircheck.service", "aircheck-watchdog.service",
               "aircheck-tx-watchdog.service"];
  units.forEach(function(u){
    var s = services[u];
    if (s && s.active !== "active") level = Math.max(level, 2);
  });
  var tx = snap && snap.tx;
  if (tx) {
    var age = now - (tx.ts || 0);
    if (age > 30) level = Math.max(level, 2);
    else if (age < 5 && tx.audio_present === false) level = Math.max(level, 1);
  } else if (link.state === "CONNECTED") level = Math.max(level, 2);
  var hdrBg = level >= 2 ? RED : (level === 1 ? YELLOW : GREEN);
  var hdr = " KKFM AIRCHECK DASHBOARD | " + fmtTs(now) + " | " + linkLine(now) + " ";
  put(rows, 0, 0, center(hdr, COLS), A(BLACK, true, false, hdrBg));

  /* the chip shows the STATION's state, not the browser's: LIVE whenever
     the transmitter carries audio, whether or not anyone here listens.
     On-air comes straight from the tx snapshot, and until the FIRST
     snapshot exists there is no chip at all: never a guessed PLAY that
     flips to LIVE a beat later. */
  playHit = null; window.termHits = [];
  if (snap || player.state !== "idle") {
    var onAir = !!(tx && (now - (tx.ts || 0) < 30) && tx.audio_present);
    var listening = player.state !== "idle";   /* connecting counts as on */
    var chip = listening ? "  PLAYING\u2026  "
             : onAir ? "  \u25CF LIVE  " : "  \u25B6 PLAY  ";
    put(rows, 2, 2, chip, A(BLACK, true, false, GREEN));
    playHit = {row: 2, c0: 2, c1: 2 + chip.length};
    window.termHits = [playHit];   /* the renderer shows a pointer over these */
    var hintCol = 2 + chip.length + 1;
    /* what is on air: a jingle or commercial names itself generically
       ("Jingle"/"Commercial"), an intro/outro shows nothing, otherwise
       the song title stands. */
    var onair = (!st.jing.started && !st.comm.started && st.lastSong)
              ? st.lastSong : null;   /* blank until a song title is on air */
    if (player.err)
      put(rows, 2, hintCol, String(player.err).slice(0, 70), A(RED));
    else if (listening) {
      /* toggled on: the title (or Jingle/Commercial) replaces the hint */
      if (onair) put(rows, 2, hintCol, onair, valA);
    } else
      put(rows, 2, hintCol, "<-- Click to LISTEN!", A(YELLOW, true));
  }

  /* SYSTEM SERVICES */
  put(rows, 4, 2, "SYSTEM SERVICES", titleA);
  var y = 5;
  units.forEach(function(u){
    var s = services[u] || {active: (link.state === "CONNECTED" ? "CONNECT" : "OFFLINE"), sub: "-"};
    put(rows, y, 4, (u + "                                   ").slice(0, 35) + " ", bodyA);
    var color = s.active === "active" ? GREEN
              : (s.active === "CONNECT" || s.active === "OFFLINE") ? YELLOW : RED;
    put(rows, y, 40, s.active + "/" + s.sub, A(color, true));
    y++;
  });

  /* TRANSMITTER */
  put(rows, 9, 2, "TRANSMITTER (Arduino/Si4713)", titleA);
  if (tx) {
    var age2 = now - (tx.ts || 0);
    var fresh = age2 < 5, down = age2 > 30;
    var audio = tx.audio_present;
    var label, color;
    if (fresh) {
      if (tx.in_fault_state) { label = "FAULT"; color = RED; }
      else if (tx.recovery_in_progress) { label = "RECOVERING (" + (tx.recovery_attempt_count||0) + ")"; color = YELLOW; }
      else if (audio) { txSilenceSince = null; label = "AUDIO"; color = GREEN; }
      else {
        /* the source's 5s silence grace: hold the last label until real */
        if (txSilenceSince === null) txSilenceSince = now;
        if (now - txSilenceSince >= 5) { label = "SILENCE"; color = YELLOW; }
        else { label = lastTxLabel; color = lastTxColor; }
      }
      lastTxLabel = label; lastTxColor = color;
    } else if (down) {
      label = "NO TELEMETRY"; color = RED;
    } else {
      /* aging telemetry keeps the last label, exactly as the source does */
      label = lastTxLabel; color = lastTxColor;
    }
    var x = 4;
    put(rows, 10, x, "TX: ", bodyA); x += 4;
    put(rows, 10, x, label, A(color, true)); x += label.length;
    put(rows, 10, x, "  Freq: ", bodyA); x += 8;
    var freq = ((typeof tx.freq_mhz === "number" && Number.isInteger(tx.freq_mhz))
                ? tx.freq_mhz.toFixed(1) : fmtValue(tx.freq_mhz)) + " MHz";
    put(rows, 10, x, freq, A(CYAN, true)); x += freq.length;
    put(rows, 10, x, "  Port: ", bodyA); x += 8;
    put(rows, 10, x, String(tx.port), valA);
    put(rows, 11, 4, "Status: ", bodyA);
    put(rows, 11, 12, fmtTs(tx.ts), valA);
    put(rows, 12, 4, fmtKv("In", tx.inlevel, 14) + fmtKv("Noise", tx.noise, 14)
                   + fmtKv("Overmod", tx.overmod, 0), valA);
  } else {
    put(rows, 10, 4, "Waiting for transmitter data...", A(YELLOW));
  }

  /* RADIO LOGIC MONITOR (derived from /logs, parsed here) */
  put(rows, 14, 2, "RADIO LOGIC MONITOR", titleA);
  var le = st.lastEvent;
  var lvl = le ? (LEVELS[le.etype] || "INFO") : "IDLE";
  var lvlA = lvl === "INFO" ? A(CYAN) : lvl === "WARN" ? A(YELLOW)
           : lvl === "ERROR" ? A(ORANGE, true) : lvl === "CRIT" ? A(RED, true) : bodyA;
  put(rows, 15, 4, "LOGS:", bodyA);
  put(rows, 15, 11, (lvl.slice(0,7) + "        ").slice(0, 8), lvlA);
  if (le) {
    var ageS = Math.max(0, now - le.ts);
    var g1 = le.etype + " (" + String(Math.round(ageS)).padStart(3) + "s)";
    put(rows, 15, 19, g1, A(CYAN));
    if (le.name) {
      put(rows, 15, 19 + g1.length, " -> ", bodyA);
      put(rows, 15, 19 + g1.length + 4, prettyBwav(le.name), valA);
    }
  }
  put(rows, 16, 4, "BWAV:", bodyA);
  if (st.lastBwav) {
    put(rows, 16, 11, ((st.lastBwav.src || "FILE").slice(0,7) + "        ").slice(0, 8), A(CYAN));
    put(rows, 16, 19, prettyBwav(st.lastBwav.name).slice(0, 40), bodyA);
  } else {
    put(rows, 16, 11, "IDLE", bodyA);
  }
  [["JING", st.jing, st.lastJingleName, st.lastJingleState],
   ["COMM", st.comm, st.lastCommName, st.lastCommState]].forEach(function(row, i){
    var yy = 17 + i, kind = row[0], d = row[1], lastName = row[2], lastState = row[3];
    put(rows, yy, 4, kind + ":", bodyA);
    if (d.started) {
      put(rows, yy, 11, "PLAYING ", A(GREEN, true));
      put(rows, yy, 19, prettyBwav(d.started).slice(0, 40), bodyA);
    } else if (d.picked) {
      put(rows, yy, 11, "PICKED  ", A(YELLOW));
      put(rows, yy, 19, prettyBwav(d.picked).slice(0, 40), bodyA);
    } else if (lastName) {
      put(rows, yy, 11, ((lastState || "PREV").slice(0,7) + "        ").slice(0, 8), A(CYAN));
      put(rows, yy, 19, prettyBwav(lastName).slice(0, 40), bodyA);
    } else {
      put(rows, yy, 11, "IDLE", bodyA);
    }
  });

  /* RECENT ANOMALIES */
  put(rows, 20, 0, center(" RECENT ANOMALIES ", COLS), A(BLACK, true, false, WHITE));
  var recs = (snap && snap.anomalies) || [];
  var grouped = {};
  recs.forEach(function(r){
    if (!r.code) return;
    if (!grouped[r.code] || (r.ts||0) > (grouped[r.code].ts||0)) grouped[r.code] = r;
  });
  var sorted = Object.keys(grouped).map(function(k){ return grouped[k]; })
    .sort(function(a,b){ return (b.ts||0) - (a.ts||0); });
  var sevColor = function(sev){
    return sev === "critical" ? MAGENTA : sev === "error" ? RED
         : sev === "warn" ? YELLOW : CYAN;
  };
  if (!sorted.length) {
    put(rows, 21, 4, "No anomalies detected. System healthy.", bodyA);
  } else {
    function mmdd(ts){
      var d = new Date((ts||0)*1000);
      function p(n){ return (n<10?"0":"")+n; }
      return p(d.getMonth()+1) + "/" + p(d.getDate());
    }
    function hms(ts){
      var d = new Date((ts||0)*1000);
      function p(n){ return (n<10?"0":"")+n; }
      return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
    }
    var yA = 21;
    var first = sorted[0];
    put(rows, yA, 4, mmdd(first.ts) + "  ", bodyA);
    put(rows, yA, 11, first.code || "", A(sevColor((first.severity||"info").toLowerCase()), true));
    yA++;
    var lastDate = mmdd(first.ts);
    for (var ri = 0; ri < sorted.length && yA < ROWS; ri++) {
      var r = sorted[ri];
      var thisDate = mmdd(r.ts);
      if (thisDate !== lastDate) {
        yA++;
        if (yA >= ROWS) break;
        put(rows, yA, 4, thisDate + "  " + (r.code || ""), bodyA);
        yA++;
        lastDate = thisDate;
        if (yA >= ROWS) break;
      }
      var sev = (r.severity || "info").toLowerCase();
      put(rows, yA, 11, (sev.toUpperCase() + "      ").slice(0, 6), A(sevColor(sev), true));
      put(rows, yA, 19, (hms(r.ts) + "        ").slice(0, 8), bodyA);
      put(rows, yA, 29, String(r.message || "").slice(0, COLS - 30), bodyA);
      yA++;
    }
  }

  window.__linkdiag = "feed:   logs " + (link.logsUp ? "streaming" : link.state.toLowerCase())
    + ", status " + (snap ? Math.round(now - lastStatusRx) + "s ago" : "never");
  return serialize(rows);
}

function refresh() {
  if (!window.termFeed) return;
  try { window.termFeed(render()); }
  catch (e) { document.getElementById("diag").textContent = "FATAL: " + e.message; throw e; }
}
function tick() {
  refresh();
  setTimeout(tick, 1000);
}

startLogStream();
pollStatus();
tick();
