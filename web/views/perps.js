/* Perps: Kalshi's BTC perpetual (KXBTCPERP prod / KXBTCPERP1 demo) on the phone and the desktop, next to the Kalshi
   and Polymarket event screens (the venue row's third choice; the choice is remembered on this device).
   Data (the shared perps contract, routes under the PIN middleware): GET /api/perps/view once on entry, then
   EventSource('/api/perps/stream'): every `event: view` carries the View fields that changed, merged here field by
   field; `event: ping` (every 15 s) proves the stream is alive. No message for STALE_MS, or a dropped stream, shows a
   visible "stale" state with every entry and exit slide off (old numbers are never shown as live) and reconnects.
   Ordering: every payload carries server_ms. A full view from an HTTP answer (the GET, approve / adopt / mode / kill)
   older than the last payload applied is dropped, since the stream re-sends only fields that changed after it and the
   old values would stay on screen; stream frames arrive in order and always apply. The clock offset only moves forward
   within one connection.
   The presence heartbeat (POST /api/trading/heartbeat) is the app's one (web/app.js), shared with Play.
   Trading: every action is a slide with a ticket (KD1). The numbers shown before a slide are a preview ticket from
   the same prepare route (sent once the inputs have been still for EDIT_MS, refreshed when it expires); the slide
   prepares a fresh, bound ticket on drag start and shows it in the dock while dragging, and approves THAT ticket on
   slide end (KTD15: POST /api/perps/approve {token, shown}). A ticket that is not back yet, expired, or no longer
   matches the position, the trigger, the stream's freshness or the protective price snaps the slider back (nothing is
   sent at a ticket the user did not see). Refusals and outcomes are the server's words, shown word for word.
   Every number on screen is a payload field or arithmetic on payload fields (distance from the mark, dollar effect of
   a stop, BTC price = contract price / contract_btc); a missing one shows "—" with the payload's *_reason.
   Perps trade from the primary perps account (KD4 as changed by the user 2026-10-30): before every entry or add the
   screen says the whole perps balance backs the position (the ticket's at_risk_text).
   Layout (the user's approved wireframe, 2026-10-30): the Play screen's parts in Play's order — gear, countdown (to
   the next funding) and KILL; THE CALL; the chart with the signal lights; in a position the P&L card, keep vs close
   and one display-only advice line; the dock with side, amount chips, the leverage slider (the user's call) and one
   slide; a settings sheet.
   The call and the lights are Play's own read (GET /api/play/now for Kalshi 15m, every READ_MS while this screen is
   open): the lean is the majority of the 1m / 5m / 15m pressure and crowd lights, said as a lean, never as an edge. */
(function () {
  'use strict';
  var B = window.BTC, api = B.api, fmt = B.fmt;
  var PROD_CONFIRM = 'I understand this uses real money';
  var OPEN_KEY = 'perps.open', COLL_KEY = 'perps.collateral', FILL_KEY = 'perps.fill', LIMIT_KEY = 'perps.limit', BAND_KEY = 'perps.band';
  var FILLS = ['market', 'all_or_nothing', 'limit'];
  var FILL_NAMES = { market: 'Market', all_or_nothing: 'All or nothing', limit: 'Limit price' };
  var BANDS = [0.0025, 0.005, 0.01, 0.02];  // protective band choices (fractions, domain/perps.protective_price)
  // The contract's banner priority; codes it does not list follow in the server's order.
  var PRIORITY = ['unknown_order', 'no_stop', 'diverged', 'paused', 'stale', 'near_liquidation'];
  var ENTRY_SLIDE = 0.9, EXIT_SLIDE = 0.7;  // as Play: an entry needs 90% of the track, an exit or trigger 70%
  var STALE_MS = 20000, RECONNECT_MS = 3000, EDIT_MS = 400, REFUSED_RETRY_MS = 10000;
  var REBIND_RETRY_MS = 2000;  // a preview bound to another position / trigger is re-prepared at most this often
  var HIST_MS = 15 * 60000, HIST_CAP = 3000;  // the chart keeps this page's received marks for 15 min
  var REDUCE = [0.25, 0.5, 0.75], REDUCE_NAMES = { 0.25: 'Quarter', 0.5: 'Half', 0.75: 'Three quarters' };
  var ACTS = ['close', 'reduce', 'add', 'protect'];
  var ACT_NAMES = { close: 'Close', reduce: 'Reduce', add: 'Add', protect: 'Stop / TP' };
  var DOCK_ACTS = ['add', 'reduce', 'protect', 'close'];  // the dock's order (the wireframe's)
  var MODE_NAMES = { perp_demo: 'Demo (Kalshi demo exchange)', perp_prod: 'Production (REAL MONEY)', off: 'Off' };
  var TRIGGER_WORDS = { none: 'None set', pending: 'Setting on Kalshi…', active: 'Active on Kalshi', failed: 'Failed' };
  var CHIPS = [1, 2, 5, 10], STAKE_DEFAULT = 10;  // amount chips ($), "Other" beside them; $10 until one is picked (the user, 2026-10-30), capped at the perps cash
  var TP_PROMPT_KEY = 'perps.tpPrompt', PCT_STEP = 0.25;  // the take-profit / stop sheet after an entry (Play's auto-sell sheet)
  var RECORD_LAST = 10;
  var READ_MS = 3000, READ_STALE_MS = 15000;  // Play's read (the call and the lights): polled, and shown as old after this
  var LIGHTS = ['pressure_60', 'pressure_300', 'pressure_900', 'crowd'];
  var THEMES = ['system', 'light', 'dark'], THEME_NAMES = { system: 'System', light: 'Light', dark: 'Dark' };

  function load(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function save(key, v) { try { if (v == null) window.localStorage.removeItem(key); else window.localStorage.setItem(key, String(v)); } catch (e) { /* storage blocked */ } }
  function ok(x) { return typeof x === 'number' && isFinite(x); }
  var usd = fmt.usd, sUsd = fmt.signedUsd;  // app.js: the shared formatters
  function usd4(x) { return fmt.usd(x, 4); }
  function px(x) { return ok(x) ? '$' + x.toFixed(4) : '—'; }
  function pctf(f, dp) { return ok(f) ? (f * 100).toFixed(dp == null ? 1 : dp) + '%' : '—'; }
  function cnt(n) { return ok(n) ? n.toLocaleString('en-US', { maximumFractionDigits: 4 }) : '—'; }
  function lev(x) { return ok(x) ? (Math.round(x * 100) / 100) + 'x' : '—'; }
  function errText(e) { return (e && e.message) || String(e); }
  function sideWord(s) { return s === 'long' ? 'Long' : s === 'short' ? 'Short' : '—'; }
  function sideSign(s) { return s === 'long' ? 1 : s === 'short' ? -1 : 0; }
  function arrow(l) { return l === 'UP' ? '▲' : l === 'DOWN' ? '▼' : '■'; }
  // The perps call is Play's call (the user's choice, 2026-10-30: "just like the other play modes"): the Kalshi 15-min
  // market's lean at Play's thresholds, ENTER UP -> long, ENTER DOWN -> short, WAIT -> no side.
  function perpCall(call) {
    var c = call || {}, g = c.agree || {}, n = g.n != null ? g.n : null, of = n != null ? g.of : null;
    var why = c.reason || 'No reason was sent.';
    if (c.action === 'ENTER UP') return { lean: 'long', headline: '▲ LONG', reason: why, n: n, of: of };
    if (c.action === 'ENTER DOWN') return { lean: 'short', headline: '▼ SHORT', reason: why, n: n, of: of };
    return { lean: null, headline: c.headline || 'WAIT', reason: why, n: null, of: null };
  }
  // A typed dollar amount: "$1,234.5" -> 1234.5; blank or not above 0 is an error with the reason (never coerced).
  function parseAmt(raw, what) {
    var t = String(raw == null ? '' : raw).trim().replace(/^\$/, '').replace(/,/g, ''), n = Number(t);
    if (!t) return { err: 'Enter the ' + what + ' in dollars.' };
    if (!isFinite(n) || n <= 0) return { err: 'The ' + what + ' must be a dollar amount above $0.' };
    return { v: Math.round(n * 100) / 100 };
  }
  // A typed price per contract (4 dp, Kalshi's tick); blank = none.
  function parsePx(raw, what) {
    var t = String(raw == null ? '' : raw).trim().replace(/^\$/, '').replace(/,/g, ''), n = Number(t);
    if (!t) return { v: null };
    if (!isFinite(n) || n <= 0) return { err: 'The ' + what + ' must be a price above $0 per contract.' };
    return { v: Math.round(n * 10000) / 10000 };
  }
  // Remembered entry options: a stored valid value wins; nothing stored = the stated default; anything else is shown
  // as unrecognised (the default is used and named), never silently swapped.
  function loadFill() { var r = load(FILL_KEY); return r == null ? { v: 'market', unknown: null } : FILLS.indexOf(r) >= 0 ? { v: r, unknown: null } : { v: 'market', unknown: r }; }
  function loadBand() {
    var r = load(BAND_KEY), n = Number(r);
    if (r == null) return { v: null, unknown: null };
    return BANDS.indexOf(n) >= 0 && String(n) === String(r).trim() ? { v: n, unknown: null } : { v: null, unknown: r };
  }
  function loadPrice(key) { var r = load(key); if (r == null) return { v: '', unknown: null }; var p = parsePx(r, 'x'); return p.err || p.v == null ? { v: '', unknown: r } : { v: String(p.v), unknown: null }; }
  function loadColl() { var r = load(COLL_KEY); if (r == null) return { v: '', unknown: null }; var a = parseAmt(r, 'x'); return a.err ? { v: '', unknown: r } : { v: String(a.v), unknown: null }; }

  // Opens the Perps screen from the venue row or the first-run choice (index.html) and remembers it on this device.
  window.perpsOpen = function () {
    save(OPEN_KEY, '1');
    try { history.replaceState(null, '', location.pathname + location.search + '#perps'); } catch (e) { /* keep the hash */ }
    Alpine.store('app').view = 'perps';
  };

  window.perpsView = function () {
    var es = null, reconnectT = null, chart = null, chartErr = '';
    var hist = [];  // [[server ms, mark | null]]: the marks this page received (never back-filled)
    var fill0 = loadFill(), band0 = loadBand(), limit0 = loadPrice(LIMIT_KEY), coll0 = loadColl();
    return {
      v: null, error: '', live: false, everLive: false, lastMsg: 0, liveAt: null, offset: 0, offsetSet: false, lastServerMs: null, tick: 0,
      side: null, slideSide: null, collInput: coll0.v || (coll0.unknown == null ? String(STAKE_DEFAULT) : ''), collUnknown: coll0.unknown, levVal: 1, levAuto: true,
      collAuto: !coll0.v && coll0.unknown == null,  // the $10 default is in use (nothing picked or saved yet)
      slInput: '', tpInput: '',
      fill: fill0.v, fillUnknown: fill0.unknown, limitInput: limit0.v, limitUnknown: limit0.unknown,
      band: band0.v, bandUnknown: band0.unknown, optsOpen: false,
      act: 'close', redPick: 0.5, redCount: '', trigSl: '', trigTp: '', trigFor: null,
      preview: null, inflight: false, edited: 0, drag: null,
      slideX: 0, sliding: false, sx0: 0, busy: false, pulse: '', msg: '', msgCls: '',
      allBanners: false, adoptAsk: null, outcomeSeen: null, noStopSeen: false,
      modeForm: { mode: '', confirm: '' }, modeMsg: '', modeBusy: false,
      killBusy: false, killMsg: '', pc: null, chartErr: '',
      read: null, readAt: 0, readErr: '', readBusy: false, sigTip: '', collOther: false, sheet: false,
      tpSheet: false, tpFor: null, tpWant: false, tpOffered: {}, tpOn: false, tpPct: 1, slOn: false, slPct: 1, tpBusy: false, tpMsg: '',
      tpPrompt: load(TP_PROMPT_KEY) !== 'off', tpNoMore: false, rec: null, recErr: '', recOpen: false,
      themeMode: window.BTCTheme ? window.BTCTheme.mode : 'system', themeUnknown: window.BTCTheme ? window.BTCTheme.unknown : null,
      themeMsg: window.BTCTheme ? '' : 'Theme control unavailable (the page head script did not run).',

      init: function () {
        var self = this, store = Alpine.store('app'), on = function () { return store.view === 'perps'; };
        if (load(OPEN_KEY) === '1' && store.view === 'play' && !location.hash.replace(/^#\/?/, '')) store.view = 'perps';
        window.onView(this, 'perps', function () { self.connect(); self.loadRead(); });
        Alpine.effect(function () { if (store.view !== 'perps') self.disconnect(); });
        self.loadPc();
        setInterval(function () {  // a hidden page or another screen does not tick (B6#4)
          if (!on() || document.visibilityState !== 'visible') return;
          self.tick++; self.checkLive(); self.previewTick(); self.drawChart();
        }, 500);
        setInterval(function () { if (on() && document.visibilityState === 'visible') self.loadRead(); }, READ_MS);
        document.addEventListener('visibilitychange', function () {
          if (document.visibilityState !== 'visible') { self.slideReset(); self.disconnect(); return; }
          if (on()) { self.connect(); self.loadRead(); }
        });
      },
      loadPc: function () {
        var self = this;
        api.get('/api/access').then(function () { self.pc = true; }, function (e) { if (e && e.status === 403) self.pc = false; });
      },
      phone: function () { return this.pc === false; },
      goVenue: function (x) {
        if (this.sliding || this.busy) return;
        save(OPEN_KEY, null);
        try { history.replaceState(null, '', location.pathname + location.search + '#play'); } catch (e) { /* keep the hash */ }
        Alpine.store('app').view = 'play';
        window.dispatchEvent(new CustomEvent('pl-venue', { detail: x }));
      },

      // ----- data: one GET, then the push stream; liveness from its messages -----
      connect: function () {
        var self = this;
        if (es) return;
        clearTimeout(reconnectT); reconnectT = null;
        self.offsetSet = false;  // a new connection measures the offset afresh (the device clock may have been reset)
        api.get('/api/perps/view').then(function (r) { if (self.merge(r, true)) self.error = ''; }, function (e) { self.error = errText(e); });
        try { es = new EventSource('/api/perps/stream'); } catch (e) { self.error = 'Could not open the live stream: ' + errText(e); self.reconnect(); return; }
        es.addEventListener('view', function (ev) {
          var d;
          try { d = JSON.parse(ev.data); } catch (e) { self.error = 'The live stream sent a view that is not JSON; it is ignored.'; return; }
          self.lastMsg = Date.now(); self.live = true; self.everLive = true;
          self.merge(d);
        });
        es.addEventListener('ping', function () { self.lastMsg = Date.now(); });
        es.onerror = function () {
          if (self.live) self.liveAt = self.lastMsg;
          self.live = false; self.slideCheck();
          if (es && es.readyState === 2) { es = null; self.reconnect(); }  // CLOSED: the browser will not retry by itself
        };
      },
      disconnect: function () {
        if (es) { es.close(); es = null; }
        clearTimeout(reconnectT); reconnectT = null;
        if (this.live) this.liveAt = this.lastMsg;
        this.live = false;
      },
      reconnect: function () {
        var self = this;
        if (reconnectT) return;
        reconnectT = setTimeout(function () { reconnectT = null; if (Alpine.store('app').view === 'perps' && document.visibilityState === 'visible') self.connect(); }, RECONNECT_MS);
      },
      // A stream that went quiet past STALE_MS (no view, no ping) is treated as dead: stale, closed, reopened.
      checkLive: function () {
        if (this.live && Date.now() - this.lastMsg > STALE_MS) {
          this.liveAt = this.lastMsg; this.live = false; this.slideCheck();
          if (es) { es.close(); es = null; }
          this.reconnect();
        } else if (!es && !reconnectT && document.visibilityState === 'visible') this.reconnect();
      },
      // full: a whole View from an HTTP answer (dropped when older than the last payload applied); else a stream frame.
      // Returns whether the payload was applied.
      merge: function (d, full) {
        if (!d || typeof d !== 'object') return false;
        if (typeof d.enabled === 'boolean') Alpine.store('app').perpsOff = !d.enabled;
        if (full && ok(d.server_ms) && this.lastServerMs != null && d.server_ms < this.lastServerMs) return false;
        if (full && !ok(d.server_ms)) this.error = 'The server sent a view with no server_ms; it is shown, but its age is unknown.';
        var was = this.v, hadPos = !!(was && was.position);
        this.v = Object.assign({}, was || {}, d);
        if (ok(d.server_ms)) {
          this.lastServerMs = d.server_ms;
          var off = d.server_ms - Date.now();
          if (!this.offsetSet || off > this.offset) { this.offset = off; this.offsetSet = true; }
        }
        if ('quote' in d || 'position' in d || !was) this.noteMark();
        if (!!this.v.position !== hadPos) { this.preview = null; this.act = 'close'; this.trigFor = null; }
        var ns = this.hasBanner('no_stop');
        if (ns && !this.noStopSeen && !this.sliding) this.act = 'close';  // R14: the close slide beside the warning
        this.noStopSeen = ns;
        if (this.v.mode && !this.modeForm.mode) this.modeForm.mode = this.v.mode;
        this.collFit();
        this.tpCheck();
        if ('outcome' in d && this.recOpen) this.loadRecord();
        this.slideCheck();
        var self = this;
        this.$nextTick(function () { self.drawChart(); });
        return true;
      },
      now: function () { return Date.now() + this.offset; },
      noteMark: function () {
        var v = this.v, q = v.quote, m = q && ok(q.mark) ? q.mark : null, t = ok(v.server_ms) ? v.server_ms : this.now();
        if (hist.length && t <= hist[hist.length - 1][0]) return;
        hist.push([t, m]);
        while (hist.length && (hist.length > HIST_CAP || t - hist[0][0] > HIST_MS)) hist.shift();
      },

      // ----- header -----
      on: function () { return !!(this.v && this.v.enabled); },
      pos: function () { return (this.v && this.v.position) || null; },
      mk: function () { return (this.v && this.v.market) || {}; },
      quote: function () { return (this.v && this.v.quote) || null; },
      mark: function () { var q = this.quote(); return q && ok(q.mark) ? q.mark : null; },
      real: function () { return !!(this.v && this.v.mode === 'perp_prod'); },
      modeLine: function () {
        var v = this.v;
        if (!v) return 'Loading…';
        if (!v.enabled) return v.enabled_reason || 'Perps are switched off (no reason sent).';
        return v.mode === 'perp_prod' ? 'REAL MONEY' : v.mode === 'perp_demo' ? 'Demo · Kalshi demo money' : v.mode === 'off' ? 'Perps trading is off' : 'Mode: ' + (v.mode || '—');
      },
      tickerLine: function () {
        var m = this.mk();
        return (m.ticker || '—') + ' · 1 contract = ' + (ok(m.contract_btc) ? m.contract_btc + ' BTC' : '—');
      },
      liveText: function () {
        this.tick;
        if (this.live) return 'Live';
        return this.everLive ? 'Stale' : 'Connecting…';
      },
      liveCls: function () { return this.live ? 'pv-live-on' : 'pv-live-off'; },
      staleText: function () {
        if (this.live) return '';
        return this.everLive ? 'Live data stopped at ' + fmt.time(this.liveAt) + '. Reconnecting; entries and exits are off until it is back.'
          : 'Connecting to live data… entries and exits stay off until it answers.';
      },
      balLine: function () {
        var v = this.v, b = v && v.balance;
        if (!v) return '';
        if (!b) return 'Perps balance: — (' + (v.balance_reason || 'no reason sent') + ')';
        return 'Perps balance: ' + usd(b.equity) + ' · ' + usd(b.available) + ' available';
      },
      markLine: function () {
        var v = this.v, q = this.quote();
        if (!v) return '';
        if (!q) return 'Mark price: — (' + (v.quote_reason || 'no reason sent') + ')';
        return 'Mark ' + px(q.mark) + ' · BTC ' + this.btcPx(q.mark) + ' · bid ' + px(q.bid) + ' / ask ' + px(q.ask);
      },
      // The contract price as a BTC price (contract price / contract_btc): the venue's own figure, rescaled.
      btcPx: function (p) {
        var cb = this.mk().contract_btc;
        return ok(p) && ok(cb) && cb > 0 ? '$' + Math.round(p / cb).toLocaleString('en-US') : '—';
      },
      killed: function () { return !!(this.v && this.v.killed); },
      // Quiet round KILL until engaged; engaged it is a red pill: "Killed" (a phone cannot resume) or "Resume".
      killLabel: function () { return this.killBusy ? '…' : this.killed() ? (this.phone() ? 'Killed' : 'Resume') : 'KILL'; },
      // ----- the market picker and the money line (design pass 2026-10-31): Kalshi -> BTC 15m · BTC Perps -----
      pickerLabel: function () { return 'BTC Perps' + (this.v && !this.v.enabled ? ' · off' : ''); },
      pickerSub: function () {
        var v = this.v, f = v && v.funding;
        if (!v) return 'Loading…';
        if (!v.enabled) return 'Switched off';
        if (v.mode === 'off') return 'Perps trading is off';
        return (this.pos() ? 'Your position · ' : '') + (f ? 'Funding ' + pctf(f.rate, 4) + ' · next ' + (ok(f.next_ms) ? fmt.hm(f.next_ms) + ' (' + this.fundLeft() + ')' : '—') : 'Funding — (' + (v.funding_reason || 'no reason sent') + ')');
      },
      // Live = real money (perp_prod), Paper = Kalshi demo money (perp_demo), Off = no perps mode picked.
      pillWord: function () { var m = this.v && this.v.mode; return m === 'perp_prod' ? 'Live' : m === 'perp_demo' ? 'Paper' : m === 'off' ? 'Off' : m ? '⚠ ' + m : '—'; },
      pillCls: function () { var m = this.v && this.v.mode; return m === 'perp_prod' ? 'nv-pill-prod' : m === 'perp_demo' ? 'nv-pill-sandbox' : 'nv-pill-shadow'; },
      pillAria: function () { var m = this.v && this.v.mode; return m === 'perp_prod' ? 'Live: real money' : m === 'perp_demo' ? 'Paper: Kalshi demo money' : m === 'off' ? 'Perps trading is off' : 'Unknown mode'; },
      resumeLocked: function () { return this.killed() && this.phone(); },
      // The one shared kill switch (KTD16): the existing routes reach the perps service too.
      toggleKill: function () {
        var self = this, kill = !self.killed(), fails = [];
        if (self.killBusy || (!kill && self.phone())) return;
        if (!kill && !window.confirm('Resume? New entries become possible again (each still needs your slide).')) return;
        self.killBusy = true; self.killMsg = '';
        var note = function (what) { return function (e) { fails.push(what + ': ' + errText(e)); }; };
        Promise.all([
          api.post(kill ? '/api/trading/kill' : '/api/trading/resume').catch(note('trading')),
          api.post('/api/coach/pause', { paused: kill }).catch(note('coach'))
        ]).then(function () {
          self.killMsg = fails.length ? 'Kill switch ' + (kill ? 'on' : 'off') + ' failed for ' + fails.join('; ') : '';
          Alpine.store('app').announce(fails.length ? self.killMsg : kill ? 'Kill switch on: no new entries.' : 'Resumed.');
        })['finally'](function () { self.killBusy = false; api.get('/api/perps/view').then(function (r) { self.merge(r, true); }, function () {}); });
      },

      // ----- banners: one at a time by priority, the rest folded into a count -----
      banners: function () {
        var v = this.v, list = ((v && v.banners) || []).slice(), st = this.staleText();
        if (st) list = list.filter(function (b) { return b.code !== 'stale'; }).concat([{ code: 'stale', severity: 'amber', text: st }]);
        var rank = function (b) { var i = PRIORITY.indexOf(b.code); return i < 0 ? PRIORITY.length : i; };
        return list.map(function (b, i) { return { b: b, i: i }; })
          .sort(function (a, b) { return rank(a.b) - rank(b.b) || a.i - b.i; }).map(function (x) { return x.b; });
      },
      hasBanner: function (code) { return ((this.v && this.v.banners) || []).some(function (b) { return b.code === code; }); },
      shownBanners: function () { var l = this.banners(); return this.allBanners ? l : l.slice(0, 1); },
      moreBanners: function () { return Math.max(0, this.banners().length - 1); },
      bannerCls: function (b) { return 'pv-banner pv-sev-' + (b.severity === 'red' || b.severity === 'amber' ? b.severity : 'info'); },

      // ----- the outcome (R12: the server's sentence from recorded fills) -----
      outcome: function () { var o = this.v && this.v.outcome; return o && o.text && o.at_ms !== this.outcomeSeen ? o : null; },
      closeOutcome: function () { var o = this.v && this.v.outcome; if (o) this.outcomeSeen = o.at_ms; },

      // ----- positions this app did not open (R17) -----
      foreign: function () { return (this.v && this.v.foreign) || []; },
      fDir: function (f) { return f.direction || (ok(f.size) ? (f.size < 0 ? 'short' : f.size > 0 ? 'long' : null) : null); },
      fLine: function (f) {
        return sideWord(this.fDir(f)) + ' ' + cnt(ok(f.size) ? Math.abs(f.size) : null) + ' contracts · avg entry ' + px(f.avg_entry) +
          ' · unrealized ' + sUsd(f.unrealized_pnl);
      },
      fHead: function (f) {
        return f.kind === 'app_isolated' ? 'In Kalshi\'s app (isolated, subaccount ' + f.subaccount + '): shown here read-only'
          : 'A position Settleview did not open (subaccount ' + f.subaccount + ')';
      },
      adopt: function (f, i) {
        var self = this;
        if (self.busy) return;
        self.busy = true; self.adoptAsk = null; self.msg = '';
        api.post('/api/perps/adopt', { size_shown: f.size }).then(function (r) {
          if (r && r.view) self.merge(r.view, true);
          if (r && r.ok) self.say('Adopted: Settleview now manages this position.', 'pl-done pl-info');
          else self.say(self.refusal(r), 'red');
        }, function (e) { self.say('Could not adopt: ' + errText(e), 'red'); })['finally'](function () { self.busy = false; });
      },

      // ----- the position card (R13) -----
      bandCls: function () { var p = this.pos(); return p ? 'pv-band-' + (p.band === 'red' || p.band === 'amber' ? p.band : 'ok') : ''; },
      liqText: function () {
        var p = this.pos();
        if (!p) return '—';
        return px(p.liquidation_price) + (ok(p.liquidation_price) ? ' · BTC ' + this.btcPx(p.liquidation_price) : '') + ' (' + (p.liquidation_source === 'kalshi' ? 'Kalshi' : p.liquidation_source === 'estimate' ? 'estimate' : '—') + ')';
      },
      distText: function () { var p = this.pos(); return p && ok(p.distance) ? pctf(p.distance) + ' away' : '—'; },
      fundingSoFar: function () {
        var p = this.pos();
        if (!p || !ok(p.funding_total)) return '—';
        return sUsd(p.funding_total) + (p.funding_total < 0 ? ' paid' : p.funding_total > 0 ? ' received' : '');
      },
      fundingNext: function () {
        this.tick;
        var f = this.v && this.v.funding;
        if (!f) return '— (' + ((this.v && this.v.funding_reason) || 'no reason sent') + ')';
        if (!ok(f.next_ms)) return '—';
        return fmt.hm(f.next_ms) + ' · in ' + fmt.countdown(f.next_ms - this.now()) + ' · rate ' + pctf(f.rate, 4);
      },
      trig: function () { return (this.v && this.v.trigger) || { state: 'none' }; },
      trigState: function () {
        var t = this.trig(), w = TRIGGER_WORDS[t.state] || ('Unknown state "' + t.state + '"');
        return w + (t.reason && t.state !== 'active' ? ': ' + t.reason : '');
      },
      trigCls: function () { var s = this.trig().state; return s === 'failed' ? 'pl-down' : s === 'pending' ? 'pl-warn' : s === 'active' ? 'pl-up' : ''; },
      trigLine: function () {
        var t = this.trig();
        return 'Stop loss ' + (ok(t.stop_loss) ? px(t.stop_loss) : 'none') + ' · take profit ' + (ok(t.take_profit) ? px(t.take_profit) : 'none');
      },
      bandsLine: function () {
        var s = this.v && this.v.settings;
        if (!s) return 'Warning bands: — (the server sent no settings)';
        return 'Amber when the mark is under ' + pctf(s.amber) + ' from the liquidation price, red under ' + pctf(s.red) + '. Set on the server.';
      },

      // ----- inputs -----
      edit: function () { this.edited = Date.now(); },
      pickSide: function (s) { if (this.sliding || this.busy || this.pos()) return; this.side = s; this.edit(); },
      // The side for an entry: the one picked, else the call's lean (pre-selected and marked "the call", as on Play).
      // A tap holds only until the next 15-min window of the call (as Play's `chosen`); a slide holds the call's side only
      // while the slide lasts (slideSide), so the call keeps driving the pick after a refusal or a let-go.
      sideNow: function () { return this.side || this.slideSide || this.pcall().lean; },
      entrySide: function () { var p = this.pos(); return p ? p.direction : this.sideNow(); },
      sideLabel: function (s) { return (s === 'long' ? '▲ Long' : '▼ Short') + (!this.pos() && this.pcall().lean === s ? ' · the call' : ''); },
      maxLev: function () {
        var m = this.mk(), s = this.entrySide();
        var x = s === 'long' ? m.max_leverage_long : s === 'short' ? m.max_leverage_short : null;
        return ok(x) && x >= 1 ? x : null;
      },
      levMax: function () { var x = this.maxLev(); return x == null ? 1 : x; },
      setLev: function (x) { if (ok(x)) { this.levVal = Math.round(x * 100) / 100; this.levAuto = false; this.edit(); } },
      // The cheapest leverage at which the amount buys one whole contract (step x the touch the server sizes at:
      // the ask for a long, the bid for a short), or null when a figure is missing.
      oneLev: function (side) {
        var q = this.quote(), step = this.mk().count_step, c = this.collNum();
        var touch = q ? (side === 'long' ? q.ask : side === 'short' ? q.bid : null) : null;
        return ok(touch) && ok(step) && ok(c) && c > 0 ? step * touch / c : null;
      },
      // Until the slider is moved, the leverage is the lowest that buys one contract (2% headroom for the touch
      // moving, rounded up to 0.1x), never under 1x or over Kalshi's maximum.
      levNow: function () {
        if (!this.levAuto) return this.levVal;
        var need = this.oneLev(this.entrySide());
        var x = need == null ? 1 : Math.ceil(need * 1.02 * 10 - 1e-9) / 10;
        return Math.min(Math.max(1, x), this.levMax());
      },
      levNote: function () {
        if (!this.entrySide()) return 'Pick long or short to see the maximum leverage.';
        if (this.maxLev() == null) return 'Maximum leverage: — (' + ((this.v && this.v.quote_reason) || 'Kalshi\'s leverage estimate is not loaded') + ')';
        return 'Up to ' + lev(this.maxLev()) + ' for a ' + this.entrySide() + ' at this size (Kalshi\'s own estimate).' +
          (this.levAuto ? ' Set to the lowest that buys one whole contract until you move it.' : '');
      },
      saveColl: function () { var a = parseAmt(this.collInput, 'collateral'); this.collAuto = false; if (!a.err) { save(COLL_KEY, a.v); this.collUnknown = null; } },
      // The default amount (Play's $5) never exceeds the perps cash: while it is in use it is capped at what is available.
      avail: function () { var b = this.v && this.v.balance; return b && ok(b.available) ? b.available : null; },
      collFit: function () {
        var a = this.avail();
        if (!this.collAuto || a == null || a <= 0) return;
        var x = String(Math.min(STAKE_DEFAULT, Math.floor(a * 100) / 100));
        if (x !== this.collInput) { this.collInput = x; this.edit(); }
      },
      collOff: function (x) { var a = this.avail(); return a != null && x > a + 1e-9; },
      availHint: function () {
        var a = this.avail();
        return a == null ? 'Perps cash: — (' + ((this.v && this.v.balance_reason) || 'not read yet') + ')' : 'Up to your ' + usd(a) + ' perps cash' + (this.collAuto ? ' (amount set from it until you pick one)' : '') + '.';
      },
      setFill: function (f) {
        if (FILLS.indexOf(f) < 0 || this.sliding || this.busy) return;
        this.fill = f; this.fillUnknown = null; save(FILL_KEY, f === 'market' ? null : f); this.edit();
      },
      setBand: function (b) {
        if (this.sliding || this.busy || (b != null && BANDS.indexOf(b) < 0)) return;
        this.band = b; this.bandUnknown = null; save(BAND_KEY, b); this.edit();
      },
      saveLimit: function () { var p = parsePx(this.limitInput, 'limit price'); if (!p.err) { save(LIMIT_KEY, p.v); this.limitUnknown = null; } },
      bandName: function (b) { return b == null ? 'Default (' + pctf(this.v && this.v.settings ? this.v.settings.band : null, 2) + ')' : pctf(b, 2); },
      optsSummary: function () {
        var out = [];
        if (this.fill !== 'market') out.push(this.fill === 'limit' ? 'Limit ' + (this.limitInput ? '$' + this.limitInput : '—') : FILL_NAMES[this.fill]);
        if (this.band != null) out.push('Band ' + pctf(this.band, 2));
        return out.length ? out.join(' · ') : 'Market, default band';
      },
      setAct: function (a) {
        if (ACTS.indexOf(a) < 0 || this.sliding || this.busy) return;
        this.act = a; this.edit();
        if (a === 'protect') this.fillTrig();
      },
      // The stop / take-profit form starts from the trigger Kalshi holds now.
      fillTrig: function () {
        var t = this.trig();
        this.trigSl = ok(t.stop_loss) ? String(t.stop_loss) : ''; this.trigTp = ok(t.take_profit) ? String(t.take_profit) : '';
        this.trigFor = t.updated_time == null ? null : t.updated_time;
      },
      trigEdited: function () { var t = this.trig(); return this.trigFor !== (t.updated_time == null ? null : t.updated_time); },
      clearTrig: function () { this.trigSl = ''; this.trigTp = ''; this.edit(); },
      setRed: function (x) { if (this.sliding || this.busy) return; this.redPick = x; this.edit(); },
      reduceCount: function () {
        var p = this.pos();
        if (!p || !ok(p.size)) return { err: 'The position size is not known.' };
        var size = Math.abs(p.size), n;
        if (this.redPick === 'count') {
          var t = String(this.redCount).trim(); n = Number(t);
          if (!t || !isFinite(n) || n <= 0 || Math.floor(n) !== n) return { err: 'Enter a whole number of contracts to reduce by.' };
        } else {
          n = Math.floor(size * this.redPick + 1e-9);
          if (n < 1) return { err: REDUCE_NAMES[this.redPick] + ' of ' + cnt(size) + ' contracts is less than one whole contract.' };
        }
        if (n >= size) return { err: 'That is the whole position (' + cnt(size) + ' contracts): use Close.' };
        return { v: n };
      },

      // Stop and take profit checked against the mark and the liquidation price (inline, before any request).
      // dir: the position's side; liq: the liquidation price shown (the ticket's estimate, or the position's).
      stopWhy: function (stop, dir, mark, liq) {
        if (stop == null || !dir) return '';
        if (!ok(mark)) return 'The mark price is not known, so a stop cannot be checked.';
        if (dir === 'long' && stop >= mark) return 'A long\'s stop loss must be below the mark price (' + px(mark) + ').';
        if (dir === 'short' && stop <= mark) return 'A short\'s stop loss must be above the mark price (' + px(mark) + ').';
        if (ok(liq) && (dir === 'long' ? stop <= liq : stop >= liq)) return 'That stop is past the liquidation price (' + px(liq) + '): Kalshi would liquidate the position first.';
        return '';
      },
      tpWhy: function (tp, dir, mark) {
        if (tp == null || !dir) return '';
        if (!ok(mark)) return 'The mark price is not known, so a take profit cannot be checked.';
        if (dir === 'long' && tp <= mark) return 'A long\'s take profit must be above the mark price (' + px(mark) + ').';
        if (dir === 'short' && tp >= mark) return 'A short\'s take profit must be below the mark price (' + px(mark) + ').';
        return '';
      },
      // Distance from the mark and the dollar effect if it fills there (count x price move, before fees).
      effect: function (price, dir, count, from) {
        var m = this.mark();
        if (price == null || !ok(m)) return '';
        var d = (price - m) / m, out = (d >= 0 ? '+' : '−') + Math.abs(d * 100).toFixed(2) + '% from the mark';
        if (ok(count) && ok(from)) out += ' · ' + sUsd(sideSign(dir) * count * (price - from)) + ' if hit (before fees)';
        return out;
      },
      // The entry form's stop / take profit, as typed, with its inline check (the liquidation price is the preview's).
      entrySl: function () { return parsePx(this.slInput, 'stop loss'); },
      entryTp: function () { return parsePx(this.tpInput, 'take profit'); },
      entrySlWhy: function () { var s = this.entrySl(), t = this.shown(); return s.err || this.stopWhy(s.v, this.sideNow(), this.mark(), t && t.liquidation_est); },
      entryTpWhy: function () { var s = this.entryTp(); return s.err || this.tpWhy(s.v, this.sideNow(), this.mark()); },
      entrySlFx: function () { var s = this.entrySl(), t = this.shown(); return this.entrySlWhy() ? '' : this.effect(s.v, this.sideNow(), t && t.count, this.mark()); },
      entryTpFx: function () { var s = this.entryTp(), t = this.shown(); return this.entryTpWhy() ? '' : this.effect(s.v, this.sideNow(), t && t.count, this.mark()); },
      trigSlP: function () { return parsePx(this.trigSl, 'stop loss'); },
      trigTpP: function () { return parsePx(this.trigTp, 'take profit'); },
      trigSlWhy: function () { var p = this.pos(), s = this.trigSlP(); return s.err || (p ? this.stopWhy(s.v, p.direction, p.mark, p.liquidation_price) : ''); },
      trigTpWhy: function () { var p = this.pos(), s = this.trigTpP(); return s.err || (p ? this.tpWhy(s.v, p.direction, p.mark) : ''); },
      trigSlFx: function () { var p = this.pos(), s = this.trigSlP(); return p && !this.trigSlWhy() ? this.effect(s.v, p.direction, Math.abs(p.size), p.avg_entry) : ''; },
      trigTpFx: function () { var p = this.pos(), s = this.trigTpP(); return p && !this.trigTpWhy() ? this.effect(s.v, p.direction, Math.abs(p.size), p.avg_entry) : ''; },

      // ----- the request for the current action: {kind, path, body} or {err} -----
      kind: function () { return this.pos() ? this.act : 'entry'; },
      req: function () {
        var k = this.kind(), p = this.pos();
        if (k === 'close') return { kind: k, path: '/api/perps/prepare_exit', body: { action: 'close', count: null } };
        if (k === 'reduce') { var n = this.reduceCount(); return n.err ? n : { kind: k, path: '/api/perps/prepare_exit', body: { action: 'reduce', count: n.v } }; }
        if (k === 'protect') {
          var a = this.trigSlP(), b = this.trigTpP(), w = this.trigSlWhy() || this.trigTpWhy();
          if (w) return { err: w };
          var t = this.trig();
          if (a.v === (ok(t.stop_loss) ? t.stop_loss : null) && b.v === (ok(t.take_profit) ? t.take_profit : null)) return { err: 'No change: Kalshi already holds this stop loss and take profit.' };
          return { kind: k, path: '/api/perps/prepare_trigger', body: { stop_loss: a.v, take_profit: b.v } };
        }
        var side = k === 'add' ? p.direction : this.sideNow();
        if (!side) return { err: 'Pick long or short.' };
        var c = parseAmt(this.collInput, 'collateral');
        if (c.err) return c;
        var mx = this.maxLev();
        if (mx == null) return { err: 'The maximum leverage is not known yet, so nothing can be sized.' };
        var lv = this.levNow();
        if (!(lv >= 1)) return { err: 'Leverage must be at least 1x.' };
        if (lv > mx + 1e-9) return { err: 'Leverage ' + lev(lv) + ' is over Kalshi\'s maximum ' + lev(mx) + ' for a ' + side + ': move the slider.' };
        var one = this.oneLev(side);
        if (one != null && lv < one - 1e-9) {
          return { err: one > mx + 1e-9
            ? usd(c.v) + ' cannot buy one whole contract even at Kalshi\'s maximum ' + lev(mx) + ': it needs at least ' + usd(Math.ceil(c.v * one / mx * 100) / 100) + '.'
            : 'One contract needs at least ' + usd(Math.ceil(c.v * one / lv * 100) / 100) + ' at ' + lev(lv) + ': raise the leverage to ' + lev(Math.ceil(one * 100) / 100) + ' or the amount.' };
        }
        var sl = { v: null }, tp = { v: null };
        if (k === 'entry') {
          sl = this.entrySl(); tp = this.entryTp();
          var why = sl.err || tp.err || this.stopWhy(sl.v, side, this.mark(), null) || this.tpWhy(tp.v, side, this.mark());
          if (why) return { err: why };
        }
        var lim = { v: null };
        if (this.fill === 'limit') {
          lim = parsePx(this.limitInput, 'limit price');
          if (lim.err) return lim;
          if (lim.v == null) return { err: 'Enter your limit price, or pick Market under Entry options.' };
        }
        return { kind: k, path: '/api/perps/prepare', body: { action: k, side: side, collateral: c.v, leverage: lv,
          stop_loss: sl.v, take_profit: tp.v, fill: this.fill, limit_price: lim.v, band: this.band } };
      },
      reqKey: function (r) { return r && !r.err ? r.path + ' ' + JSON.stringify(r.body) : ''; },
      // Why the current action cannot be slid right now ('' = it can). Every disabled slide says why.
      gate: function (k) {
        var v = this.v, can = (v && v.can) || {};
        if (!v) return 'Loading…';
        if (!v.enabled) return v.enabled_reason || 'Perps are switched off.';
        if (!this.live) return this.everLive ? 'Live data stopped: reconnecting' : 'Connecting to live data…';
        if (k === 'entry' || k === 'add') {
          if (v.unknown_order) return 'Checking whether your order went through';
          if (!v.ready) return v.ready_reason || 'Not ready (no reason sent)';
          if (!can.entry) return can.entry_reason || 'Entries are unavailable (no reason sent)';
          return '';
        }
        if (k === 'protect') return can.trigger ? '' : (can.trigger_reason || 'Stop and take profit are unavailable (no reason sent)');
        return can.exit ? '' : (can.exit_reason || 'Exits are unavailable (no reason sent)');
      },
      // The preview ticket: prepared once the inputs have been still for EDIT_MS, refreshed when it expires.
      previewTick: function () {
        var self = this;
        if (self.sliding || self.busy || self.inflight || !self.v) return;
        var r = self.req();
        if (r.err || self.gate(r.kind)) return;
        if (Date.now() - self.edited < EDIT_MS) return;
        var key = self.reqKey(r), p = self.preview;
        if (p && p.key === key && (p.ticket ? self.now() < p.ticket.expires_ms - 1000 && (!self.boundWhy(p.ticket, r.kind) || Date.now() - p.got < REBIND_RETRY_MS)
          : Date.now() - p.got < REFUSED_RETRY_MS)) return;
        self.inflight = true;
        api.post(r.path, r.body).then(function (res) {
          self.preview = { key: key, got: Date.now(), ticket: res && res.ok ? Object.assign({}, res.ticket, { expires_ms: res.expires_ms != null ? res.expires_ms : res.ticket.expires_ms }) : null, refusal: res && res.ok ? null : res };
        }, function (e) {
          self.preview = { key: key, got: Date.now(), ticket: null, refusal: { reason: errText(e) } };
        })['finally'](function () { self.inflight = false; });
      },
      // The ticket on screen: the one bound to the drag while sliding, else the preview for exactly these inputs, only
      // while the stream is live, the preview has not expired and it is bound to the position and trigger shown now.
      shown: function () {
        if (this.drag && this.drag.ticket) return this.drag.ticket;
        this.tick;
        var p = this.preview, r = this.req(), k = this.reqKey(r);
        if (!p || !p.ticket || p.key !== k || !this.live) return null;
        return this.now() < p.ticket.expires_ms && !this.boundWhy(p.ticket, r.kind) ? p.ticket : null;
      },
      shownRefusal: function () {
        var p = this.preview, k = this.reqKey(this.req());
        return p && p.refusal && p.key === k && !this.drag ? this.refusal(p.refusal) : '';
      },
      refusal: function (r) {
        return r && r.reason ? 'Refused: ' + r.reason : 'Refused (code ' + ((r && r.code) || 'none') + ') with no reason text from the server.';
      },
      atRisk: function () {
        var t = this.shown(), b = this.v && this.v.balance;
        if (t && t.at_risk_text) return t.at_risk_text;
        if (b && ok(b.equity)) return 'Your whole perps balance (' + usd(b.equity) + ') backs this position: a big enough move can lose all of it.';
        return 'Your whole perps balance backs this position: a big enough move can lose all of it. (Balance: — ' + ((this.v && this.v.balance_reason) || 'not loaded') + ')';
      },
      fundText: function (f) {
        if (!f) return '—';
        var a = f.est_amount;
        return fmt.hm(f.next_ms) + ' · ' + (ok(a) ? (a < 0 ? 'you pay ≈ ' + usd4(-a) : a > 0 ? 'you receive ≈ ' + usd4(a) : '≈ $0') : '—') + ' · rate ' + pctf(f.rate, 4);
      },
      // Rows of a ticket, by kind (only the fields the contract gives that kind; a missing value is "—").
      rows: function () {
        var t = this.shown(), k = this.kind(), out = [];
        if (!t) return out;
        var row = function (a, b) { out.push([a, b]); };
        if (k === 'protect') {
          row('Stop loss', ok(t.stop_loss) ? px(t.stop_loss) : 'None (removed)');
          row('Take profit', ok(t.take_profit) ? px(t.take_profit) : 'None (removed)');
          row('Applies to', 'The whole position (' + cnt(t.bound && t.bound.position_size) + ' contracts), held by Kalshi');
          return out;
        }
        row('Size', cnt(t.count) + ' contracts (' + (ok(t.btc) ? t.btc.toFixed(4) : '—') + ' BTC)');
        row('Notional', usd(t.notional));
        row('Price', (t.protective_text || '—') + (t.time_in_force ? ' · ' + t.time_in_force : ''));
        row('Fee', '≈ ' + usd4(t.fee_est));
        if (k === 'entry' || k === 'add') {
          row('Collateral × leverage', usd(t.collateral) + ' × ' + lev(t.leverage));
          row('Worst case', usd(t.worst_case));
          if (k === 'entry') row('Liquidation price', px(t.liquidation_est) + ' (' + (t.liquidation_label || 'estimate') + ')');
          row('Next funding', this.fundText(t.funding));
        }
        if (k === 'entry') {
          row('Stop loss', ok(t.stop_loss) ? px(t.stop_loss) : 'None');
          row('Take profit', ok(t.take_profit) ? px(t.take_profit) : 'None');
        }
        if (k === 'add') {
          row('New average entry', px(t.new_avg_entry));
          row('New size', cnt(t.new_size) + ' contracts');
          row('New liquidation price', px(t.new_liquidation_est) + ' (estimate)');
        }
        if (k === 'close' || k === 'reduce') {
          row('Remaining', cnt(t.remaining) + ' contracts');
          row('Est. realized P&L', sUsd(t.est_realized_pnl) + ' after fees');
        }
        return out;
      },
      chips: function () { var t = this.shown(); return (t && Array.isArray(t.chips)) ? t.chips : []; },
      ticketHead: function () {
        var k = this.kind(), p = this.pos();
        if (k === 'entry') return 'Open ' + (this.sideNow() ? sideWord(this.sideNow()).toLowerCase() : '—');
        if (k === 'add') return 'Add to your ' + (p ? p.direction : '');
        if (k === 'close') return 'Close the whole position';
        if (k === 'reduce') return 'Reduce the position';
        return 'Stop loss and take profit';
      },
      ticketAge: function () {
        this.tick;
        var t = this.shown();
        if (!t) return '';
        if (this.drag && this.drag.ticket) return 'Bound to this slide · expires in ' + Math.max(0, Math.ceil((t.expires_ms - this.now()) / 1000)) + ' s';
        return 'Preview from the server at ' + fmt.time(this.preview.got) + '. Sliding fetches the final ticket and shows it before you let go.';
      },

      // ----- the slide (KTD15): prepare on drag start, approve the shown ticket on slide end -----
      slideWhy: function () {
        var r = this.req();
        return this.gate(r.kind || this.kind()) || r.err || '';
      },
      slideOk: function () { return !this.busy && !this.slideWhy(); },
      slideMax: function () { var t = this.$refs.pvtrack; return t ? Math.max(60, t.clientWidth - 64) : 200; },
      slideNeed: function () { var k = this.kind(); return k === 'entry' || k === 'add' ? ENTRY_SLIDE : EXIT_SLIDE; },
      slideReset: function () { this.sliding = false; this.slideX = 0; this.drag = null; this.slideSide = null; },
      flash: function (t) {
        var self = this;
        self.pulse = t; clearTimeout(self.pulseTimer);
        self.pulseTimer = setTimeout(function () { self.pulse = ''; }, 2500);
      },
      slideStart: function (e) {
        var self = this;
        if (!self.slideOk()) return;
        if (!self.pos() && !self.side) self.slideSide = self.sideNow();  // the call's side, as shown, holds for this slide
        var r = self.req();
        self.sliding = true; self.sx0 = e.clientX - self.slideX; self.pulse = '';
        self.drag = { kind: r.kind, key: self.reqKey(r), pending: true, ticket: null, token: null };
        var d = self.drag;  // Alpine's reactive proxy (the raw object would never equal self.drag below)
        try { e.target.setPointerCapture(e.pointerId); } catch (err) { /* older browsers */ }
        api.post(r.path, r.body).then(function (res) {
          if (self.drag !== d) return;  // let go or snapped back already
          d.pending = false;
          if (!res || !res.ok) { self.slideReset(); self.say(self.refusal(res), 'red'); self.flash('Refused — see the message'); return; }
          d.token = res.token;
          d.ticket = Object.assign({}, res.ticket, { expires_ms: res.expires_ms != null ? res.expires_ms : res.ticket.expires_ms });
          self.slideCheck();
        }, function (err) {
          if (self.drag !== d) return;
          self.slideReset(); self.say('Refused: ' + errText(err), 'red');
        });
      },
      slideMove: function (e) { if (this.sliding) this.slideX = Math.max(0, Math.min(this.slideMax(), e.clientX - this.sx0)); },
      slideEnd: function () {
        if (!this.sliding) return;
        this.sliding = false;
        var d = this.drag;
        if (this.slideX < this.slideMax() * this.slideNeed()) { this.slideReset(); return; }
        if (!d || d.pending || !d.ticket) { this.slideReset(); this.flash('The ticket was not back yet — slide again'); return; }
        if (!this.slideCheck()) return;
        this.slideX = this.slideMax();
        this.approve(d);
      },
      // Snap back when what the shown ticket was bound to has changed (checked on every push and at release).
      slideCheck: function () {
        var d = this.drag;
        if (!d || this.busy) return true;
        var why = this.frozenWhy(d);
        if (!why) return true;
        this.slideReset(); this.flash(why);
        return false;
      },
      frozenWhy: function (d) {
        var g = this.gate(d.kind);
        if (g) return g;
        var t = d.ticket, p = this.pos();
        if (!t) return '';
        if (this.now() >= t.expires_ms) return 'The ticket expired — slide again';
        var bw = this.boundWhy(t, d.kind);
        if (bw) return bw;
        if (d.kind === 'protect' || !ok(t.protective_price)) return '';
        var q = this.quote(), buys = d.kind === 'entry' || d.kind === 'add' ? t.side === 'long' : !!(p && p.direction === 'short');
        if (!q) return 'No live quote — slide again';
        if (buys && ok(q.ask) && q.ask > t.protective_price + 1e-9) return 'The ask moved past ' + (t.protective_text || px(t.protective_price)) + ' — slide again';
        if (!buys && ok(q.bid) && q.bid < t.protective_price - 1e-9) return 'The bid moved past ' + (t.protective_text || px(t.protective_price)) + ' — slide again';
        return '';
      },
      // Whether a ticket is still bound to the position and (for a stop / take profit) the trigger on screen ('' = yes).
      boundWhy: function (t, kind) {
        var b = t.bound, v = this.v, p = this.pos();
        if (!b) return '';
        var sign = p ? sideSign(p.direction) : 0, size = p && ok(p.size) ? Math.abs(p.size) : 0;
        if (b.position_sign !== sign || Math.abs(Math.abs(b.position_size || 0) - size) > 1e-9) return 'Your position changed — slide again';
        var tu = v && v.trigger && v.trigger.updated_time != null ? v.trigger.updated_time : null;
        if (kind === 'protect' && (b.trigger_updated_time == null ? null : b.trigger_updated_time) !== tu) return 'Your stop or take profit changed — slide again';
        return '';
      },
      approve: function (d) {
        var self = this;
        self.busy = true; self.msg = '';
        api.post('/api/perps/approve', { token: d.token, shown: d.ticket }).then(function (r) {
          if (r && r.view) self.merge(r.view, true);
          if (!r || !r.ok) { self.say(self.refusal(r), 'red'); return; }
          self.say(r.outcome && r.outcome.text ? r.outcome.text : 'Sent. The result shows here when Kalshi records it.', 'pl-done pl-info');
          if (r.outcome && r.outcome.at_ms != null && self.v && self.v.outcome && self.v.outcome.at_ms === r.outcome.at_ms) self.outcomeSeen = r.outcome.at_ms;
          if (d.kind === 'protect') self.trigFor = null;
          if (d.kind === 'entry') { self.tpWant = true; self.tpCheck(); }
          try { if (navigator.vibrate) navigator.vibrate(30); } catch (err) { /* no haptics */ }
        }, function (err) {
          self.say('Could not confirm what happened: ' + errText(err) + ' The screen shows "Checking whether your order went through" until Kalshi\'s answer is known.', 'red');
        })['finally'](function () { self.busy = false; self.slideReset(); self.preview = null; });
      },
      say: function (text, cls) { this.msg = text; this.msgCls = cls || ''; Alpine.store('app').announce(text); },
      slideCls: function () {
        var k = this.kind(), s = this.entrySide();
        var base = k === 'entry' || k === 'add' ? (s === 'long' ? 'pl-slide-up' : s === 'short' ? 'pl-slide-down' : 'pl-slide-none') : 'pl-slide-exit';
        return base + (this.slideOk() ? '' : ' off') + (this.pulse ? ' pl-pulse' : '');
      },
      slideText: function () {
        this.tick;
        if (this.busy) return 'Sending…';
        if (this.pulse) return this.pulse;
        if (this.drag && this.drag.pending) return 'Getting your ticket…';
        if (this.drag && this.drag.ticket) return 'Let go past ' + Math.round(this.slideNeed() * 100) + '% to send';
        var why = this.slideWhy();
        if (why) return why;
        var k = this.kind(), p = this.pos(), c = parseAmt(this.collInput, 'x').v, r = this.req();
        if (k === 'entry') return 'Slide to open ' + this.sideNow() + ' · ' + usd(c) + ' × ' + lev(this.levNow());
        if (k === 'add') return 'Slide to add · ' + usd(c) + ' × ' + lev(this.levNow());
        if (k === 'close') return 'Slide to close ' + cnt(p && Math.abs(p.size)) + ' contracts';
        if (k === 'reduce') return 'Slide to reduce by ' + cnt(r.body && r.body.count) + ' contracts';
        return r.body && r.body.stop_loss == null && r.body.take_profit == null ? 'Slide to remove stop and take profit' : 'Slide to set stop / take profit';
      },
      // The dock's summary of the ticket on screen (the bound one while dragging).
      dockLine: function () {
        var t = this.shown(), k = this.kind();
        if (!t) return '';
        if (k === 'protect') return 'Stop ' + (ok(t.stop_loss) ? px(t.stop_loss) : 'none') + ' · take profit ' + (ok(t.take_profit) ? px(t.take_profit) : 'none');
        return cnt(t.count) + ' contracts (' + (ok(t.btc) ? t.btc.toFixed(4) : '—') + ' BTC) · ' + (t.protective_text || '—');
      },
      dockLine2: function () {
        var t = this.shown(), k = this.kind();
        if (!t || k === 'protect') return '';
        if (k === 'close' || k === 'reduce') return 'Fee ≈ ' + usd4(t.fee_est) + ' · remaining ' + cnt(t.remaining) + ' · est. P&L ' + sUsd(t.est_realized_pnl) + ' after fees';
        return 'Worst case ' + usd(t.worst_case) + ' · fee ≈ ' + usd4(t.fee_est) + ' · liquidation ' + px(k === 'add' ? t.new_liquidation_est : t.liquidation_est) + ' (estimate)';
      },
      slideAria: function () { return this.slideText() + '. Drag right to confirm.'; },

      // ----- mode (perp_demo / perp_prod / off; production needs the typed real-money confirmation) -----
      modes: function () { return (this.v && this.v.modes_available) || []; },
      modeName: function (m) { return MODE_NAMES[m] || m; },
      canSetMode: function () {
        var f = this.modeForm;
        if (this.modeBusy || !f.mode || !this.v || f.mode === this.v.mode) return false;
        if (f.mode === 'perp_prod') return f.confirm === PROD_CONFIRM;  // the phone too (the user's call, 2026-10-30)
        return true;
      },
      setMode: function () {
        var self = this, f = self.modeForm;
        if (!self.canSetMode()) return;
        self.modeBusy = true; self.modeMsg = '';
        api.post('/api/perps/mode', { mode: f.mode, confirm: f.mode === 'perp_prod' ? f.confirm : null }).then(function (r) {
          if (r && r.view) self.merge(r.view, true);
          self.modeMsg = r && r.ok ? 'Switched to ' + self.modeName(f.mode) + '.' : self.refusal(r).replace(/^Refused/, 'Not switched');
          f.confirm = '';
        }, function (e) { self.modeMsg = 'Not switched: ' + errText(e); })['finally'](function () { self.modeBusy = false; });
      },

      // ----- Play's read: the call, the 15-min market bar and the signal lights (display only) -----
      loadRead: function () {
        var self = this;
        if (self.readBusy) return;
        self.readBusy = true;
        api.get('/api/play/now?venue=kalshi&span=15m').then(function (r) {
          var was = self.read && self.read.window, now = r.window || null;
          if (was && now && was.end !== now.end) self.side = null;  // a new window: the new call picks again
          self.read = { call: r.call || {}, signals: r.signals || [], window: now, btc: r.btc || null };
          self.readAt = Date.now(); self.readErr = '';
        }, function (e) { self.readErr = errText(e); })['finally'](function () { self.readBusy = false; });
      },
      readOld: function () { this.tick; return !this.read || Date.now() - this.readAt > READ_STALE_MS; },
      pcall: function () {
        if (this.readOld()) {
          return { lean: null, headline: 'WAIT', n: null, of: null, reason: this.readErr ? 'Signals unavailable: ' + this.readErr
            : this.read ? 'The signals are over 15 s old: waiting for a fresh read.' : 'Loading the signals…' };
        }
        return perpCall(this.read.call);
      },
      callCls: function () { var l = this.pcall().lean; return l === 'long' ? 'pl-up' : l === 'short' ? 'pl-down' : 'pl-flat'; },
      agreeText: function () { var c = this.pcall(); return c.n != null ? c.n + ' of ' + c.of + ' agree' : ''; },
      rcall: function () { return (this.read && !this.readOld() && this.read.call) || {}; },
      pct: function (x) { return ok(x) ? Math.round(x * 100) + '%' : '—'; },
      upWidth: function () { var m = this.rcall().mid_up; return (ok(m) ? Math.round(m * 100) : 0) + '%'; },
      fairLeft: function () { var f = this.rcall().fair_up; return ok(f) ? Math.max(0, Math.min(100, f * 100)) + '%' : null; },
      fairLabelLeft: function () { var f = this.rcall().fair_up; return ok(f) ? Math.max(30, Math.min(70, f * 100)) + '%' : null; },
      downPct: function () { var m = this.rcall().mid_up; return ok(m) ? this.pct(1 - m) : '—'; },
      marketHead: function () {
        var r = this.read, w = r && r.window, b = r && r.btc;
        if (this.readOld()) return 'Kalshi 15-min: —';
        return 'Kalshi 15-min · ' + (w ? fmt.hm(w.end) : '—') + ' above ' + (b && ok(b.strike) ? '$' + Math.round(b.strike).toLocaleString('en-US') : '—');
      },
      sigChips: function () {
        var sig = this.readOld() ? [] : this.read.signals;
        var k$ = function (x) { return x == null ? '—' : '$' + (x >= 1000 ? (x / 1000).toFixed(x >= 10000 ? 0 : 1) + 'k' : Math.round(x)); };
        return sig.filter(function (s) { return LIGHTS.indexOf(s.key) >= 0; }).map(function (s) {
          var crowd = s.key === 'crowd';
          var t = crowd ? 'Crowd ' + arrow(s.lean) + (s.up_usd != null ? ' ' + k$(s.up_usd) + '/' + k$(s.down_usd) : ' —') : s.name.replace(/ pressure$/, '') + ' ' + arrow(s.lean);
          var tip = crowd ? 'Dollars bought ▲ Up / ▼ Down on Kalshi\'s 15-min market in the last minute' + (s.value ? ': ' + s.value : ' (no data)') + '. ' + (s.note || '')
            : 'The Coach\'s ' + s.name.toLowerCase() + ': ' + (s.value || 'no lean') + ' (▲ leans Up, ▼ leans Down, ■ no lean).';
          return { key: s.key, text: t, cls: s.lean === 'UP' ? 'pl-up' : s.lean === 'DOWN' ? 'pl-down' : 'pl-flat', tip: tip };
        });
      },
      sigTap: function (ch) { this.sigTip = this.sigTip === ch.tip ? '' : ch.tip; },

      // ----- the header: countdown to the next funding (perps have no window) and the cash line -----
      fundLeft: function () {
        this.tick;
        var f = this.v && this.v.funding;
        return f && ok(f.next_ms) ? fmt.countdown(f.next_ms - this.now()) : '—';
      },
      cashLine: function () {
        var v = this.v, b = v && v.balance, cb = this.mk().contract_btc;
        if (!v) return '';
        return 'Perps cash ' + (b ? usd(b.available) : '— (' + (v.balance_reason || 'no reason sent') + ')') + ' · 1 contract = ' + (ok(cb) ? cb + ' BTC' : '—');
      },

      // ----- the call card's tiles (sizes and liquidation from the preview ticket; nothing before it is back) -----
      targetPx: function () {
        var q = this.quote();
        var s = this.sideNow();
        if (!s || !q) return '—';
        return this.btcPx(s === 'short' ? q.bid : q.ask);
      },
      targetSub: function () {
        var t = this.shown();
        if (!this.sideNow()) return 'Pick ▲ Long or ▼ Short below';
        return sideWord(this.sideNow()) + (this.side ? '' : ' (the call)') + ' · ' + (FILL_NAMES[this.fill] || this.fill) + (t && t.protective_text ? ', ' + t.protective_text : '');
      },
      planHead: function () { var c = this.collNum(); return 'Plan · ' + (c == null ? '$—' : usd(c)) + ' at ' + lev(this.levNow()); },
      // One percent of the notional: what a 1% BTC move does to this size, before fees.
      // What a BTC move of pct% your way (sign +1) or against you (-1) makes or loses at this size, after the entry fee
      // and an exit fee of the same estimate (the ticket's own fee_est; Kalshi's taker fee both ways).
      planNet: function (pct, sign) {
        var t = this.shown();
        return t && ok(t.notional) && ok(t.fee_est) && this.sideNow() ? sign * t.notional * pct / 100 - 2 * t.fee_est : null;
      },
      planMove: function (pct, sign) { var x = this.planNet(pct, sign); return x == null ? '—' : sUsd(x); },
      planWord: function (pct, sign) { var up = (this.sideNow() === 'short') !== (sign > 0); return 'if BTC ' + (up ? '+' : '−') + pct + '%'; },
      // The upside in one line: perps have no payout cap, so the gain is per move (and at the take profit, if one is set).
      makeLine: function () {
        var g1 = this.planNet(1, 1), g5 = this.planNet(5, 1), s = this.sideNow(), t = this.shown(), tp = this.kind() === 'entry' ? this.entryTp().v : null;
        if (g1 == null || !t) return '';
        var dir = s === 'short' ? 'falls' : 'rises', out = 'Could make ' + sUsd(g1) + ' if BTC ' + dir + ' 1%, ' + sUsd(g5) + ' if it ' + dir + ' 5% (after fees)';
        if (ok(tp) && ok(t.count)) out += '; at your take profit ' + sUsd(Math.abs(tp - this.mark()) * t.count - 2 * t.fee_est);
        return out + '. No cap: the gain grows with the move.';
      },
      planLiq: function () {
        var t = this.shown(), m = this.mark();
        if (!t || !ok(t.liquidation_est)) return 'Liquidation —';
        var d = ok(m) && m > 0 ? (t.liquidation_est - m) / m * 100 : null;
        return 'Liquidation ≈ BTC ' + this.btcPx(t.liquidation_est) + (d == null ? '' : ' (' + (d >= 0 ? '+' : '−') + Math.abs(d).toFixed(1) + '%)');
      },

      // ----- amount chips -----
      collNum: function () { var a = parseAmt(this.collInput, 'x'); return a.err ? null : a.v; },
      pickColl: function (x) { if (this.sliding || this.busy) return; this.collOther = false; this.collInput = String(x); this.saveColl(); this.edit(); },
      pickOther: function () { if (this.sliding || this.busy) return; this.collOther = true; },
      collOn: function (x) { return !this.collOther && this.collNum() === x; },
      otherOn: function () { var c = this.collNum(); return this.collOther || (c != null && CHIPS.indexOf(c) < 0); },
      worstLine: function () {
        var t = this.shown();
        if (t && ok(t.worst_case)) return 'Worst case ' + usd(t.worst_case) + ': your whole perps balance backs this position.';
        return this.atRisk();
      },

      // ----- in a position -----
      pchips: function () {
        var p = this.pos(), t = this.trig();
        if (!p) return [];
        return [
          { k: 'side', text: (p.direction === 'long' ? '▲ LONG ' : '▼ SHORT ') + cnt(Math.abs(p.size)) + ' ct', cls: p.direction === 'long' ? 'pl-up' : 'pl-down' },
          { k: 'entry', text: 'Entry BTC ' + this.btcPx(p.avg_entry), cls: '' },
          { k: 'lev', text: lev(p.leverage), cls: '' },
          { k: 'liq', text: 'Liq BTC ' + this.btcPx(p.liquidation_price), cls: 'pl-down' },
          { k: 'sl', text: 'Stop ' + (ok(t.stop_loss) ? 'BTC ' + this.btcPx(t.stop_loss) : '—'), cls: '', tap: true },
          { k: 'tp', text: 'Take profit ' + (ok(t.take_profit) ? 'BTC ' + this.btcPx(t.take_profit) : '—'), cls: '', tap: true }
        ];
      },
      pnlCls: function () { var p = this.pos(); return !p || !ok(p.unrealized_pnl) || p.unrealized_pnl === 0 ? '' : p.unrealized_pnl > 0 ? 'pl-up' : 'pl-down'; },
      btcMove: function () {
        var p = this.pos();
        if (!p || !ok(p.mark) || !ok(p.avg_entry) || p.avg_entry <= 0) return '—';
        var d = (p.mark - p.avg_entry) / p.avg_entry * 100;
        return (d >= 0 ? '▲ +' : '▼ −') + Math.abs(d).toFixed(2) + '% BTC';
      },
      pnlSub: function () {
        var p = this.pos();
        if (!p) return '';
        var on = ok(p.unrealized_pnl) && ok(p.collateral) && p.collateral > 0
          ? (p.unrealized_pnl >= 0 ? '+' : '−') + Math.abs(p.unrealized_pnl / p.collateral * 100).toFixed(1) + '% on ' + usd(p.collateral) : '—';
        return on + ' · funding ' + this.fundingSoFar();
      },
      // The distance bar: 0 = liquidated, the right end = 4x the amber band; red and amber zones from the settings.
      distScale: function () { var s = this.v && this.v.settings; return s && ok(s.amber) && s.amber > 0 ? s.amber * 4 : null; },
      distLeft: function () { var p = this.pos(), k = this.distScale(); return p && ok(p.distance) && k ? Math.max(0, Math.min(100, p.distance / k * 100)) + '%' : null; },
      distBg: function () {
        var s = this.v && this.v.settings, k = this.distScale();
        if (!s || !k || !ok(s.red)) return '';
        var r = Math.min(100, s.red / k * 100), a = Math.min(100, s.amber / k * 100);
        return 'background:linear-gradient(90deg,var(--down-strong) 0 ' + r + '%,var(--warn) ' + r + '% ' + a + '%,var(--up-strong) ' + a + '% 100%)';
      },
      distWord: function () { var p = this.pos(); return !p ? '' : p.band === 'red' ? 'red' : p.band === 'amber' ? 'amber' : 'safe'; },
      holdMove: function (sign) { var p = this.pos(); return p && ok(p.mark) && ok(p.size) ? sUsd(sign * Math.abs(p.size) * p.mark * 0.01) : '—'; },
      holdUpWord: function () { var p = this.pos(); return p && p.direction === 'short' ? 'if BTC −1%' : 'if BTC +1%'; },
      holdDnWord: function () { var p = this.pos(); return p && p.direction === 'short' ? 'if BTC +1%' : 'if BTC −1%'; },
      // Close now: the close preview ticket's own estimate (after fees), only while that preview is on screen.
      closeTicket: function () { var t = this.shown(); return this.kind() === 'close' && t ? t : null; },
      closeNow: function () { var t = this.closeTicket(); return t ? sUsd(t.est_realized_pnl) : '—'; },
      closeSub: function () {
        var t = this.closeTicket();
        return t ? 'after fee ≈ ' + usd4(t.fee_est) : this.act === 'close' ? 'waiting for the close preview' : 'pick Close below to see it';
      },
      // One display-only line, like Play's HOLD / CONSIDER EXIT: the warning band first, then the signals vs the side.
      advice: function () {
        var p = this.pos(), c = this.pcall();
        if (!p) return null;
        var away = ok(p.distance) ? pctf(p.distance) + ' from liquidation' : 'liquidation distance unknown';
        var tail = ' ' + away.charAt(0).toUpperCase() + away.slice(1) + '.';
        if (p.band === 'red' || p.band === 'amber') return { action: 'CONSIDER CLOSING', cls: 'pl-warn', text: 'The mark is ' + away + ' (' + p.band + ').' };
        if (c.lean && c.lean !== p.direction) return { action: 'CONSIDER CLOSING', cls: 'pl-warn', text: c.reason + ' That is against your ' + p.direction + '.' + tail };
        return { action: 'HOLD', cls: c.lean ? 'pl-up' : 'pl-flat', text: c.reason + tail };
      },

      // ----- the settings sheet (the gear) -----
      openSheet: function () { this.sheet = true; if (this.v && this.v.mode) this.modeForm.mode = this.v.mode; },
      // Limits and phone access live in Play's settings sheet (one set for every venue): go there and open it.
      openPlaySettings: function () {
        this.sheet = false;
        this.goVenue('kalshi');
        setTimeout(function () { window.dispatchEvent(new CustomEvent('pl-open-settings')); }, 0);
      },
      setTheme: function (m) {
        if (!window.BTCTheme) return;
        var saved = window.BTCTheme.set(m);
        this.themeMode = m; this.themeUnknown = window.BTCTheme.unknown;
        this.themeMsg = saved ? '' : 'Could not save the theme on this device (storage blocked): it applies until you close this page.';
      },

      // ----- take profit and stop after an entry (Play's auto-sell sheet): once per position this page opened -----
      // The sheet sits above the dock with no dim, so the close slide stays usable; it closes if the position does.
      // Both start off unless Kalshi already holds them (KD3: no TP / SL by default); a checkbox turns each on, a slider
      // sets its distance from the entry price. Submit prepares the trigger ticket and approves exactly that ticket.
      tpCheck: function () {
        var p = this.pos();
        if (this.tpSheet && (!p || p.id !== this.tpFor)) this.tpSheet = false;
        if (p && this.tpWant && !this.tpOffered[p.id]) {
          this.tpOffered[p.id] = true; this.tpWant = false;
          if (this.tpPrompt) this.tpOpen();
        }
      },
      setTpPrompt: function (on) { this.tpPrompt = !!on; save(TP_PROMPT_KEY, on ? null : 'off'); },
      tpClose: function () { if (this.tpNoMore) this.setTpPrompt(false); this.tpNoMore = false; this.tpSheet = false; },
      // A held level as its distance from the entry, in PCT_STEP steps (1% when none is held).
      pctOf: function (price) {
        var p = this.pos();
        if (!p || !ok(price) || !ok(p.avg_entry) || p.avg_entry <= 0) return 1;
        return Math.max(PCT_STEP, Math.round(Math.abs(price / p.avg_entry - 1) * 100 / PCT_STEP) * PCT_STEP);
      },
      tpOpen: function () {
        var p = this.pos(), t = this.trig();
        if (!p) return;
        this.tpFor = p.id; this.tpMsg = ''; this.tpSheet = true;
        this.tpOn = ok(t.take_profit); this.tpPct = this.pctOf(t.take_profit);
        this.slOn = ok(t.stop_loss); this.slPct = Math.min(this.slMax(), this.pctOf(t.stop_loss));
      },
      // Contract prices from the percentages (Kalshi's tick, 4 dp): a long's take profit is above the entry, its stop below.
      lvl: function (pct, sign) {
        var p = this.pos();
        if (!p || !ok(p.avg_entry)) return null;
        return Math.round(p.avg_entry * (1 + sign * sideSign(p.direction) * pct / 100) * 10000) / 10000;
      },
      tpPrice: function () { return this.lvl(this.tpPct, 1); },
      slPrice: function () { return this.lvl(this.slPct, -1); },
      // The stop slider ends one step before the liquidation price (a stop past it is refused by Kalshi).
      slMax: function () {
        var p = this.pos();
        if (!p || !ok(p.liquidation_price) || !ok(p.avg_entry) || p.avg_entry <= 0) return 20;
        return Math.max(PCT_STEP, Math.floor((Math.abs(p.liquidation_price / p.avg_entry - 1) * 100 - PCT_STEP) / PCT_STEP) * PCT_STEP);
      },
      moveUsd: function (price) { var p = this.pos(); return p && ok(price) && ok(p.size) ? Math.abs(p.size) * Math.abs(price - p.avg_entry) : null; },
      tpLabel: function () {
        var p = this.pos(), pr = this.tpPrice();
        if (!this.tpOn) return 'Off: nothing closes at a profit unless you slide to close.';
        if (!p || pr == null) return 'The entry price is not known yet.';
        return 'Closes all if BTC ' + (p.direction === 'long' ? 'rises' : 'falls') + ' to ' + this.btcPx(pr) + ' (' + this.tpPct.toFixed(2) + '% from entry) → ' + sUsd(this.moveUsd(pr)) + ' before fees.';
      },
      tpWhyNow: function () { var p = this.pos(); return this.tpOn && p ? this.tpWhy(this.tpPrice(), p.direction, p.mark) : ''; },
      slLabel: function () {
        var p = this.pos(), pr = this.slPrice();
        if (!this.slOn) return 'Off: nothing closes at a loss before liquidation unless you slide to close.';
        if (!p || pr == null) return 'The entry price is not known yet.';
        return 'Closes all if BTC ' + (p.direction === 'long' ? 'falls' : 'rises') + ' to ' + this.btcPx(pr) + ' (' + this.slPct.toFixed(2) + '% from entry) → ' + sUsd(-this.moveUsd(pr)) + ' before fees. Liquidation BTC ' + this.btcPx(p.liquidation_price) + '. Drawn in red on the chart.';
      },
      slWhyNow: function () { var p = this.pos(); return this.slOn && p ? this.stopWhy(this.slPrice(), p.direction, p.mark, p.liquidation_price) : ''; },
      tpSave: function () {
        var self = this, p = self.pos(), t = self.trig();
        if (!p || self.tpBusy || self.busy || p.id !== self.tpFor) return;
        var why = self.tpWhyNow() || self.slWhyNow() || self.gate('protect');
        if (why) { self.tpMsg = why; return; }
        var sl = self.slOn ? self.slPrice() : null, tp = self.tpOn ? self.tpPrice() : null;
        if (sl === (ok(t.stop_loss) ? t.stop_loss : null) && tp === (ok(t.take_profit) ? t.take_profit : null)) { self.tpClose(); return; }
        self.tpBusy = true; self.tpMsg = '';
        api.post('/api/perps/prepare_trigger', { stop_loss: sl, take_profit: tp }).then(function (res) {
          if (!res || !res.ok) { self.tpMsg = self.refusal(res); return null; }
          var shown = Object.assign({}, res.ticket, { expires_ms: res.expires_ms != null ? res.expires_ms : res.ticket.expires_ms });
          return api.post('/api/perps/approve', { token: res.token, shown: shown }).then(function (r) {
            if (r && r.view) self.merge(r.view, true);
            if (!r || !r.ok) { self.tpMsg = self.refusal(r); return; }
            self.tpClose(); self.trigFor = null;
            self.say(r.outcome && r.outcome.text ? r.outcome.text : 'Sent. Kalshi\'s answer shows on the position.', 'pl-done pl-info');
          });
        })['catch'](function (e) { self.tpMsg = 'Could not confirm: ' + errText(e) + ' Check the stop / take profit on the position.'; })
          ['finally'](function () { self.tpBusy = false; });
      },

      // ----- your record (Play's "Your record" card): the last closed positions, realized P&L from fills -----
      loadRecord: function () {
        var self = this;
        api.get('/api/perps/record?last=' + RECORD_LAST).then(function (r) { self.rec = r; self.recErr = ''; }, function (e) { self.recErr = errText(e); });
      },
      recToggle: function (open) { this.recOpen = open; if (open) this.loadRecord(); },
      recHead: function () { var r = this.rec; return r && r.rows ? r.wins + '–' + r.losses + ' · ' + sUsd(r.pnl) : ''; },
      recLine: function (r) {
        var why = { user_close: 'closed', stop: 'stop', take_profit: 'take profit', liquidated: 'liquidated', exchange_reduced: 'reduced by Kalshi' }[r.close_reason] || (r.close_reason || 'closed');
        return (r.direction === 'long' ? '▲ Long' : '▼ Short') + ' · ' + why + ' · ' + fmt.hm(r.closed_ms);
      },

      // ----- chart (web/perpchart.js) -----
      drawChart: function () {
        var el = this.$refs.pvchart, v = this.v, p = this.pos(), t = this.trig(), s = v && v.settings;
        if (!el || !v) return;
        if (!chart && !chartErr) {
          try { chart = new window.PerpChart(el); } catch (e) { chartErr = this.chartErr = 'Chart unavailable: ' + errText(e); return; }
        }
        if (!chart) return;
        var tk = this.shown(), k = this.kind();
        var entrySl = k === 'entry' ? this.entrySl().v : null, entryTp = k === 'entry' ? this.entryTp().v : null;
        chart.set({
          pts: hist.slice(), now_ms: this.now(),
          side: p ? p.direction : this.sideNow(),
          entry: p ? p.avg_entry : null,
          stop: p ? (this.tpSheet ? (this.slOn ? this.slPrice() : null) : ok(t.stop_loss) ? t.stop_loss : null) : entrySl,
          tp: p ? (this.tpSheet ? (this.tpOn ? this.tpPrice() : null) : ok(t.take_profit) ? t.take_profit : null) : entryTp,
          liq: p ? p.liquidation_price : (tk && k === 'entry' ? tk.liquidation_est : null),
          liq_label: p ? (p.liquidation_source === 'kalshi' ? 'Kalshi' : 'estimate') : 'estimate',
          amber: s ? s.amber : null, red: s ? s.red : null
        });
      },
      chartEmpty: function () { this.tick; return !hist.some(function (q) { return q[1] != null; }); },

      CHIPS: CHIPS, PCT_STEP: PCT_STEP, THEMES: THEMES, THEME_NAMES: THEME_NAMES,
      FILLS: FILLS, FILL_NAMES: FILL_NAMES, BANDS: BANDS, REDUCE: REDUCE, REDUCE_NAMES: REDUCE_NAMES, ACTS: ACTS, DOCK_ACTS: DOCK_ACTS, ACT_NAMES: ACT_NAMES,
      PROD_CONFIRM: PROD_CONFIRM, usd: usd, sUsd: sUsd, usd4: usd4, px: px, pctf: pctf, cnt: cnt, lev: lev, sideWord: sideWord
    };
  };
})();
