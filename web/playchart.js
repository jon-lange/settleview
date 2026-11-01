/* PlayChart: the phone Play screen's live "price to beat" chart on TradingView Lightweight Charts v5.0.8.
   One BTC line (BaselineSeries: green fill above the strike, red below), x fixed from the window's open to its close
   so the time left is empty space, price lines for the strike / entry / stop / reference levels, an entry marker, an
   amber danger band next to the stop, and a small second chart for odds (a venue's Up mid, or the held side's bid with
   the auto-sell line). Recorded points only: the grid carries a price forward at most GAP_S seconds; longer gaps stay
   gaps. In a trade, the server's "likely close" cone (1σ / 2σ) is drawn from now to the close. The Coach keeps CockpitChart (chart.js); nothing here changes it.
   Colours: read from the page's CSS theme tokens (style.css :root) when the chart is made, and re-read on every theme
   change (the 'btc-theme' event from index.html's head script: the settings switch, or the device's light/dark change
   while on System). A missing or non-hex token is an error (the caller shows "Chart unavailable"), never a guess. */
(function () {
  'use strict';
  var STEP_S = 3;    // the grid: one slot every 3 s from the payload's window open to close (300 slots for 15 min,
                     // 100 for 5 min); the x range is always the window's own length (logical index ~ time)
  var GAP_S = 10;    // a price is carried forward at most this long; beyond it the slot is empty
  function pad(n) { return String(n).padStart(2, '0'); }
  function hm(t) { var d = new Date(t * 1000); return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
  function hms(t) { var d = new Date(t * 1000); return hm(t) + ':' + pad(d.getSeconds()); }
  function usd0(p) { return '$' + Math.round(p).toLocaleString('en-US'); }
  function rgba(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + a + ')';
  }
  // Token -> chart role. bg = the card surface; ink = the price line; strike = the price-to-beat line; blue = entry.
  var TOKENS = { bg: '--surface', text: '--text-3', grid: '--grid', level: '--line-strong', up: '--up', down: '--down',
    amber: '--warn', blue: '--info', ink: '--text', strike: '--strike', label: '--text' };
  function colors() {
    var cs = window.getComputedStyle(document.documentElement), out = {};
    Object.keys(TOKENS).forEach(function (k) {
      var v = cs.getPropertyValue(TOKENS[k]).trim();
      if (!/^#[0-9a-fA-F]{6}$/.test(v)) throw new Error('theme token ' + TOKENS[k] + ' is missing or not #rrggbb (' + (v || 'empty') + ')');
      out[k] = v;
    });
    [['--area-op', 'areaOp'], ['--cone-1', 'cone1'], ['--cone-2', 'cone2']].forEach(function (t) {
      var op = parseFloat(cs.getPropertyValue(t[0]));
      if (!(op > 0 && op <= 1)) throw new Error('theme token ' + t[0] + ' is missing or out of range');
      out[t[1]] = op;
    });
    return out;
  }
  var live = [];  // every PlayChart made, re-themed together
  window.addEventListener('btc-theme', function () {
    if (!live.length) return;
    var c;
    try { c = colors(); } catch (e) { if (window.console) console.error(e); return; }
    live.forEach(function (pc) { pc.theme(c); });
  });
  function reduced() { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } }

  // [[ms, value], ...] -> one entry per STEP_S slot from t0 to t1 (seconds): {time, value} or whitespace {time}.
  function grid(pts, t0, t1) {
    var out = [], j = 0, last = null;
    for (var t = t0; t <= t1; t += STEP_S) {
      while (j < pts.length && pts[j][0] / 1000 <= t + STEP_S / 2) { last = pts[j]; j++; }
      if (last && t - last[0] / 1000 <= GAP_S && last[1] != null) out.push({ time: t, value: last[1] });
      else out.push({ time: t });
    }
    return out;
  }
  function snap(sec, t0) { return t0 + Math.round((sec - t0) / STEP_S) * STEP_S; }

  var FONT = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif';
  // The colour part of a chart's options (applied again on a theme change).
  function chartColors(c) {
    return {
      layout: { background: { type: 'solid', color: c.bg }, textColor: c.text },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      crosshair: { vertLine: { color: c.text, labelBackgroundColor: c.label } }
    };
  }
  function btcFills(c, on) {
    var a = on ? c.areaOp : 0, b = on ? 0.02 : 0;
    return { topLineColor: c.ink, bottomLineColor: c.ink,
      topFillColor1: rgba(c.up, a), topFillColor2: rgba(c.up, b), bottomFillColor1: rgba(c.down, b), bottomFillColor2: rgba(c.down, a) };
  }
  function bandFills(c) {
    var x = rgba(c.amber, 0.22);
    return { topFillColor1: x, topFillColor2: x, bottomFillColor1: x, bottomFillColor2: x };
  }
  function makeChart(LC, el, c, extra) {
    return LC.createChart(el, Object.assign({
      autoSize: true,
      // Attribution: the logo is off on this small chart; the credit is in Play's settings fold ("Charts by TradingView").
      layout: { background: { type: 'solid', color: c.bg }, textColor: c.text, fontSize: 11, fontFamily: FONT, attributionLogo: false },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.1, bottom: 0.1 } },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, tickMarkFormatter: hm },
      localization: { timeFormatter: hms },
      // Phone: no pan / zoom, so a vertical drag scrolls the page and a tap or horizontal drag moves the crosshair.
      handleScroll: { mouseWheel: false, pressedMouseMove: false, horzTouchDrag: false, vertTouchDrag: false },
      handleScale: false,
      crosshair: { mode: 1, horzLine: { visible: false, labelVisible: false }, vertLine: { color: c.text, width: 1, style: 3, labelVisible: true, labelBackgroundColor: c.label } }
    }, extra || {}));
  }

  /* The "likely close" cone (in a trade): a series primitive drawn under the BTC line from the server's cone payload.
     Two nested bands fan out from BTC now (cone.btc at cone.now_ms) to the server's 1σ and 2σ settlement ranges at the
     close (cone.bands["1"] / ["2"] at cone.end_ms); the widening follows the square root of the time elapsed (a random
     walk), so only the two end ranges and the start point are data, the curve between them is the shape of that
     model. Fill opacity from the --cone-1 / --cone-2 tokens, colour --info, re-read on a theme change. */
  var CONE_STEPS = 24;
  function Cone(owner) { this.o = owner; this.d = null; this.req = null; this.series = null; var self = this;
    this.view = { zOrder: function () { return 'bottom'; }, renderer: function () { return { draw: function (t) { self.draw(t); } }; } }; }
  Cone.prototype.attached = function (p) { this.series = p.series; this.req = p.requestUpdate; };
  Cone.prototype.detached = function () { this.series = null; this.req = null; };
  Cone.prototype.updateAllViews = function () {};
  Cone.prototype.paneViews = function () { return this.d ? [this.view] : []; };
  Cone.prototype.set = function (d) { this.d = d; if (this.req) this.req(); };
  Cone.prototype.draw = function (target) {
    var d = this.d, s = this.series, o = this.o, c = o.c;
    if (!d || !s) return;
    var ts = o.chart.timeScale();
    // logicalToCoordinate takes whole slots only (a fraction comes back as 0): interpolate between the two neighbours
    var x = function (ms) {
      var l = (ms / 1000 - d.s0) / STEP_S, a = Math.floor(l), xa = ts.logicalToCoordinate(a), xb = ts.logicalToCoordinate(a + 1);
      return xa == null || xb == null ? null : xa + (xb - xa) * (l - a);
    };
    var y = function (p) { return s.priceToCoordinate(p); };
    var band = function (lo, hi) {
      var up = [], dn = [];
      for (var i = 0; i <= CONE_STEPS; i++) {
        var f = Math.sqrt(i / CONE_STEPS), t = d.now + (d.end - d.now) * i / CONE_STEPS;
        up.push([x(t), y(d.btc + (hi - d.btc) * f)]); dn.push([x(t), y(d.btc + (lo - d.btc) * f)]);
      }
      return up.concat(dn.reverse());
    };
    target.useMediaCoordinateSpace(function (scope) {
      var ctx = scope.context, x0 = x(d.now), x1 = x(d.end), yb = y(d.btc);
      if (x0 == null || x1 == null || yb == null) return;
      var fill = function (poly, a) {
        if (poly.some(function (q) { return q[0] == null || q[1] == null; })) return;
        ctx.beginPath(); poly.forEach(function (q, i) { if (i) ctx.lineTo(q[0], q[1]); else ctx.moveTo(q[0], q[1]); });
        ctx.closePath(); ctx.fillStyle = rgba(c.blue, a); ctx.fill();
      };
      ctx.save();
      fill(band(d.b2[0], d.b2[1]), c.cone2);
      fill(band(d.b1[0], d.b1[1]), c.cone1);
      // now: a dotted vertical, and BTC now carried flat to the close (where the cone starts)
      ctx.setLineDash([2, 3]); ctx.lineWidth = 1; ctx.strokeStyle = rgba(c.blue, 0.9);
      ctx.beginPath(); ctx.moveTo(x0, yb); ctx.lineTo(x1, yb); ctx.stroke();
      ctx.strokeStyle = c.text;
      ctx.beginPath(); ctx.moveTo(x0, 0); ctx.lineTo(x0, scope.mediaSize.height); ctx.stroke();
      ctx.setLineDash([]);
      // labels inside the bands at the close: "Likely close · 2σ" and "1σ" (one combined label when they would touch)
      ctx.font = '600 11px ' + FONT; ctx.fillStyle = c.blue; ctx.textAlign = 'right'; ctx.textBaseline = 'top';
      var y2 = y(d.b2[1]), y1 = y(d.b1[1]), top = 4, xr = x1 - 6;
      if (y2 == null || y1 == null) { ctx.restore(); return; }
      y2 = Math.max(top, y2 + 4); y1 = Math.max(top, y1 + 4);
      if (y1 - y2 < 15) ctx.fillText('Likely close · 1σ / 2σ', xr, y2);
      else { ctx.fillText('Likely close · 2σ', xr, y2); ctx.fillText('1σ', xr, y1); }
      ctx.restore();
    });
  };

  function PlayChart(el, oddsEl) {
    var LC = window.LightweightCharts;
    if (!LC) throw new Error('Chart library missing');
    var c = this.c = colors(), self = this;
    this.last = null; this.lastOdds = null;
    this.LC = LC;
    this.chart = makeChart(LC, el, c);
    this.include = [];
    // The danger band: a BaselineSeries whose line sits at the band's far edge and whose base is the stop, so its
    // fill is exactly the band. Drawn first, under the BTC line.
    this.band = this.chart.addSeries(LC.BaselineSeries, {
      lineVisible: false, topLineColor: 'transparent', bottomLineColor: 'transparent',
      topFillColor1: rgba(c.amber, 0.22), topFillColor2: rgba(c.amber, 0.22),
      bottomFillColor1: rgba(c.amber, 0.22), bottomFillColor2: rgba(c.amber, 0.22),
      lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
      priceFormat: { type: 'custom', minMove: 1, formatter: usd0 },  // the first series sets the axis's format
      autoscaleInfoProvider: function () { return null; }
    });
    this.btc = this.chart.addSeries(LC.BaselineSeries, {
      lineWidth: 2, topLineColor: c.ink, bottomLineColor: c.ink,
      topFillColor1: rgba(c.up, c.areaOp), topFillColor2: rgba(c.up, 0.02),
      bottomFillColor1: rgba(c.down, 0.02), bottomFillColor2: rgba(c.down, c.areaOp),
      priceLineVisible: false, lastValueVisible: true,
      lastPriceAnimation: reduced() ? 0 : 1,  // the pulsing dot at the latest point
      crosshairMarkerRadius: 4,
      priceFormat: { type: 'custom', minMove: 1, formatter: usd0 },
      autoscaleInfoProvider: function (orig) {
        var r = orig();
        if (!self.include.length) return r;
        var lo = Math.min.apply(null, self.include), hi = Math.max.apply(null, self.include);
        if (r && r.priceRange) { lo = Math.min(lo, r.priceRange.minValue); hi = Math.max(hi, r.priceRange.maxValue); }
        if (hi - lo < 20) { var m = (hi + lo) / 2; lo = m - 10; hi = m + 10; }
        return { priceRange: { minValue: lo, maxValue: hi } };
      }
    });
    this.cone = new Cone(this);
    this.btc.attachPrimitive(this.cone);
    this.markers = LC.createSeriesMarkers(this.btc, []);
    this.lines = [];
    this.oddsChart = makeChart(LC, oddsEl, c, {
      layout: { background: { type: 'solid', color: c.bg }, textColor: c.text, fontSize: 10, fontFamily: FONT, attributionLogo: false },
      timeScale: { visible: false },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.08, bottom: 0.08 } },
      grid: { vertLines: { visible: false }, horzLines: { visible: false } }
    });
    var pct = { type: 'custom', minMove: 0.01, formatter: function (p) { return Math.round(p * 100) + '¢'; } };
    this.oddsLine = this.oddsChart.addSeries(LC.LineSeries, { color: c.blue, lineWidth: 2, priceFormat: pct, priceLineVisible: false,
      crosshairMarkerVisible: false, autoscaleInfoProvider: function () { return { priceRange: { minValue: 0, maxValue: 1 } }; } });
    this.oddsHalf = this.oddsLine.createPriceLine({ price: 0.5, color: c.grid, lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: '' });
    this.oddsTp = null;
    this.cross = null;
    this.chart.subscribeCrosshairMove(function (p) {
      if (!self.cross) return;
      var d = p && p.time != null ? p.seriesData.get(self.btc) : null;
      self.cross(d && d.value != null ? { time: Number(p.time), btc: d.value } : null);
    });
    live.push(this);
  }

  // A theme change: new colours on both charts and every series, then the last data is drawn again so the price lines,
  // the entry marker and the odds line pick them up.
  PlayChart.prototype.theme = function (c) {
    this.c = c;
    this.chart.applyOptions(chartColors(c));
    this.oddsChart.applyOptions({ layout: chartColors(c).layout, crosshair: chartColors(c).crosshair });
    this.band.applyOptions(bandFills(c));
    this.btc.applyOptions(btcFills(c, true));
    this.oddsHalf.applyOptions({ color: c.grid });
    if (this.last) this.set(this.last);
    if (this.lastOdds) this.setOdds(this.lastOdds);
  };

  PlayChart.prototype.onCross = function (fn) { this.cross = fn; };

  /* d: {t0, t1 (ms), pts [[ms, price]], strike, stop, near (danger band width $), entry {ts, btc, side} | null,
         levels [{price, title}], danger (bool: BTC inside the band),
         cone: the server's cone {now_ms, end_ms, btc, bands {"1": [lo, hi], "2": [lo, hi]}} | null (null = hidden)} */
  PlayChart.prototype.set = function (d) {
    var c = this.c, s0 = Math.floor(d.t0 / 1000), s1 = Math.floor(d.t1 / 1000), self = this;
    this.last = d;
    var data = grid(d.pts, s0, s1);
    var base = d.strike != null ? d.strike : (d.pts.length ? d.pts[0][1] : 0);
    this.btc.applyOptions(Object.assign({ baseValue: { type: 'price', price: base } }, btcFills(c, d.strike != null)));
    this.btc.setData(data);
    // the band, from the stop towards the price, across the whole window
    if (d.stop != null && d.near) {
      var edge = d.stop + (d.side === 'UP' ? d.near : -d.near);
      this.band.applyOptions({ baseValue: { type: 'price', price: d.stop } });
      this.band.setData(data.map(function (p) { return { time: p.time, value: edge }; }));
    } else this.band.setData([]);
    this.include = [];
    if (d.strike != null) this.include.push(d.strike);
    if (d.stop != null) this.include.push(d.stop);
    // the cone (validated by the caller): its 1σ range at the close is kept on the price axis (the 2σ band may run
    // past the plot's edge early in a window rather than flatten the BTC line)
    var k = d.cone;
    if (k) {
      this.include.push(k.bands['1'][0], k.bands['1'][1], k.btc);
      this.cone.set({ s0: s0, now: k.now_ms, end: k.end_ms, btc: k.btc, b1: k.bands['1'], b2: k.bands['2'] });
    } else this.cone.set(null);
    this.lines.forEach(function (l) { self.btc.removePriceLine(l); });
    this.lines = [];
    var add = function (price, color, width, style, title, axis) {
      self.lines.push(self.btc.createPriceLine({ price: price, color: color, lineWidth: width, lineStyle: style, title: title, axisLabelVisible: axis }));
    };
    (d.levels || []).forEach(function (l) { add(l.price, c.level, 1, 1, l.title, !!l.title); });
    if (d.strike != null) add(d.strike, c.strike, 1, 3, 'Price to beat', true);
    if (d.entry && d.entry.btc != null) add(d.entry.btc, c.blue, 1, 1, 'entry', true);
    if (d.stop != null) add(d.stop, c.down, 2, 2, 'stop', true);
    // The entry: a marker at the fill time and BTC then (v5 price-anchored marker; a filled circle stands in for a ring).
    var marks = [];
    if (d.entry && d.entry.btc != null && d.entry.ts >= d.t0 && d.entry.ts <= d.t1) {
      marks.push({ time: snap(Math.floor(d.entry.ts / 1000), s0), position: 'atPriceMiddle', price: d.entry.btc, shape: 'circle', color: c.blue, size: 1 });
    }
    this.markers.setMarkers(marks);
    this.chart.timeScale().setVisibleLogicalRange({ from: 0, to: data.length - 1 });
  };

  // The close's x: the plot's right edge (the range is fixed to end at the close), read after layout.
  PlayChart.prototype.closeX = function () { var w = this.chart.timeScale().width(); return w > 0 ? w : null; };

  /* o: {t0, t1 (ms), pts [[ms, 0..1]], autosell (0..1) | null, tone 'up' | 'down' | 'info'} */
  var TONES = { up: 'up', down: 'down', info: 'blue' };
  PlayChart.prototype.setOdds = function (o) {
    var s0 = Math.floor(o.t0 / 1000), s1 = Math.floor(o.t1 / 1000), data = grid(o.pts, s0, s1);
    if (!TONES[o.tone]) throw new Error('odds tone must be up, down or info (got ' + o.tone + ')');
    this.lastOdds = o;
    this.oddsLine.applyOptions({ color: this.c[TONES[o.tone]] });
    this.oddsLine.setData(data);
    if (this.oddsTp) { this.oddsLine.removePriceLine(this.oddsTp); this.oddsTp = null; }
    if (o.autosell != null) {
      this.oddsTp = this.oddsLine.createPriceLine({ price: o.autosell, color: this.c.amber, lineWidth: 1, lineStyle: 2,
        axisLabelVisible: true, title: 'auto-sell ' + Math.round(o.autosell * 100) + '¢' });
    }
    this.oddsChart.timeScale().setVisibleLogicalRange({ from: 0, to: data.length - 1 });
  };

  PlayChart.STEP_S = STEP_S;
  PlayChart.colors = colors;
  window.PlayChart = PlayChart;
})();
