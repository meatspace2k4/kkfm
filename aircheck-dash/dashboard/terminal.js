"use strict";
/* where this script lives; scene.cfg sits beside it, whatever page
   embeds us (the radio root page loads us from /dash/) */
var SCRIPT_BASE = (function(){ var s = document.currentScript;
  return s && s.src ? s.src.replace(/[^\/]*$/, "") : ""; })();
/* The dashboard TUI, rendered by a terminal emulator whose framebuffer is
   geometry: cells are a scene, drawn through one primitive list against a
   fixed orthographic camera.

   - COLS/ROWS are this TUI's terminal (100x28), DEF_FG is the Gotham theme's
     TextColor (gotham-default.terminal).
   - feedAnsi handles the SGR the TUI emits: 38;2 truecolor fg, 4/24
     underline, 39/49 defaults. It throws on anything unhandled rather than
     skipping quietly, so extend it here when the TUI emits something new.
   - cells carry `ul`; glyph runs split on it; the text draw underlines.
   - the data layer: dashboard.js fetches the station's log endpoint over
     plain HTTP, reproduces the dashboard's own drawing as full ANSI
     repaints, and feeds them to feedAnsi. Nothing here is a screenshot
     mimic; it renders terminal bytes. */

/* ---- scene config (custom compact format; fail loud) ---- */
var CFG = null;
function parseCfg(text) {
  var cfg = {};
  var lines = text.split("\n");
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].split("#")[0].trim();
    if (!line) continue;
    var tk = line.split(/\s+/);
    cfg[tk[0]] = tk.slice(1).map(Number);
  }
  var need = ["ch","canvas","depth","fov","trail","grid"];
  for (var n = 0; n < need.length; n++)
    if (!(need[n] in cfg)) throw new Error("scene.cfg missing: " + need[n]);
  return cfg;
}

/* ---- fixed camera: orthographic, identity, never reassigned ---- */
var CAMERA = Object.freeze({ kind:"ortho", rot:Object.freeze([0,0]) });
var scenePose = { rx: 0, ry: 0 };   /* what dragging changes: the assembly */

/* ---- the terminal ---- */
var COLS = 100, ROWS = 28;
var DEF_FG = "#99d1ce", DEF_BG = null;   /* Gotham TextColor / transparent */
var term = [];        /* [row][col] = {ch, fg, bg, bold, ul} */
var SEL = null;       /* {a:[r,c], f:[r,c]} native text selection */
function termClear() {
  term = [];
  for (var r = 0; r < ROWS; r++) { term.push([]);
    for (var c = 0; c < COLS; c++)
      term[r].push({ch:" ", fg:DEF_FG, bg:DEF_BG, bold:false, ul:false}); }
}
termClear();

/* Parse one repaint of ANSI exactly as a terminal would. Handles the
   sequences the generator emits: [H home, [2J clear, [K erase-to-EOL,
   [?25l/h cursor, and SGR m codes (0 reset, 1 bold, 4/24 underline,
   30 fg-black, 97 fg-bright-white, 39/49 defaults, 38;2;r;g;b truecolor fg,
   48;2;r;g;b truecolor bg). Anything else in the stream is a bug in this
   emulator: throw, do not skip quietly. */
function feedAnsi(s) {
  var cx = 0, cy = 0, fg = DEF_FG, bg = DEF_BG, bold = false, ul = false;
  var i = 0;
  while (i < s.length) {
    var ch = s[i];
    if (ch === "\x1b") {
      if (s[i+1] !== "[") throw new Error("ansi: ESC without [ at " + i);
      var j = i + 2, params = "";
      while (j < s.length && !/[A-Za-z]/.test(s[j])) { params += s[j]; j++; }
      var letter = s[j];
      if (letter === "H") { cx = 0; cy = 0; }
      else if (letter === "J") { termClear(); cx = 0; cy = 0; }
      else if (letter === "K") {
        for (var k = cx; k < COLS; k++)
          term[cy][k] = {ch:" ", fg:DEF_FG, bg:DEF_BG, bold:false, ul:false};
      }
      else if (letter === "l" || letter === "h") { /* cursor vis: no cells */ }
      else if (letter === "m") {
        var ps = params === "" ? ["0"] : params.split(";");
        for (var p = 0; p < ps.length; p++) {
          var v = parseInt(ps[p] || "0", 10);
          if (v === 0) { fg = DEF_FG; bg = DEF_BG; bold = false; ul = false; }
          else if (v === 1) bold = true;
          else if (v === 4) ul = true;
          else if (v === 24) ul = false;
          else if (v === 30) fg = "#0c1014";
          else if (v === 97) fg = "#f4f4f4";
          else if (v === 39) fg = DEF_FG;
          else if (v === 49) bg = DEF_BG;
          else if (v === 38 && ps[p+1] === "2") {
            fg = "rgb(" + ps[p+2] + "," + ps[p+3] + "," + ps[p+4] + ")";
            p += 4;
          }
          else if (v === 48 && ps[p+1] === "2") {
            bg = "rgb(" + ps[p+2] + "," + ps[p+3] + "," + ps[p+4] + ")";
            p += 4;
          }
          else throw new Error("ansi: unhandled SGR " + ps[p]);
        }
      }
      else throw new Error("ansi: unhandled CSI " + letter);
      i = j + 1;
      continue;
    }
    if (ch === "\n") { cy++; cx = 0; i++; if (cy >= ROWS) cy = ROWS - 1; continue; }
    if (ch === "\r") { cx = 0; i++; continue; }
    if (cx < COLS && cy < ROWS)
      term[cy][cx] = {ch:ch, fg:fg, bg:bg, bold:bold, ul:ul};
    cx++; i++;
  }
}

/* ---- frame data: this TUI has no depth grid; the bars stay parked ---- */
var N = 8, NO_TARGET = 4000;
var HOLD_FRAMES = 6;
var hold = [], stale = [];
for (var r0 = 0; r0 < N; r0++) { hold.push([]); stale.push([]);
  for (var c0 = 0; c0 < N; c0++) { hold[r0].push(NO_TARGET); stale[r0].push(7); } }
var trail = [];

/* ---- the one render path ----
   Terminal grid -> primitive list in scene space -> rotate assembly ->
   painter sort -> draw. Bars and glyph runs go through the same transform. */
function rotp(p, cx, cy, cz) {
  var x = p[0]-cx, y = p[1]-cy, z = p[2]-cz;
  var cyr = Math.cos(scenePose.ry), syr = Math.sin(scenePose.ry);
  var x1 = x*cyr + z*syr, z1 = -x*syr + z*cyr;
  var cxr = Math.cos(scenePose.rx), sxr = Math.sin(scenePose.rx);
  var y1 = y*cxr - z1*sxr, z2 = y*sxr + z1*cxr;
  return [x1+cx, y1+cy, z2+cz];
}
function darken(css, f) {
  var m = /rgb\((\d+),(\d+),(\d+)\)/.exec(css);
  if (!m) return css;
  return "rgb(" + [1,2,3].map(function(i){ return Math.round(+m[i]*f); }).join(",") + ")";
}

var MAXZ = null;   /* the back plane: NO_TARGET at depth scale (set on load) */
function cellZ(row, col) {
  /* z of this char cell's FRONT FACE. The interface itself lives at z=0 and
     nothing may cross it (the 4th wall). This TUI's scene.cfg parks the
     depth region off-grid, so every cell is the glass: z=0, no bar. */
  var g = CFG.grid;
  var r = row - g[1], c = Math.floor((col - g[0]) / g[2]);
  if (r < 0 || r >= N || c < 0 || c >= N || col < g[0] || col >= g[0]+N*g[2])
    return 0;
  var v = stale[r][c] <= HOLD_FRAMES ? hold[r][c] : NO_TARGET;
  return (v >= NO_TARGET ? NO_TARGET : v) * CFG.depth[0];
}

function buildPrims() {
  var chw = CFG.ch[0], chh = CFG.ch[1], prims = [];
  /* background runs: merge adjacent same-bg same-depth cells per row into
     one bar (front + 4 sides when extruded) */
  for (var row = 0; row < ROWS; row++) {
    var col = 0;
    while (col < COLS) {
      var cell = term[row][col];
      var zf = cellZ(row, col);
      var end = col;
      while (end < COLS && term[row][end].bg === cell.bg
             && cellZ(row, end) === zf) end++;
      if (cell.bg !== null) {
        var x0 = col*chw, x1 = end*chw, y0 = row*chh, y1 = (row+1)*chh;
        var A=[x0,y0,zf], B=[x1,y0,zf], C=[x1,y1,zf], D=[x0,y1,zf];
        if (zf > 0 && zf < MAXZ) {      /* a bar: face forward of the back plane */
          var A2=[x0,y0,MAXZ], B2=[x1,y0,MAXZ], C2=[x1,y1,MAXZ], D2=[x0,y1,MAXZ];
          prims.push({t:"quad", p:[A2,B2,C2,D2], fill:darken(cell.bg,0.45)});
          prims.push({t:"quad", p:[A,B,B2,A2], fill:darken(cell.bg,0.72)});
          prims.push({t:"quad", p:[D,C,C2,D2], fill:darken(cell.bg,0.55)});
          prims.push({t:"quad", p:[A,D,D2,A2], fill:darken(cell.bg,0.66)});
          prims.push({t:"quad", p:[B,C,C2,B2], fill:darken(cell.bg,0.60)});
        }
        prims.push({t:"quad", p:[A,B,C,D], fill:cell.bg});
      }
      col = end;
    }
    /* selection overlay: HTML-like selection on the glass, Gotham
       SelectionColor. One quad per selected span at the span's face depth
       (this TUI is all z=0), after the bg runs and before the glyphs. */
    var span = _selSpan(row);
    if (span) {
      var zSel = cellZ(row, span[0]);
      var qx0 = span[0]*chw, qx1 = (span[1]+1)*chw;
      var qy0 = row*chh, qy1 = (row+1)*chh;
      prims.push({t:"quad", fill:"#0a3749",
                  p:[[qx0,qy0,zSel],[qx1,qy0,zSel],[qx1,qy1,zSel],[qx0,qy1,zSel]]});
    }
    /* glyph runs: same fg/bold/underline AND same face depth */
    col = 0;
    while (col < COLS) {
      var c2 = term[row][col];
      if (c2.ch === " " && !c2.ul) { col++; continue; }
      var z2 = cellZ(row, col);
      var e2 = col, s = "";
      while (e2 < COLS && term[row][e2].fg === c2.fg
             && term[row][e2].bold === c2.bold
             && term[row][e2].ul === c2.ul
             && cellZ(row, e2) === z2
             && !(term[row][e2].ch === " " && e2 > col && term[row][e2+1] && term[row][e2+1].ch === " " && !c2.ul)) {
        s += term[row][e2].ch; e2++;
      }
      if (!c2.ul) s = s.replace(/\s+$/, "");
      if (s) prims.push({t:"text", o:[col*chw, row*chh, z2], s:s,
                         w:s.length*chw, h:chh, color:c2.fg, bold:c2.bold,
                         ul:c2.ul});
      col = e2 > col ? e2 : col + 1;
    }
  }
  return prims;
}

var VIEW = 1;   /* content scale is 1: buffer is native size, untouched */
function draw() {
  var cv = document.getElementById("cv"), ctx = cv.getContext("2d");
  var chw = CFG.ch[0], chh = CFG.ch[1];
  var W = CFG.canvas[0]*chw, H = CFG.canvas[1]*chh;
  if (cv.width !== W) { cv.width = W; cv.height = H; }
  /* the buffer is EXACTLY the content: no slack, so the element's box is
     the interface and nothing dead participates in layout or zoom. A
     rotation swing clips at the edges; that is cosmetic. */
  var OX = 0, OY = 0;
  ctx.setTransform(1,0,0,1,0,0);
  ctx.clearRect(0,0,cv.width,cv.height);
  var cx = W/2, cy = H/2, cz = 0;
  var prims = buildPrims(), drawn = [];
  for (var i = 0; i < prims.length; i++) {
    var p = prims[i];
    if (p.t === "quad") {
      var q = [rotp(p.p[0],cx,cy,cz), rotp(p.p[1],cx,cy,cz),
               rotp(p.p[2],cx,cy,cz), rotp(p.p[3],cx,cy,cz)];
      drawn.push({z:(q[0][2]+q[1][2]+q[2][2]+q[3][2])/4, t:"quad", q:q, fill:p.fill});
    } else {
      var o = rotp(p.o,cx,cy,cz);
      var ex = rotp([p.o[0]+1,p.o[1],p.o[2]],cx,cy,cz);
      var ey = rotp([p.o[0],p.o[1]+1,p.o[2]],cx,cy,cz);
      /* A glyph must beat ITS OWN BAR from any side; see the source. */
      var ctrF = rotp([p.o[0]+(p.w||0)/2, p.o[1]+(p.h||0)/2, p.o[2]],cx,cy,cz);
      var ctrB = rotp([p.o[0]+(p.w||0)/2, p.o[1]+(p.h||0)/2,
                       p.o[2] > 0 ? MAXZ : 0],cx,cy,cz);
      drawn.push({z:Math.min(ctrF[2], ctrB[2])-1, t:"text", o:o,
                  ax:ex[0]-o[0], ay:ex[1]-o[1], bx:ey[0]-o[0], by:ey[1]-o[1],
                  s:p.s, w:p.w, color:p.color, bold:p.bold, ul:p.ul});
    }
  }
  drawn.sort(function(a,b){ return b.z - a.z; });
  for (var d = 0; d < drawn.length; d++) {
    var e = drawn[d];
    if (e.t === "quad") {
      ctx.setTransform(1,0,0,1,OX,OY);
      ctx.beginPath();
      ctx.moveTo(e.q[0][0], e.q[0][1]);
      for (var v = 1; v < 4; v++) ctx.lineTo(e.q[v][0], e.q[v][1]);
      ctx.closePath(); ctx.fillStyle = e.fill; ctx.fill();
    } else {
      ctx.setTransform(e.ax, e.ay, e.bx, e.by, e.o[0]+OX, e.o[1]+OY);
      ctx.font = (e.bold ? "bold " : "") + (chh-5) + "px Menlo, monospace";
      ctx.textBaseline = "top";
      ctx.fillStyle = e.color;
      ctx.fillText(e.s, 1, 2);
      if (e.ul) ctx.fillRect(1, chh-4, (e.w||0)-2, 1.2);
    }
  }
  document.getElementById("diag").textContent =
    "camera: " + CAMERA.kind + " rot=[" + CAMERA.rot + "] (frozen; never written)\n"
    + "scene:  rx=" + scenePose.rx.toFixed(3) + " ry=" + scenePose.ry.toFixed(3)
    + "  drag rotates the assembly; there is no camera control to reach\n"
    + (window.__linkdiag || "");
  requestAnimationFrame(draw);
}

/* ---- native pointer: the glass is a real surface -------------------
   The interface is a rigid plane at z=0, so the render transform inverts
   exactly: mouse -> canvas px -> undo the assembly rotation -> cell. That
   makes cells clickable and text selectable AT ANY ROTATION.
   plain drag on the glass = select (like HTML); a no-move click goes to
   window.termClick(row, col); SHIFT + two-finger pan (trackpad wheel,
   Safari included) rotates the assembly. Drag never rotates. */
/* clickable regions the app registers as window.termHits = [{row,c0,c1}].
   The pointer cursor is native to them, at any rotation. */
function _overClickable(cell) {
  if (!cell) return false;
  var hits = window.termHits;
  if (!hits) return false;
  for (var i = 0; i < hits.length; i++) {
    var h = hits[i];
    if (h && cell[0] === h.row && cell[1] >= h.c0 && cell[1] < h.c1) return true;
  }
  return false;
}
/* a cell shows the text I-beam only if it actually holds a glyph; empty
   cells are not selectable text and get the default arrow. */
function _overGlyph(cell) {
  if (!cell) return false;
  var row = term[cell[0]];
  var g = row && row[cell[1]];
  return !!(g && g.ch && g.ch !== " ");
}
function _canvasPoint(e) {
  /* client px -> buffer px -> divide out the uniform content scale, so the
     result is in grid coordinates whatever the window size */
  var cv = document.getElementById("cv");
  var r = cv.getBoundingClientRect();
  return [(e.clientX - r.left) * (cv.width / r.width),
          (e.clientY - r.top) * (cv.height / r.height)];
}
function screenToCell(px, py) {
  if (!CFG) return null;
  var chw = CFG.ch[0], chh = CFG.ch[1];
  var cx = CFG.canvas[0]*chw/2, cy = CFG.canvas[1]*chh/2;
  var X = px - cx, Y = py - cy;   /* OX=OY=0: interface at the origin */
  var cyr = Math.cos(scenePose.ry), syr = Math.sin(scenePose.ry);
  var cxr = Math.cos(scenePose.rx), sxr = Math.sin(scenePose.rx);
  if (Math.abs(cyr) < 0.05 || Math.abs(cxr) < 0.05) return null; /* edge-on */
  var x = X / cyr;
  var y = (Y - x * syr * sxr) / cxr;
  var c = Math.floor((x + cx) / chw), r = Math.floor((y + cy) / chh);
  if (r < 0 || r >= ROWS || c < 0 || c >= COLS) return null;
  return [r, c];
}
function _selNorm() {
  if (!SEL) return null;
  var a = SEL.a[0]*COLS + SEL.a[1], f = SEL.f[0]*COLS + SEL.f[1];
  return [Math.min(a, f), Math.max(a, f)];
}
function _selSpan(row) {
  var n = _selNorm();
  if (!n) return null;
  var r0 = Math.floor(n[0]/COLS), r1 = Math.floor(n[1]/COLS);
  if (row < r0 || row > r1) return null;
  return [row === r0 ? n[0]%COLS : 0, row === r1 ? n[1]%COLS : COLS-1];
}
function _selText() {
  var n = _selNorm();
  if (!n) return "";
  var out = [];
  for (var r = Math.floor(n[0]/COLS); r <= Math.floor(n[1]/COLS); r++) {
    var sp = _selSpan(r), line = "";
    for (var c = sp[0]; c <= sp[1]; c++) line += term[r][c].ch;
    out.push(line.replace(/\s+$/, ""));
  }
  return out.join("\n");
}
document.addEventListener("copy", function(e){
  var t = _selText();
  if (!t) return;
  e.clipboardData.setData("text/plain", t);
  e.preventDefault();
});
var selecting = false, selMoved = false;
window.addEventListener("mousedown", function(e){
  var pt = _canvasPoint(e), cell = screenToCell(pt[0], pt[1]);
  if (!cell) return;
  selecting = true; selMoved = false;
  SEL = {a: cell, f: cell};
  e.preventDefault();
});
window.addEventListener("mouseup", function(e){
  if (selecting && SEL && !selMoved) {
    var cell = SEL.a;
    SEL = null;                       /* a click clears, like HTML */
    if (window.termClick) window.termClick(cell[0], cell[1]);
  }
  selecting = false;
});
window.addEventListener("mousemove", function(e){
  var cv = document.getElementById("cv");
  var pt = _canvasPoint(e);
  if (!selecting) {
    var cell = screenToCell(pt[0], pt[1]);
    cv.style.cursor = _overClickable(cell) ? "pointer"
                    : _overGlyph(cell) ? "text" : "default";
    return;
  }
  var cell = screenToCell(pt[0], pt[1]);
  if (cell) {
    if (cell[0] !== SEL.a[0] || cell[1] !== SEL.a[1]) selMoved = true;
    SEL.f = cell;
  }
});
/* SHIFT + two-finger pan rotates. Trackpads deliver both axes; a shifted
   mouse wheel lands on deltaX in some browsers, so both feed rotation.
   passive:false because a shifted pan must not scroll the page. */
window.addEventListener("wheel", function(e){
  if (!e.shiftKey) return;
  e.preventDefault();
  scenePose.ry += e.deltaX * 0.004;
  scenePose.rx += e.deltaY * 0.004;
}, {passive: false});

/* touch: TAP ONLY. A short single tap clicks the cell (the play chip);
   everything else is left to the browser. No touch selection, no touch
   rotation. */
function _touchCanvasPoint(t) {
  var cv = document.getElementById("cv");
  var r = cv.getBoundingClientRect();
  return [(t.clientX - r.left) * (cv.width / r.width),
          (t.clientY - r.top) * (cv.height / r.height)];
}
var touchStartCell = null, touchStartT = 0, touchStartXY = null;
window.addEventListener("touchstart", function(e){
  touchStartCell = null;
  if (e.target !== document.getElementById("cv")) return;
  if (e.touches.length !== 1) return;
  touchStartCell = screenToCell.apply(null, _touchCanvasPoint(e.touches[0]));
  touchStartT = Date.now();
  touchStartXY = [e.touches[0].clientX, e.touches[0].clientY];
}, {passive: true});
window.addEventListener("touchend", function(e){
  if (!touchStartCell) return;
  var moved = false;
  if (e.changedTouches.length === 1 && touchStartXY) {
    var dx = e.changedTouches[0].clientX - touchStartXY[0];
    var dy = e.changedTouches[0].clientY - touchStartXY[1];
    moved = Math.sqrt(dx*dx + dy*dy) > 10;
  }
  if (!moved && Date.now() - touchStartT < 500 && window.termClick)
    window.termClick(touchStartCell[0], touchStartCell[1]);
  touchStartCell = null;
}, {passive: true});
document.getElementById("flat").onclick = function(){ scenePose.rx = scenePose.ry = 0; };

fetch(SCRIPT_BASE + "scene.cfg").then(function(r){
  if (!r.ok) throw new Error("scene.cfg: HTTP " + r.status);
  return r.text();
}).then(function(text){
  CFG = parseCfg(text);
  MAXZ = NO_TARGET * CFG.depth[0];
  /* the data layer: dashboard.js drives this with full ANSI repaints over
     plain HTTP. Same hook the SSE feed drove in the source. */
  window.termFeed = feedAnsi;
  requestAnimationFrame(draw);
}).catch(function(err){
  document.getElementById("diag").textContent = "FATAL: " + err.message;
  throw err;   /* fail loud: no scene, no render, no fallback */
});
