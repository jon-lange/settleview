/* PerpChart: the Perps screen's live mark-price chart, drawn on a plain <canvas> (display only: no pan, zoom or
   drag-to-set; stops and take profits are set in the form and approved with a slide).
   Data: only the mark prices this page has received (the view and the live stream's pushes, [ms, mark | null]);
   nothing is back-filled or invented. A null point (no quote) or a gap longer than GAP_MS breaks the line.
   Lines: entry (info), stop loss (down, dashed), take profit (up, dashed), liquidation price (down, dotted) labelled
   with its source ("Kalshi" or "estimate"); the amber and red warning bands are the price zones where the distance to
   the liquidation price is under the server's amber / red settings (domain/perps.liquidation_distance:
   long (mark - liq) / mark, short (liq - mark) / mark). A liquidation price far outside the marks is not allowed to
   flatten the line: it is named at the plot's edge with an arrow instead.
   Colours: read from the CSS theme tokens (style.css :root) when the chart is made and on every theme change (the
   'btc-theme' event from theme.js). A missing or non-hex token is an error (the caller shows "Chart unavailable"),
   never a guessed colour. */
(function () {
  'use strict';
  var GAP_MS = 20000;   // the stream pings every 15 s; a longer silence is a gap, not a flat price
  var MIN_SPAN_MS = 60000;
  var TOKENS = { bg: '--surface', grid: '--grid', text: '--text-3', ink: '--text', entry: '--info', down: '--down',
    up: '--up', amber: '--warn', label: '--text-2' };
  var FONT = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif';
  function colors() {
    var cs = window.getComputedStyle(document.documentElement), out = {};
    Object.keys(TOKENS).forEach(function (k) {
      var v = cs.getPropertyValue(TOKENS[k]).trim();
      if (!/^#[0-9a-fA-F]{6}$/.test(v)) throw new Error('theme token ' + TOKENS[k] + ' is missing or not #rrggbb (' + (v || 'empty') + ')');
      out[k] = v;
    });
    return out;
  }
  function rgba(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + a + ')';
  }
  function pad(n) { return String(n).padStart(2, '0'); }
  function hms(ms) { var d = new Date(ms); return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); }
  function px(p) { return '$' + p.toFixed(4); }
  function ok(x) { return typeof x === 'number' && isFinite(x); }

  var live = [];
  window.addEventListener('btc-theme', function () {
    live.forEach(function (ch) {
      try { ch.c = colors(); ch.draw(); } catch (e) { if (window.console) console.error(e); }
    });
  });

  function PerpChart(canvas) {
    if (!canvas || !canvas.getContext) throw new Error('Chart canvas missing');
    this.cv = canvas; this.c = colors(); this.d = null;
    var self = this;
    if (window.ResizeObserver) { this.ro = new ResizeObserver(function () { self.draw(); }); this.ro.observe(canvas); }
    live.push(this);
  }

  /* d: {pts [[ms, mark | null]], now_ms, side 'long' | 'short' | null, entry, stop, tp, liq (numbers or null),
         liq_label ('Kalshi' | 'estimate'), amber, red (fractions or null)} */
  PerpChart.prototype.set = function (d) { this.d = d; this.draw(); };

  PerpChart.prototype.draw = function () {
    var cv = this.cv, d = this.d, c = this.c;
    var w = cv.clientWidth, h = cv.clientHeight, dpr = window.devicePixelRatio || 1;
    if (!w || !h) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    var ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = c.bg; ctx.fillRect(0, 0, w, h);
    if (!d) return;
    var pts = (d.pts || []).filter(function (p) { return ok(p[0]); });
    var vals = pts.filter(function (p) { return ok(p[1]); }).map(function (p) { return p[1]; });
    if (!vals.length) return;
    // a liquidation price reserves a second row under the times (its label goes there when it lies off the plot)
    var L = 8, R = 74, T = 10, Bm = ok(d.liq) ? 34 : 20, pw = w - L - R, ph = h - T - Bm;
    if (pw < 40 || ph < 40) return;
    var t1 = ok(d.now_ms) ? Math.max(d.now_ms, pts[pts.length - 1][0]) : pts[pts.length - 1][0];
    var t0 = Math.min(pts[0][0], t1 - MIN_SPAN_MS);
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    var mid = (lo + hi) / 2, span = Math.max(hi - lo, mid * 0.002);
    // entry / stop / take profit always on the axis; the liquidation price only when it is near the marks
    [d.entry, d.stop, d.tp].forEach(function (x) { if (ok(x)) { lo = Math.min(lo, x); hi = Math.max(hi, x); } });
    var liqIn = ok(d.liq) && d.liq >= lo - 3 * span && d.liq <= hi + 3 * span;
    if (liqIn) { lo = Math.min(lo, d.liq); hi = Math.max(hi, d.liq); }
    if (hi - lo < span) { lo = mid - span / 2; hi = mid + span / 2; }
    var padY = (hi - lo) * 0.08; lo -= padY; hi += padY;
    var X = function (t) { return L + (t - t0) / (t1 - t0) * pw; };
    var Y = function (p) { return T + (hi - p) / (hi - lo) * ph; };
    var clampY = function (y) { return Math.max(T, Math.min(T + ph, y)); };

    // warning bands: the price zones where the distance to liquidation is under amber / red
    if (ok(d.liq) && (d.side === 'long' || d.side === 'short')) {
      var zone = function (f, color) {
        if (!ok(f) || f <= 0 || f >= 1) return;
        var edge = d.side === 'long' ? d.liq / (1 - f) : d.liq / (1 + f);
        var ya = clampY(Y(d.liq)), yb = clampY(Y(edge));
        if (Math.abs(ya - yb) < 0.5) return;
        ctx.fillStyle = color; ctx.fillRect(L, Math.min(ya, yb), pw, Math.abs(ya - yb));
      };
      zone(d.amber, rgba(c.amber, 0.16));
      zone(d.red, rgba(c.down, 0.18));
    }

    // grid + price axis
    ctx.font = '11px ' + FONT; ctx.textBaseline = 'middle'; ctx.lineWidth = 1;
    for (var i = 0; i <= 4; i++) {
      var gp = lo + (hi - lo) * i / 4, gy = Math.round(Y(gp)) + 0.5;
      ctx.strokeStyle = c.grid; ctx.beginPath(); ctx.moveTo(L, gy); ctx.lineTo(L + pw, gy); ctx.stroke();
      ctx.fillStyle = c.text; ctx.textAlign = 'left'; ctx.fillText(px(gp), L + pw + 6, gy);
    }
    ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic'; ctx.fillStyle = c.text;
    var ty = T + ph + 15;
    ctx.fillText(hms(t0), L, ty);
    ctx.textAlign = 'right'; ctx.fillText(hms(t1), L + pw, ty);

    // level lines with labels kept apart on the right
    var labels = [];
    var level = function (p, color, dash, width, title) {
      if (!ok(p)) return;
      var y = Math.round(Y(p)) + 0.5;
      if (y < T || y > T + ph) return;
      ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash);
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + pw, y); ctx.stroke(); ctx.restore();
      labels.push({ y: y, text: title + ' ' + px(p), color: color });
    };
    level(d.entry, c.entry, [], 1, 'entry');
    level(d.tp, c.up, [6, 4], 1.5, 'take profit');
    level(d.stop, c.down, [6, 4], 1.5, 'stop');
    if (liqIn) level(d.liq, c.down, [2, 3], 2, 'liquidation' + (d.liq_label ? ' (' + d.liq_label + ')' : ''));

    // the mark line (breaks on a missing quote or a silence longer than GAP_MS)
    ctx.save(); ctx.strokeStyle = c.ink; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.beginPath();
    var prev = null, last = null;
    pts.forEach(function (p) {
      if (!ok(p[1])) { prev = null; return; }
      var x = X(p[0]), y = Y(p[1]);
      if (prev && p[0] - prev[0] <= GAP_MS) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      prev = p; last = p;
    });
    ctx.stroke(); ctx.restore();
    if (last) {
      ctx.fillStyle = c.ink; ctx.beginPath(); ctx.arc(X(last[0]), Y(last[1]), 3.5, 0, Math.PI * 2); ctx.fill();
    }

    // labels: inside the plot at its right edge, nudged apart so none overlap
    labels.sort(function (a, b) { return a.y - b.y; });
    for (var j = 1; j < labels.length; j++) if (labels[j].y - labels[j - 1].y < 14) labels[j].y = labels[j - 1].y + 14;
    ctx.font = '600 11px ' + FONT; ctx.textAlign = 'right'; ctx.textBaseline = 'bottom';
    labels.forEach(function (l) {
      var tw = ctx.measureText(l.text).width;
      ctx.fillStyle = rgba(c.bg, 0.85); ctx.fillRect(L + pw - tw - 8, l.y - 14, tw + 6, 13);
      ctx.fillStyle = l.color; ctx.fillText(l.text, L + pw - 5, l.y - 2);
    });
    // a liquidation price off the chart: named on its own row under the times, with the way it lies
    if (ok(d.liq) && !liqIn) {
      var txt = (d.liq < lo ? '▼ ' : '▲ ') + 'liquidation ' + px(d.liq) + (d.liq_label === 'estimate' ? ' (est.)' : d.liq_label ? ' (' + d.liq_label + ')' : '') + ' off chart';
      ctx.fillStyle = c.down; ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
      ctx.fillText(txt, L, h - 4);
    }
  };

  PerpChart.colors = colors;
  window.PerpChart = PerpChart;
})();
