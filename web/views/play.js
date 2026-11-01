/* Play (#play): the phone screen where YOU make the call and trade it (one scrolling screen; wireframes 8-14).
   Venues: Kalshi (strategy "you") and Polymarket US (strategy "you_poly"), picked at the top and remembered. A venue
   with more than one market length (the payload's spans: Polymarket 15 min and 5 min, strategy "you_poly5") gets a
   "15 min | 5 min" tab under it, remembered per venue; every order uses the payload's own strategy and window.
   No silent defaults: a missing or unknown remembered venue shows "Pick a venue"; a venue with several spans and no
   remembered (or no longer offered) span shows "Pick 15 min or 5 min" — nothing loads and no slide shows until a tap.
   No position (board 13): the live "price to beat" chart card (BTC, the ▲/▼ gap chip, signal chips, the chart on
   web/playchart.js, Odds / Levels toggles), compact target tiles, the call, the amount, your last N attempts and a
   sticky slide-to-buy bar. In a trade (board 14): the parameters bar, the same chart with entry / stop / danger band
   and the held side's bid with the auto-sell line, one HOLD / CONSIDER EXIT sentence, and a pinned slide-to-sell.
   Data: GET /api/play/now?venue=&span= every 2 s (it carries the window's BTC series), /api/coach/series every 5 s while
   the Kalshi odds pane shows, /api/play/scoreboard every 10 s. Orders only through the existing /api/trading/prepare,
   /approve and /prepare_exit routes with the venue's strategy, after a deliberate slide; refusals are shown word for
   word.
   Play is the whole app (2026-10-29): it also carries the one kill switch (top right; state from GET /api/trading/status
   ?strategy=you, shared by every strategy, plus GET /api/coach/pause, every 5 s while the page is visible) and a
   settings sheet (the gear) with the PC-only phone access card (/api/access*, loaded only when the sheet opens).
   PC-only changes (R10): on a phone (GET /api/access answers 403) switching to production, turning the kill switch
   off, raising or removing a trade limit and widening the market protection are shown disabled with "Change this on
   the PC"; the server refuses them from a phone anyway. */
(function () {
  'use strict';
  var B = window.BTC, api = B.api, fmt = B.fmt;
  var PROD_CONFIRM = 'I understand this uses real money';
  var VENUE_KEY = 'play.venue', STAKE_KEY = 'play.stake', LAST_KEY = 'play.last', BOTH_KEY = 'play.both', TP_PROMPT_KEY = 'pl.autosellPrompt', SLIP_KEY = 'pl.slip', SLIPS = [1, 3, 5, 10, 25], SLIP_DEFAULT = 10;
  // Order type, as on the venues: "market" (Kalshi's Quick Order / Polymarket's market buy: fills at the best offers,
  // up to the ask + the market protection setting) or "limit" (a max price = the ask + your tolerance). Market is the stated default.
  var ORDER_KEY = 'pl.order', ORDERS = ['market', 'limit'], ORDER_NAMES = { market: 'Market', limit: 'Limit with max price' }, ORDER_DEFAULT = 'market';
  // Ask Settleview (application/copilot.py; wireframe v2 "Phone" 1-5 and 8): Off (the default: nothing is asked or
  // shown) / Quiet (warnings only) / Full (adds the setup lights, the rule lines, Fill in ticket and the hold note).
  // Kept on this device like Play's other settings. Each warning has its own switch (all on until one is turned off).
  var CP_KEY = 'pl.copilot', CP_MODES = ['off', 'quiet', 'full'], CP_NAMES = { off: 'Off', quiet: 'Quiet', full: 'Full' };
  var CPW_KEY = 'pl.copilot.warn', CP_WARNS = ['against_btc', 'late', 'band_50_75'];
  var CP_WARN_NAMES = { against_btc: 'Against BTC', late: 'Under 3 minutes left', band_50_75: '50–75¢ favourites' };
  var SPAN_KEY = 'play.span.';  // + venue: the market-length tab last picked on that venue
  var VENUES = ['kalshi', 'polymarket'];
  var VENUE_NAMES = { kalshi: 'Kalshi', polymarket: 'Polymarket' };
  // A trading mode in the money line's pill (and the Record tab's mode rows): the app's words for each.
  var MODE_WORDS = { prod: 'Live', sandbox: 'Paper', shadow: 'Shadow' };
  var MODE_ARIA = { prod: 'Live: real money', sandbox: 'Paper: Kalshi demo money', shadow: 'Shadow: no money' };
  var SPAN_FORMS = ['15m', '5m', '1h'];  // spans the server's /api/play routes accept; spans[venue] says which are live
  // A scoreboard trade's venue key (the backend's venue_key: "<venue>" for 15 min, "<venue>_<span>" otherwise).
  var TRADE_VENUES = { kalshi: 'Kalshi', polymarket: 'Polymarket 15 min', polymarket_5m: 'Polymarket 5 min' };
  // Discovery only: before a span is chosen, one /api/play/now call learns the server's spans map. Its window payload
  // is discarded (nothing is drawn, no slide); every venue accepts this span in the request.
  var PROBE_SPAN = '15m';
  var CHIPS = [1, 2, 5, 10];
  var W = 340, H = 140, PAD = 10;
  // Theme modes (index.html's head script owns window.BTCTheme; the chart colours come from the CSS tokens, read by
  // PlayChart itself and re-read on every theme change).
  var THEMES = ['system', 'light', 'dark'], THEME_NAMES = { system: 'System', light: 'Light', dark: 'Dark' };
  var ODDS_KEY = 'pl.odds', LEVELS_KEY = 'pl.levels', DETAILS_KEY = 'pl.details';
  // The verdict card's market trend: the 1-min change needs a quote at least 45 s old (aiming for 60 s); the in-trade chance sparkline
  // keeps <= 400 polled points and breaks its line across a missing stretch longer than 15 s.
  var TREND_MS = 60000, TREND_MIN_MS = 45000, TREND_CAP = 400, SPARK_GAP_MS = 15000, SPARK_W = 300, SPARK_H = 32;
  var SELL_SLIDE = 0.7, BUY_SLIDE = 0.9, SELL_SLIP = 0.02;  // a sell needs 70% of the track; its bid may slip 2c
  var RETRY_MS = 15000;  // a "price moved" retry card is good for 15 s
  // The entry tolerance: a stored valid value wins; nothing stored = the stated default (10c, shown as "default" in
  // the settings); a stored value that is not one of SLIPS is reported there as unrecognised (never silently swapped).
  function loadSlip() {
    var raw = load(SLIP_KEY), v = Number(raw);
    if (raw == null) return { slip: SLIP_DEFAULT, unknown: null };
    return SLIPS.indexOf(v) >= 0 && String(v) === String(raw).trim() ? { slip: v, unknown: null } : { slip: SLIP_DEFAULT, unknown: String(raw) };
  }
  // The order type: a stored valid value wins; nothing stored = Market (the displayed default); anything else is
  // reported in the settings as unrecognised and Market is used, shown as the default (never silently swapped).
  function loadOrder() {
    var raw = load(ORDER_KEY);
    if (raw == null) return { mode: ORDER_DEFAULT, unknown: null };
    return ORDERS.indexOf(raw) >= 0 ? { mode: raw, unknown: null } : { mode: ORDER_DEFAULT, unknown: String(raw) };
  }

  // Ask Settleview's mode: nothing stored = Off; a stored value that is not a mode is reported in the settings and
  // Off is used (never another level silently).
  function loadCp() {
    var raw = load(CP_KEY);
    if (raw == null) return { mode: 'off', unknown: null };
    return CP_MODES.indexOf(raw) >= 0 ? { mode: raw, unknown: null } : { mode: 'off', unknown: String(raw) };
  }
  // The warning switches: nothing stored = all on; stored = the comma list of those on ('' = none). An unknown key
  // in it is reported and the stored list is not used (all on until a switch is tapped).
  function loadCpWarn() {
    var raw = load(CPW_KEY);
    if (raw == null) return { on: CP_WARNS.slice(), unknown: null };
    var keys = String(raw).split(',').filter(function (k) { return k; });
    var bad = keys.filter(function (k) { return CP_WARNS.indexOf(k) < 0; });
    return bad.length ? { on: CP_WARNS.slice(), unknown: String(raw) } : { on: keys, unknown: null };
  }
  function load(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function save(key, v) { try { window.localStorage.setItem(key, String(v)); } catch (e) { /* storage blocked */ } }
  function usd(x) { return x == null || isNaN(x) ? '—' : (x < 0 ? '−$' : '$') + Math.abs(x).toFixed(2); }
  function sUsd(x) { return x == null || isNaN(x) ? '—' : (x > 0 ? '+' : x < 0 ? '−' : '') + '$' + Math.abs(x).toFixed(2); }
  function c(x) { return x == null ? '—' : Math.round(x * 100) + '¢'; }
  function pct(x) { return x == null || isNaN(x) ? '—' : Math.round(x * 100) + '%'; }
  function errText(e) { return (e && e.message) || String(e); }
  // The P&L the server wrote into an already_closed reason ("... (+$1,234.50).", trading.closed_sentence), or null
  // when the sentence carries none (e.g. settled): the banner is then neutral, never a guessed colour.
  function closedPnl(reason) {
    var m = /\(([+-])\$([\d,]+(?:\.\d+)?)\)\.?\s*$/.exec(reason || '');
    return m ? (m[1] === '-' ? -1 : 1) * Number(m[2].replace(/,/g, '')) : null;
  }
  function usd0(x) { return usd(x).replace(/\.00$/, ''); }
  // A 422's FastAPI detail list, field by field ("per_trade: Input should be greater than 0").
  function errDetail(e) {
    var d = e && e.body && e.body.detail;
    if (!Array.isArray(d) || !d.length) return errText(e);
    return d.map(function (x) { var loc = x && x.loc; return (Array.isArray(loc) ? loc[loc.length - 1] + ': ' : '') + ((x && x.msg) || JSON.stringify(x)); }).join('; ');
  }
  // Entry refusals about limits and cash: the server's reason, then what to do about it.
  var REFUSE_HINT = {
    limits_unreadable: 'Open Settings (⚙) → Trade limits and save your limits.',
    balance_unknown: 'Try again once the venue balance loads.',
    over_cash: 'Lower the amount or add cash at the venue.',
    over_trade_limit: 'Lower the amount, or change it in Settings (⚙) → Trade limits.',
    over_day_limit: 'Change it in Settings (⚙) → Trade limits, or wait for tomorrow.'
  };
  function dollars(x) { return '$' + Math.round(x).toLocaleString('en-US'); }
  // Colour is never the only cue: every UP / DOWN word gets its arrow.
  function arrows(t) { return t == null ? '' : String(t).replace(/\b(UP|DOWN)\b/g, function (w) { return (w === 'UP' ? '▲ ' : '▼ ') + w; }); }
  // The venues' own casing for a side ("▲ Up", "▼ Down").
  function sideName(s) { return s === 'UP' ? '▲ Up' : s === 'DOWN' ? '▼ Down' : ''; }
  // Selector keys are checked against explicit sets; a missing or unknown one is null, which the screen shows as an
  // explicit "pick one" state (never a silent default venue or market length).
  function loadVenue() { var v = load(VENUE_KEY); return VENUES.indexOf(v) >= 0 ? v : null; }
  // The remembered tab is only a candidate: the server's spans[venue] decides whether it is still offered.
  function loadSpan(venue) { var s = venue ? load(SPAN_KEY + venue) : null; return SPAN_FORMS.indexOf(s) >= 0 ? s : null; }
  // The open_elsewhere key of a tab (backend: "kalshi", "polymarket", "polymarket_5m"; 15 min carries no suffix).
  function tabKey(venue, span) { return span && span !== '15m' ? venue + '_' + span : venue; }
  function spanName(s) { return s ? String(s).replace(/m$/, '') + ' min' : ''; }
  function money0(x) { return x == null ? '—' : '$' + Number(x).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }

  // Fees and sizing exactly as the server sizes a ticket (TradingService.prepare): a preview; the ticket is final.
  function fee(n, p, rate, cent) {
    var raw = rate * n * p * (1 - p);
    return cent ? Math.round(raw * 100 + 1e-9) / 100 : Math.ceil(raw * 10000 - 1e-7) / 10000;
  }
  function contracts(stake, p, st) {
    if (!(stake > 0) || !(p > 0 && p < 1) || !st) return 0;
    var rate = st.fee_rate, cent = st.fee_rounding === 'cent';
    var n = Math.floor(stake / (p * (1 + rate * (1 - p))) * 100 + 1e-9) / 100;
    while (n > 0 && n * p + fee(n, p, rate, false) > stake + 1e-9) n = Math.round((n - 0.01) * 100) / 100;
    if (st.whole_contracts) {
      n = Math.floor(n + 1e-9);
      while (n > 0 && n * p + fee(n, p, rate, cent) > stake + 1e-9) n -= 1;
    }
    return Math.max(0, n);
  }

  window.playView = function () {
    var pc = null, oddsK = null, oddsP = null;  // the chart and the odds history: outside Alpine's reactivity
    var trend = null;  // { key, pts: [[ms, mid_up | null]] }: every /api/play/now poll this window (null = stale quote)
    var chance = null; // { id, pts: [[ms, chance_now | null]] }: the open position's outlook.chance_now from every poll
    var venue0 = loadVenue();
    return {
      venue: venue0, span: loadSpan(venue0),  // null = not chosen yet: the screen asks (nothing loads, no slide)
      spanGone: '',    // a remembered span the server no longer offers on this venue: shown, never silently swapped
      probing: false,
      spansMap: null,  // the server's tabs per venue, kept across switches so the tab row does not blink while loading
      v: null, sb: null, error: '', sbError: '', offset: 0, tick: 0,
      stakeInput: load(STAKE_KEY) || '5', other: false,
      last: load(LAST_KEY) || '10', both: load(BOTH_KEY) === '1',
      ticket: null, busy: false, msg: '', msgCls: '', pick: null,
      modeForm: { mode: '', confirm: '' }, modeMsg: '',
      alert: false, lastAction: null,
      synced: false, pulse: '', outcome: null, watchId: null, watchUntil: 0,
      tpSheet: false, tpFor: null, tpCents: 99, tpBusy: false, tpMsg: '', tpOffered: {}, mine: {}, slOn: false, slDist: 25,
      tpPrompt: load(TP_PROMPT_KEY) !== 'off', tpNoMore: false,
      settings: false,
      themeMode: window.BTCTheme ? window.BTCTheme.mode : 'system',
      themeUnknown: window.BTCTheme ? window.BTCTheme.unknown : null,
      themeMsg: window.BTCTheme ? '' : 'Theme control unavailable (the page head script did not run).',
      // Kill switch state: null = not known (the request failed or has not answered); never assumed off.
      kTrading: null, kCoach: null, kTradingErr: '', kCoachErr: '', killBusy: false, killMsg: '',
      // true on the PC itself, false on a phone (/api/access answers it 403), null = not known yet (nothing disabled).
      pc: null,

      init: function () {
        var self = this, on = function () { return Alpine.store('app').view === 'play'; };
        if (CHIPS.indexOf(Number(this.stakeInput)) < 0) this.other = true;
        window.onView(this, 'play', function () { self.load(); self.loadScore(); self.loadOdds(); self.heartbeat(); self.loadKill(); });
        // Record shows this component's record view: read the scoreboard (and the window, once) on entry
        window.onView(this, 'record', function () { self.loadScore(); if (self.venue && self.span && !self.v) self.load(); });
        self.loadPc();
        setInterval(function () { if (on()) self.load(); }, 2000);
        setInterval(function () { if (on() && self.oddsOn()) self.loadOdds(); }, 5000);  // the recorded Kalshi quotes' cadence
        setInterval(function () { if (on()) self.loadScore(); }, 10000);
        setInterval(function () { if (on() && document.visibilityState === 'visible') self.heartbeat(); }, 30000);
        setInterval(function () { if (on() && document.visibilityState === 'visible') self.loadKill(); }, 5000);
        setInterval(function () { self.tick++; self.retryExpire(); }, 1000);
        // A phone waking up re-syncs the server clock before anything trusts the countdown.
        document.addEventListener('visibilitychange', function () {
          if (document.visibilityState !== 'visible') { self.synced = false; self.slideReset(); return; }
          if (on()) { self.heartbeat(); self.load(); self.loadKill(); }
        });
      },
      loadPc: function () {
        var self = this;
        api.get('/api/access').then(function () { self.pc = true; }, function (e) { if (e && e.status === 403) self.pc = false; });
      },
      phone: function () { return this.pc === false; },
      heartbeat: function () { api.post('/api/trading/heartbeat', { visible: document.visibilityState === 'visible' })['catch'](function () {}); },
      load: function () {
        var self = this, venue = self.venue, span = self.span, t0 = Date.now();
        if (!venue) return;                          // "Pick a venue": nothing loads until one is tapped
        if (!span) { self.resolveSpan(); return; }   // "Pick 15 min or 5 min" (or learn the venue's spans first)
        // The amount goes along so the server's plan payoffs (entry.UP / DOWN if_wins / if_loses) are for this amount.
        var amt = self.amount();
        api.get('/api/play/now', { venue: venue, span: span, stake: amt > 0 ? amt : null }).then(function (r) {
          if (venue !== self.venue || span !== self.span || (r.span && r.span !== span)) return;  // a reply for another tab
          if (r.spans) {
            self.spansMap = r.spans;
            var list = r.spans[venue] || [];
            if (list.length && list.indexOf(span) < 0) { self.spanLost(span); return; }  // the remembered tab is gone
          }
          var t1 = Date.now(), was = self.v;
          if (was && was.window.end !== r.window.end) { self.chosen = null; self.filled = null; }  // a new window: no carried-over side or fill
          // Server clock offset, measured at the middle of the round trip.
          self.v = r; self.error = ''; self.offset = (r.server_now_ms != null ? r.server_now_ms : r.now) - (t0 + t1) / 2; self.synced = true;
          self.checkFrozen(); self.watchOutcome(was); self.tpCheck(); self.noteQuote(); self.noteTrend(); self.noteChance(); self.alertCheck();
          self.$nextTick(function () { self.drawPtb(); });
          if (r.mode && !self.modeForm.mode) self.modeForm.mode = r.mode.mode;
          self.loadCopilot();
        }, function (e) { self.error = errText(e); });
      },
      // No span chosen: learn the venue's spans (one discovery call), then a venue with a single span takes it (it is
      // the only choice); a venue with several waits for a tap on the tab row.
      resolveSpan: function () {
        var self = this, venue = self.venue;
        if (!self.spansMap) {
          if (self.probing) return;
          self.probing = true;
          api.get('/api/play/now', { venue: venue, span: PROBE_SPAN }).then(function (r) {
            if (r && r.spans) { self.spansMap = r.spans; self.error = ''; }
            else self.error = 'The server did not say which market lengths it offers.';
            if (venue === self.venue && !self.span && self.spansMap) self.resolveSpan();
          }, function (e) { self.error = errText(e); })['finally'](function () { self.probing = false; });
          return;
        }
        var list = self.spansMap[venue] || [];
        if (!list.length) { self.error = self.venueName() + ' has no markets on this server right now.'; return; }
        if (list.length === 1 && !self.spanGone) self.setSpan(list[0]);
      },
      // The remembered span is no longer offered: say so and let the user pick (the stored choice is kept until then).
      spanLost: function (s) {
        this.spanGone = s; this.span = null;
        this.slideReset(); this.v = null; this.sb = null; this.pick = null; this.chosen = null; this.synced = false; this.tpSheet = false;
      },
      // ----- the explicit "pick one" states (venue, then market length) -----
      needVenue: function () { return !this.venue; },
      needSpan: function () { return !!this.venue && !this.span && !!this.spansMap && this.spans().length > 0 && (this.spans().length > 1 || !!this.spanGone); },
      // The small status line: only while the venue's market lengths are being learned (the venue and length
      // choices themselves are the full-width choice cards).
      pickLine: function () {
        if (this.needVenue() || this.span || this.spansMap) return '';
        return 'Checking which markets ' + this.venueName() + ' offers…';
      },
      // The market-length choice card's heading (a remembered length that is no longer offered is named).
      spanAsk: function () {
        if (this.spanGone) return 'The ' + spanName(this.spanGone) + ' market isn\'t available right now — pick one.';
        return this.venueName() + ': pick a market length.';
      },
      // Nothing chosen yet (venue or length): the header countdown has no window to count, so it is hidden.
      choosing: function () { return this.needVenue() || !this.span; },
      loadScore: function () {
        var self = this, venue = self.venue, span = self.span;
        if (!venue || !span) return;
        api.get('/api/play/scoreboard', { venue: venue, span: span, last: self.last === 'all' ? null : self.last, both: self.both ? 'true' : null }).then(function (r) {
          if (venue === self.venue && span === self.span) { self.sb = r; self.sbError = ''; self.findOutcome(); }
        }, function (e) { self.sbError = errText(e); });
      },
      setVenue: function (x) {
        if (x === this.venue || this.ticket) return;
        if (VENUES.indexOf(x) < 0) { this.error = 'Unknown venue "' + x + '".'; return; }
        this.venue = x; save(VENUE_KEY, x); this.span = loadSpan(x); this.spanGone = ''; this.error = '';
        this.switched();
      },
      // The "15 min | 5 min" tab: a different market (own strategy, window, positions and record), so it resets
      // exactly like a venue switch.
      setSpan: function (s) {
        if (s === this.span || this.ticket || !this.venue) return;
        if (this.spans().indexOf(s) < 0) { this.error = 'Unknown market length "' + s + '" for ' + this.venueName() + '.'; return; }
        this.span = s; save(SPAN_KEY + this.venue, s); this.spanGone = ''; this.error = '';
        this.switched();
      },
      // After a venue or tab switch: drop everything that belonged to the old market (a slide in progress or a frozen
      // quote included), then reload.
      switched: function () {
        this.slideReset(); this.pulse = '';
        this.v = null; this.sb = null; this.pick = null; this.msg = ''; this.modeForm.mode = ''; this.chosen = null;
        this.filled = null; this.cp = null; this.cpSeq++; this.ltTip = ''; this.cpChecked = null;
        this.watchId = null; this.outcome = null; this.synced = false; this.tpSheet = false; this.readout = ''; this.sigTip = '';
        this.retry = null;
        this.load(); this.loadScore(); this.loadOdds();
      },
      // ----- tabs: the server's spans for a venue (one span = no tab row) and the open-position dots -----
      spans: function (x) { var m = this.spansMap; return (m && m[x || this.venue]) || []; },
      spanTabs: function () { return this.spans().length > 1; },
      spanName: spanName,
      // open_elsewhere counts live positions on the tabs you are NOT viewing (so the current tab never gets a dot).
      openOn: function (venue, span) { var o = this.v && this.v.open_elsewhere; return !!(o && o[tabKey(venue, span)] > 0); },
      venueOpen: function (x) {
        var o = (this.v && this.v.open_elsewhere) || {};
        return Object.keys(o).some(function (k) { return (k === x || k.indexOf(x + '_') === 0) && o[k] > 0; });
      },
      tabName: function () { return this.spanTabs() ? this.venueName() + ' ' + spanName(this.span) : this.venueName(); },

      // ----- the market picker (design pass 2026-10-31): the header names the market and opens the venue's list.
      // Kalshi: its spans, then BTC Perps (its own screen; "off" while the server has perps switched off);
      // Polymarket: its spans (15m, 5m). The spans are the server's (spans[venue]); nothing is offered that it lacks.
      pickerLabel: function () {
        if (this.needVenue()) return 'Pick a venue';
        return this.span ? 'BTC ' + this.span : 'Pick a market';
      },
      pickerSub: function () {
        if (!this.v || !this.span) return this.venue ? this.venueName() : '';
        var b = this.v.btc;
        return (this.pos() ? 'Your trade · ' : '') + this.windowText() + (b && b.strike != null ? ' · beat ' + dollars(b.strike) : '');
      },
      pickerItems: function () {
        var self = this, out = this.spans().map(function (s) {
          return { key: 'span:' + s, kind: 'span', span: s, label: 'BTC ' + s, on: self.span === s, dot: self.openOn(self.venue, s), note: '' };
        });
        if (this.venue === 'kalshi') {
          var off = Alpine.store('app').perpsOff;
          out.push({ key: 'perps', kind: 'perps', label: 'BTC Perps', on: false, dot: false, note: off === true ? 'off' : '' });
        }
        return out;
      },
      pickMarket: function (it) {
        if (it.kind === 'perps') { window.perpsOpen(); return; }
        if (it.kind === 'span') { this.setSpan(it.span); return; }
        throw new Error('play: unknown market picker item ' + it.kind);
      },
      // ----- the money line: the mode pill (Live = real money, Paper = demo money, Shadow = no money) and the cash -----
      pillWord: function () { var m = this.mode(); return m ? (MODE_WORDS[m.mode] || '⚠ ' + m.mode) : '—'; },
      pillCls: function () { var m = this.mode(); return m ? 'nv-pill-' + (MODE_WORDS[m.mode] ? m.mode : 'unknown') : ''; },
      pillAria: function () { var m = this.mode(); return m ? (MODE_ARIA[m.mode] || 'Unknown mode "' + m.mode + '"') : ''; },
      cashText: function () {
        var b = this.bal(), both = this.bothLine();
        if (!b || !b.fresh) return 'Cash unavailable' + (both ? ' · ' + both : '');
        return 'Cash ' + usd(b.cash) + (!b.positions ? '' : ' · ' + usd(b.positions) + ' in positions') + (both ? ' · ' + both : '');
      },
      // The call's headline in sentence words with its arrow ("ENTER UP" -> "▲ Enter Up", "WAIT" -> "Wait").
      heroText: function () {
        var h = String(this.call().headline || 'WAIT').toLowerCase().replace(/\b([a-z])/g, function (m) { return m.toUpperCase(); });
        return h.replace(/\bUp\b/g, '▲ Up').replace(/\bDown\b/g, '▼ Down').replace(/^(\w+) (▲|▼) /, '$2 $1 ');
      },
      setLast: function (n) { this.last = n; save(LAST_KEY, n); this.pick = null; this.loadScore(); },
      setBoth: function (b) { this.both = b; save(BOTH_KEY, b ? '1' : '0'); this.pick = null; this.loadScore(); },

      // ----- header -----
      ok: function () { return !!(this.v && this.v.available); },
      mode: function () { return this.v && this.v.mode; },
      real: function () { var m = this.mode(); return !!(m && m.real_money); },
      modeLine: function () {
        var m = this.mode();
        if (!this.v) return 'Loading…';
        if (!this.ok()) return this.v.reason;
        var word = m.real_money ? 'REAL MONEY' : m.mode === 'shadow' ? 'Shadow · no money' : 'Sandbox · demo money';
        return word;
      },
      lockLine: function () {
        var m = this.mode();
        if (!m) return '';
        if (m.killed) return '';  // the kill switch has its own line (pl-killed) and the KILL button
        if (!m.ready && m.reason) return m.reason;
        return '';
      },
      // ----- account balances (the app's existing balance readings; older than 5 min = unavailable) -----
      venueName: function () { return this.venue ? (VENUE_NAMES[this.venue] || this.venue) : ''; },
      bal: function () { var b = this.v && this.v.balances; return b ? b[this.venue] : null; },
      balLine: function () {
        var b = this.bal();
        if (!this.v) return '';
        if (!b || !b.fresh) return this.venueName() + ': balance unavailable';
        return this.venueName() + ': ' + usd(b.cash) + ' cash' + (b.positions == null ? '' : ' · ' + usd(b.positions) + ' in positions');
      },
      bothLine: function () { var b = this.v && this.v.balances; return b && b.both != null ? 'both venues ' + usd(b.both) : ''; },
      availHint: function () {
        var b = this.bal();
        if (!b || !b.fresh) return 'balance unavailable';
        return usd(this.amount() > 0 ? this.amount() : 0) + ' of ' + usd(b.cash) + ' available';
      },
      windowLine: function () { return this.v ? this.windowText() + (this.span ? ' · ' + spanName(this.span) : '') : ''; },
      // Share of the window gone (0-100), from the server's window start / end and the server-synced clock.
      winPct: function () {
        this.tick;
        var w = this.v && this.v.window;
        if (!w || w.start == null || w.end == null || !(w.end > w.start)) return null;
        return Math.round(Math.max(0, Math.min(1, (Date.now() + this.offset - w.start) / (w.end - w.start))) * 1000) / 10;
      },
      windowText: function () { return this.v ? fmt.hm(this.v.window.start) + '–' + fmt.hm(this.v.window.end) : ''; },
      // Time left = the server's window end against the server's clock (offset measured on every poll).
      leftMs: function () { this.tick; return this.v ? this.v.window.end - (Date.now() + this.offset) : null; },
      left: function () { return this.v ? fmt.countdown(this.leftMs()) : '—'; },
      // The server's entry cutoff; a payload without one means entry is closed (no slide), never an assumed 2:00.
      cutoff: function () { var w = this.v && this.v.window; return w && w.entry_cutoff_ms != null ? w.entry_cutoff_ms : null; },
      // Mirrors domain/strategy/play.clock_phase: red at or under the entry cutoff, amber under 2:00, else neutral.
      clockCls: function () {
        var ms = this.leftMs();
        if (ms == null) return '';
        return this.tooLate() ? 'pl-clock-late' : ms < 120000 ? 'pl-clock-warn' : '';
      },
      tooLate: function () { var ms = this.leftMs(), cut = this.cutoff(); return ms != null && (cut == null || ms <= cut); },
      // Settlement rule (domain/strategy/play.SETTLE_UP_OP): both venues settle on the CF BRTI 60 s average, ties UP.
      settleLine: function () {
        var b = this.v && this.v.btc, op = (this.v && this.v.window.settle_op) || '≥';
        if (!this.v) return '';
        return 'Settles ▲ UP if the 60-second average BRTI at ' + this.endHm() + ' is ' + op + ' ' +
          (b && b.strike != null ? money0(b.strike) : 'the strike (not known yet)');
      },

      // ----- verdict -----
      call: function () { return (this.v && this.v.call) || {}; },
      verdictCls: function () { var a = this.call().action; return a === 'ENTER UP' ? 'pl-up' : a === 'ENTER DOWN' ? 'pl-down' : 'pl-flat'; },
      agreeText: function () {
        var g = this.call().agree;
        return g && g.n != null ? g.n + ' of ' + g.of + ' signals agree' : '';
      },
      agreeShort: function () { var g = this.call().agree; return g && g.n != null && g.of != null ? g.n + ' of ' + g.of + ' agree' : ''; },
      openDetails: function () {
        this.setDetails(!this.details);
        if (!this.details) return;
        this.$nextTick(function () { var el = document.getElementById('pl-why'); if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); });
      },
      downPct: function () { var m = this.call().mid_up; return m == null ? '—' : pct(1 - m); },
      // The model label under the bar sits at the model tick, kept clear of the UP / DOWN labels at the ends.
      fairLabelLeft: function () { var f = this.call().fair_up; return f == null ? null : Math.max(30, Math.min(70, f * 100)) + '%'; },
      // No live market odds: the bar shows empty (grey), never an assumed 50/50.
      upWidth: function () { var m = this.call().mid_up; return (m == null ? 0 : Math.round(m * 100)) + '%'; },
      modelLine: function () {
        var g = this.agreeText();
        return 'Model ' + pct(this.call().fair_up) + ' UP' + (g ? ' · ' + g : '');
      },
      fairLeft: function () { var f = this.call().fair_up; return f == null ? null : Math.max(0, Math.min(100, f * 100)) + '%'; },
      details: load(DETAILS_KEY) === '1',
      setDetails: function (open) { this.details = !!open; save(DETAILS_KEY, open ? '1' : '0'); },

      // ----- Ask Settleview (GET /api/copilot/now with each /api/play/now poll, only while it is not Off). Every
      // sentence is the server's (code templates, no model). It never sends anything: Fill in ticket only sets the
      // side, max price and the rule's BTC stop on this screen's ticket; the slide still sends through prepare ->
      // approve. What was showing when an order went is recorded with it (POST /api/copilot/shown). -----
      cpMode: loadCp().mode, cpUnknown: loadCp().unknown, cpWarn: loadCpWarn().on, cpWarnUnknown: loadCpWarn().unknown,
      CP_MODES: CP_MODES, CP_NAMES: CP_NAMES, CP_WARNS: CP_WARNS, CP_WARN_NAMES: CP_WARN_NAMES,
      cp: null, cpErr: '', cpSeq: 0, filled: null, ltTip: '',
      setCpMode: function (m) {
        if (CP_MODES.indexOf(m) < 0) { this.error = 'Unknown Ask Settleview mode "' + m + '".'; return; }
        this.cpMode = m; this.cpUnknown = null; save(CP_KEY, m);
        this.cp = null; this.cpErr = ''; this.ltTip = '';
        if (m !== 'full' && !this.busy && !this.sliding) this.undoFill();
        this.loadCopilot();
      },
      cpModeNote: function () {
        return this.cpMode === 'off' ? 'Off: nothing is checked or shown. Quiet shows warnings only; Full adds rule matches, tickets and hold advice.'
          : this.cpMode === 'quiet' ? 'Quiet: warnings only. Full adds rule matches, tickets and hold advice.'
          : 'Full: rule matches, Fill in ticket and hold advice, as well as the warnings.';
      },
      cpWarnOn: function (k) { return this.cpWarn.indexOf(k) >= 0; },
      toggleCpWarn: function (k) {
        if (CP_WARNS.indexOf(k) < 0) { this.error = 'Unknown warning "' + k + '".'; return; }
        var was = this.cpWarn, on = this.cpWarnOn(k);
        this.cpWarn = CP_WARNS.filter(function (x) { return x === k ? !on : was.indexOf(x) >= 0; });
        this.cpWarnUnknown = null; save(CPW_KEY, this.cpWarn.join(','));
        this.loadCopilot();
      },
      cpWarnCount: function () { return this.cpWarn.length + ' on'; },
      loadCopilot: function () {
        var self = this, venue = self.venue, span = self.span, mode = self.cpMode, seq = ++self.cpSeq;
        if (mode === 'off' || !venue || !span) { self.cp = null; self.cpErr = ''; return; }
        var amt = self.amount();
        api.get('/api/copilot/now', { venue: venue, span: span, mode: mode, warn: self.cpWarn.length ? self.cpWarn.join(',') : 'none',
          stake: amt > 0 ? amt : null }).then(function (r) {
          if (seq !== self.cpSeq) return;  // a newer request (another tab, mode or switch) is on its way
          self.cp = r; self.cpErr = '';
        }, function (e) { if (seq === self.cpSeq) { self.cp = null; self.cpErr = 'Ask Settleview: ' + errText(e); } });
      },
      // The answer belongs to the market and window on screen, and Ask Settleview is on.
      cpFresh: function () {
        var cp = this.cp, v = this.v;
        return !!(this.cpMode !== 'off' && cp && v && cp.available && cp.mode === this.cpMode && cp.window_end === v.window.end);
      },
      cpFull: function () { return this.cpFresh() && this.cpMode === 'full'; },
      // The one line under the call: the server's pick for the side you tapped (or none picked).
      // The agents on this market (the fleet's live rows, views/agent.js): one quiet line, so you never trade
      // against them unknowingly ("2 agents here · one holds Up $1.00 (CUSHION100)"); tap: the Agents tab, this market.
      agentsHere: function () {
        var st = window.Alpine && Alpine.store('agent');
        if (!st || !st.enabled || !this.venue || !this.span) return null;
        return st.here(this.venue + '_' + this.span);
      },
      cpLine: function () { if (!this.cpFresh() || this.pos()) return null; var l = this.cp.line || {}; return l[this.chosen || 'none'] || null; },
      cpLights: function () { return this.cpFull() && !this.pos() ? (this.cp.lights || []) : []; },
      ltCls: function (x) { return x.state === 'green' ? 'sv-go' : x.state === 'amber' ? 'sv-form' : ''; },
      ltName: function (x) { return x.rule + ': ' + (x.state === 'green' ? 'ready' : x.state === 'amber' ? 'setting up' : 'not in play'); },
      ltTap: function (x) { this.ltTip = this.ltTip === x.rule ? '' : x.rule; },
      ltTipText: function () {
        var t = this.ltTip, x = this.cpLights().filter(function (l) { return l.rule === t; })[0];
        if (!x) return '';
        return (x.line || x.rule + ' is not in play in this window.') + ' ' + x.record_text;
      },
      // Fill in ticket: the green rule's side, its max price and its BTC stop on this screen's ticket (with Undo).
      cpFill: function () { return this.cpFull() && !this.pos() && !this.tooLate() ? this.cp.fill : null; },
      canFill: function () { var f = this.cpFill(); return !!(f && !this.filledFor(f.side) && !this.busy && !this.sliding && !this.ticket); },
      fillIn: function () {
        var f = this.cpFill();
        if (!f || this.busy || this.sliding || this.ticket) return;
        this.filled = { rule: f.rule, side: f.side, max_price: f.max_price, stop_index_below: f.stop_index_below,
          stop_index_above: f.stop_index_above, stop: f.stop, windowEnd: this.v.window.end, prev: this.chosen };
        this.chosen = f.side; this.retry = null;
      },
      undoFill: function () {
        if (!this.filled || this.busy || this.sliding) return;
        this.chosen = this.filled.prev; this.filled = null;
      },
      filledFor: function (s) { var f = this.filled, v = this.v; return !!(f && s && f.side === s && v && v.window.end === f.windowEnd); },
      // A filled-in ticket is a limit order at the rule's max price; otherwise your order type setting.
      effMode: function (s) { return this.filledFor(s) ? 'limit' : this.orderMode; },
      filledLine: function () {
        var f = this.filled;
        if (!f || !this.filledFor(f.side) || this.pos()) return '';
        var still = this.cpFull() && this.cp.fill && this.cp.fill.rule === f.rule;
        return f.rule + ' · ' + sideName(f.side) + ' · Max ' + c(f.max_price) + ' · ' + (f.stop != null ? 'BTC stop ' + dollars(f.stop) : 'no stop') +
          (still ? '' : ' · no longer ready');
      },
      // The check while you slide: at most one amber line, only when the server has a warning for this side. It
      // appears once the knob moves and stays (so it can be tapped) until the side, tab or window changes or the
      // order goes.
      cpChecked: null,
      cpSlideWarn: function () {
        if (this.pos() || !this.cpFresh()) return null;
        var s = this.sideNow();
        if (!s || this.cpChecked !== s + ':' + this.v.window.end) return null;
        return (this.cp.warnings && this.cp.warnings[s]) || null;
      },
      cpHold: function () { return this.cpFull() && this.pos() ? this.cp.hold : null; },
      // What was showing for this side as the slide sent it (null while Ask Settleview is off or not loaded).
      cpSnapshot: function (s) {
        if (!this.cpFresh()) return null;
        var cp = this.cp, line = this.cpLine(), w = cp.warnings && cp.warnings[s], f = this.filledFor(s) ? this.filled : null, b = cp.btc || {};
        var ms = this.leftMs();
        return { mode: this.cpMode, side: s, ask: this.side(s).ask == null ? null : this.side(s).ask,
          lights: (cp.lights || []).map(function (x) { return { rule: x.rule, state: x.state }; }),
          line: line ? { kind: line.kind, text: line.text, rule: line.rule || null } : null,
          warning: w ? { keys: w.keys, text: w.text } : null,
          filled: f ? { rule: f.rule, side: f.side, max_price: f.max_price, stop: f.stop } : null,
          btc: b.btc == null ? null : b.btc, strike: b.strike == null ? null : b.strike, btc_gap: b.gap == null ? null : b.gap,
          left_ms: ms == null ? null : Math.round(ms) };
      },
      cpRecord: function (t, id) {
        var self = this, shown = t.meta && t.meta.shown;
        if (!shown || !id) return;
        api.post('/api/copilot/shown', { strategy: t.strategy, bracket_id: id, shown: shown }).then(function (r) {
          if (r && r.ok === false) self.cpErr = 'Ask Settleview could not record what it showed: ' + (r.reason || r.code);
        }, function (e) { self.cpErr = 'Ask Settleview could not record what it showed: ' + errText(e); });
      },

      // ----- the market trend on the verdict card: UP% now, its change over ~1 min (the window's quotes feed the 1-min change; the call card has no sparkline since 2026-10-29).
      // Data: the market's mid_up from each 2 s /api/play/now poll (market.fresh false = a gap), plus, on Kalshi while
      // the odds pane has loaded them, the recorded Kalshi quotes from earlier in this window. Real quotes only. -----
      noteTrend: function () {
        var v = this.v, m = v && v.market, key = this.oddsKey();
        if (!v || v.now == null) return;
        if (!trend || trend.key !== key) trend = { key: key, pts: [] };
        var pts = trend.pts;
        if (pts.length && v.now <= pts[pts.length - 1][0]) return;
        pts.push([v.now, m && m.fresh && m.mid_up != null ? m.mid_up : null]);
        // Over the cap: thin every other quote (gap markers stay), then drop the oldest if still over.
        if (pts.length > TREND_CAP) pts = pts.filter(function (q, i) { return q[1] == null || i % 2 === 0 || i === pts.length - 1; });
        if (pts.length > TREND_CAP) pts = pts.slice(pts.length - TREND_CAP);
        trend.pts = pts;
      },
      trendPts: function () {
        var v = this.v, out = [];
        if (!v) return out;
        var own = trend && trend.key === this.oddsKey() ? trend.pts : [];
        var first = own.length ? own[0][0] : Infinity;
        if (this.venue === 'kalshi' && oddsK && oddsK.boundary === v.window.start) {
          oddsK.pts.forEach(function (q) {
            var x = q[1];
            if (q[0] < first && q[0] >= v.window.start && x.yes_bid != null && x.yes_ask != null) out.push([q[0], (x.yes_bid + x.yes_ask) / 2]);
          });
        }
        return out.concat(own);
      },
      trendNow: function () { return this.call().mid_up; },
      // Change in whole points against the quote nearest 1 min ago (it must be at least 45 s old); null until one exists.
      trendDelta: function () {
        var v = this.v, now = this.trendNow(), best = null;
        if (!v || now == null) return null;
        this.trendPts().forEach(function (q) {
          var age = v.now - q[0];
          if (q[1] == null || age < TREND_MIN_MS) return;
          if (!best || Math.abs(age - TREND_MS) < Math.abs(v.now - best[0] - TREND_MS)) best = q;
        });
        return best ? Math.round(now * 100) - Math.round(best[1] * 100) : null;
      },
      trendText: function () {
        var d = this.trendDelta();
        if (d == null) return '— in 1 min';
        return (d > 0 ? '▲ +' + d : d < 0 ? '▼ −' + (-d) : '■ 0') + ' in 1 min';
      },
      trendCls: function () { var d = this.trendDelta(); return d == null || d === 0 ? 'pl-trend-flat' : d > 0 ? 'pl-up' : 'pl-down'; },
      trendAria: function () {
        var d = this.trendDelta();
        return 'Market UP ' + pct(this.trendNow()) + ', ' + (d == null ? 'change over 1 minute not known yet'
          : (d === 0 ? 'unchanged' : (d > 0 ? 'up ' : 'down ') + Math.abs(d) + ' points') + ' in the last minute');
      },
      btcLine: function () {
        var b = this.v && this.v.btc;
        return b && b.gap != null ? 'BTC ' + money0(b.btc) + ' · ' + b.sentence : 'BTC or the strike is not known yet.';
      },
      focus: function () {
        var cl = this.call();
        if (cl.lean === 'UP' || cl.lean === 'DOWN') return cl.lean;
        return cl.mid_up != null && cl.mid_up < 0.5 ? 'DOWN' : 'UP';
      },
      side: function (s) { return (this.v && this.v.entry && this.v.entry[s || this.focus()]) || {}; },
      endHm: function () { return this.v ? fmt.hm(this.v.window.end) : '—'; },
      // The Plan tile: the server's settlement payoff for the focused side at this amount (entry[side].if_wins /
      // if_loses). Shown only when the payload was sized for the amount on screen (stake.value); otherwise "—" until
      // the next poll brings the new amount's numbers.
      // Market orders: the server's market terms instead (contracts at the ask, cost at the ask).
      planFor: function (s) {
        var e = this.side(s);
        if (!this.sized()) return { wins: null, loses: null };
        if (this.effMode(s) === 'market') {
          var m = e.market;
          if (!m || !(m.contracts > 0) || m.cost_at_ask == null) return { wins: null, loses: null };
          return { wins: m.contracts - m.cost_at_ask, loses: -m.cost_at_ask };
        }
        return { wins: e.if_wins == null ? null : e.if_wins, loses: e.if_loses == null ? null : e.if_loses };
      },
      // The payload was sized for the amount on screen (stake.value): its per-side numbers belong to this amount.
      sized: function () {
        var st = this.v && this.v.stake, a = this.amount();
        return !!(st && st.value != null && a > 0 && Math.abs(st.value - a) <= 0.005);
      },
      planWins: function () { return sUsd(this.planFor(this.focus()).wins); },
      planLoses: function () { var l = this.planFor(this.focus()).loses; return l == null ? '—' : '−$' + Math.abs(l).toFixed(2); },

      // ----- amount -----
      // stake.cap: the lower of your per-trade limit (stake.max, null = none) and, in production, your fresh venue cash
      // (stake.cash_cap when stake.cash_applies); null = neither applies. stake.sentence says it in words.
      cap: function () { var st = this.v && this.v.stake; return st && st.cap != null ? st.cap : null; },
      stake: function () { var n = Number(this.stakeInput); return isFinite(n) ? Math.round(n * 100) / 100 : NaN; },
      amount: function () { var n = this.stake(), mx = this.cap(); return mx != null && n > mx ? mx : n; },
      // The server's own line for what bounds a trade ("No per-trade limit · up to your $90.09 cash").
      stakeSentence: function () {
        var st = this.v && this.v.stake;
        if (!st) return '';
        return st.sentence || 'The server sent no limit summary for this market.';
      },
      // One line, only when the typed amount is over stake.cap: whichever bound is the lower one.
      stakeNote: function () {
        var n = this.stake(), mx = this.cap(), st = this.v && this.v.stake;
        if (!(n > 0)) return 'Enter an amount above $0.';
        if (mx == null || !(n > mx + 1e-9)) return '';
        var per = st.max, cash = st.cash_applies ? st.cash_cap : null, use = ' — the slide uses ' + usd0(mx);
        if (per != null && n > per + 1e-9 && (cash == null || per <= cash + 1e-9)) return 'Over your ' + usd0(per) + ' per-trade limit' + use;
        if (cash != null && n > cash + 1e-9) return 'More than your ' + usd0(cash) + ' cash' + use;
        return 'Over the most this order can be (' + usd0(mx) + ')' + use;
      },
      chipOff: function (x) { var mx = this.cap(); return mx != null && x > mx + 1e-9; },
      pickChip: function (x) { if (this.chipOff(x)) return; this.other = false; this.stakeInput = String(x); save(STAKE_KEY, x); },
      pickOther: function () { this.other = true; var self = this; setTimeout(function () { var el = document.getElementById('pl-amount'); if (el) el.focus(); }, 0); },
      saveStake: function () { if (this.stake() > 0) save(STAKE_KEY, this.stake()); },
      // The order about to be sent, per side. Market: the server's market terms (entry[side].market: contracts sized at
      // the ask, cost at the ask, worst case at 99c; null = no fresh ask), only when sized for this amount. Limit:
      // contracts sized at the max price (as the server sizes a limit ticket); the average price shown is the ask
      // (it fills at the best offers, at most the max price), fees at that average.
      preview: function (s) {
        var st = this.v && this.v.stake, e = this.side(s);
        if (!st) return null;
        if (this.effMode(s) === 'market') {
          var m = e.market;
          if (!m || !this.sized() || !(m.contracts > 0) || m.sized_at == null || m.cost_at_ask == null) return null;
          return { n: m.contracts, avg: m.sized_at, fee: Math.max(0, m.cost_at_ask - m.contracts * m.sized_at), cost: m.cost_at_ask,
            payout: m.contracts, worst: m.worst_case_cost, maxPrice: m.max_price, fits: m.fits === true, why: m.why };
        }
        var cap = this.capFor(s), n = cap == null ? 0 : contracts(this.amount(), cap, st);
        if (!n) return null;
        var avg = e.ask != null && e.ask <= cap ? e.ask : cap, f = fee(n, avg, st.fee_rate, st.fee_rounding === 'cent');
        return { n: n, avg: avg, fee: f, cost: n * cap + fee(n, cap, st.fee_rate, st.fee_rounding === 'cent'), payout: n,
          worst: null, maxPrice: cap, fits: true };
      },
      previewText: function () {
        var p = this.preview();
        if (!p) return 'No contracts at this amount and price.';
        return (Math.round(p.n * 100) / 100) + ' contracts · cost + fee ' + usd(p.cost) + ' · pays ' + usd(p.payout) + ' if ' + arrows(this.focus()) + ' wins';
      },
      // The dock's count beside the amount chips: about how many contracts this amount buys on the picked side.
      countLine: function () {
        var s = this.sideNow(), p = s && this.preview(s);
        return p && !this.tooLate() ? '≈' + (Math.round(p.n * 100) / 100) + ' ct' : '';
      },
      // The dock's one secondary line: the cap note when the amount is over one; else the market length, the order
      // type and the terms (average price, fees, payout, and a Market buy's worst case).
      infoWarn: function () { return !!this.stakeNote() && !this.pos(); },
      infoLine: function () {
        if (this.pos() || !this.v) return '';
        if (this.stakeNote()) return this.stakeNote();
        var s = this.sideNow(), p = s && this.preview(s);
        if (!s || !p || this.tooLate()) return this.span ? spanName(this.span) + ' · ' + this.stakeSentence() : '';
        var bits = ['≈' + (Math.round(p.n * 100) / 100) + ' contracts', 'avg ~' + c(p.avg), 'fees ' + usd(p.fee), 'to win ' + usd(p.payout)];
        var w = this.slideWorst();
        return bits.join(' · ') + (w ? ' · ' + w.charAt(0).toLowerCase() + w.slice(1) : '');
      },
      // The line right above the slider, in the venues' words: contracts, average price, fees, "To win" (the payout).
      slideInfo: function () {
        var s = this.sideNow(), p = s && !this.pos() && this.preview(s);
        if (!p || this.tooLate()) return '';
        return '≈' + (Math.round(p.n * 100) / 100) + ' contracts · Avg price ~' + c(p.avg) + ' · Fees ' + usd(p.fee) + ' · To win ' + usd(p.payout);
      },
      // What the order could make, beside what it could lose: the payout less the cost at the average price and fees.
      makeLine: function () {
        var s = this.sideNow(), p = s && !this.pos() && this.preview(s);
        if (!p || this.tooLate()) return '';
        var spent = p.n * p.avg + p.fee;
        return 'Could make ' + sUsd(p.payout - spent) + ' if ' + sideName(s) + ' wins (after fees) · lose ' + usd(spent) + ' if it does not';
      },
      // Market orders only: the worst case (every contract at its limit, the ask + market protection, + fee), from
      // entry[side].market.
      slideWorst: function () {
        var s = this.sideNow(), p = s && !this.pos() && this.effMode(s) === 'market' && this.preview(s);
        if (!p || this.tooLate() || p.worst == null) return '';
        return 'At most ' + usd(p.worst) + ' if the price runs (fills up to ' + c(p.maxPrice) + ')';
      },
      // A market buy whose worst case does not fit (entry[side].market.fits false): the slide is off, and the server's
      // market.why says why ("At worst this costs $52.80, over your $50.00 per-trade limit.").
      worstBlock: function (s) {
        var p = this.effMode(s) === 'market' && this.preview(s);
        if (!p || p.fits) return '';
        return p.why ? String(p.why).replace(/\.$/, '') : 'The server says this market buy does not fit, but gave no reason';
      },
      // The slider's two lines: the reason, then what to do.
      worstHead: function (s) { return this.worstBlock(s); },
      worstTail: function (s) { return this.worstBlock(s) ? 'Lower the amount, or narrow Market protection in Settings' : ''; },

      // ----- orders: one deliberate slide = Yes. The server prepares the ticket (price cap, size, stop) and the same
      // gesture approves it at once; the slider states side, amount and price cap before you slide. -----
      // WAIT / coin flip preselects no side: the slider stays dim until the user taps UP or DOWN.
      chosen: null, slideX: 0, sliding: false, sx0: 0, frozen: null,
      // Entry price tolerance: the order may fill up to `slip` cents above the ask you saw (it fills at the best
      // price available, often the ask itself). A tick inside it never blocks the entry (2026-10-29: a 1c tick
      // refused an entry and the 1c snap-back made the user slide again and again).
      slip: loadSlip().slip, slipUnknown: loadSlip().unknown, SLIP_DEFAULT: SLIP_DEFAULT, SLIPS: SLIPS,
      setSlip: function (v) { if (SLIPS.indexOf(v) < 0) return; this.slip = v; this.slipUnknown = null; save(SLIP_KEY, v); },
      // Order type (settings): Market by default, like the venues; Limit uses the max price = ask + tolerance.
      orderMode: loadOrder().mode, orderUnknown: loadOrder().unknown, ORDERS: ORDERS, ORDER_NAMES: ORDER_NAMES, ORDER_DEFAULT: ORDER_DEFAULT,
      setOrder: function (m) {
        if (ORDERS.indexOf(m) < 0) { this.error = 'Unknown order type "' + m + '".'; return; }
        if (this.sliding || this.busy) return;
        this.orderMode = m; this.orderUnknown = null; save(ORDER_KEY, m); this.retry = null;
      },
      capFor: function (s) { if (this.filledFor(s)) return this.filled.max_price; var a = this.side(s).ask; return a == null ? null : Math.round(Math.min(0.98, a + this.slip / 100) * 100) / 100; },
      sideNow: function () { var a = this.call().action; return this.chosen || (a === 'ENTER DOWN' ? 'DOWN' : a === 'ENTER UP' ? 'UP' : null); },
      choose: function (s) {
        if (this.busy || this.sliding) return;
        if (this.filled && this.filled.side !== s) this.filled = null;  // the other side: the filled-in ticket no longer applies
        if (s !== this.sideNow()) this.cpChecked = null;
        this.chosen = s;
      },
      slideOk: function () { return this.synced && (this.pos() ? this.canExit() : !!this.sideNow() && this.canBuy(this.sideNow())); },
      slideReset: function () { this.sliding = false; this.slideX = 0; this.frozen = null; },
      // The quote is frozen for the drag: a poll that moves the side's price cap snaps the slider
      // back, so nothing is ever sent at a cap the user did not see (the server's IOC cap stays the backstop).
      checkFrozen: function () {
        var f = this.frozen;
        if (!f) return true;
        if (f.exit) {
          var p = this.pos(), bid = p && p.advice ? p.advice.bid : null;
          if (!this.positions().some(function (x) { return x.id === f.id; })) {
            // The position closed under the slide (auto-sell, stop or a sale elsewhere): nothing to sell. The result
            // comes from the server's scoreboard (watchOutcome -> the outcome card), never from a guess here.
            this.slideReset();
            this.flash('Position closed — nothing to sell');
            this.say('Your position closed before the sell (auto-sell, stop or a sale elsewhere); nothing was sent. Its result shows when the server records it.', 'pl-done pl-info');
            return false;
          }
          if (p && p.id === f.id && bid != null && bid >= f.bid - SELL_SLIP - 1e-9) return true;
          this.slideReset();
          this.flash(bid == null ? 'Bid gone — slide again' : 'Bid moved to ' + c(bid) + ' — slide again');
          return false;
        }
        var now = this.side(f.side).ask;
        if (f.market) {  // a market buy: a moving ask is fine; no ask, or a worst case that no longer fits, is not
          var pm = this.preview(f.side);
          if (now != null && pm && pm.fits) return true;
          this.slideReset();
          this.flash(now == null || !pm ? 'Price gone — slide again' : this.worstHead(f.side));
          return false;
        }
        if (now != null && f.cap != null && now <= f.cap + 1e-9) return true;
        this.slideReset();
        this.flash(now == null ? 'Price gone — slide again' : 'Price jumped to ' + c(now) + ', past your max price ' + c(f.cap) + ' — slide again');
        return false;
      },
      flash: function (t) {
        var self = this;
        self.pulse = t; clearTimeout(self.pulseTimer);
        self.pulseTimer = setTimeout(function () { self.pulse = ''; }, 2500);
      },
      slideMax: function () { var t = this.$refs.track; return t ? Math.max(60, t.clientWidth - 64) : 200; },
      slideStart: function (e) {
        if (!this.slideOk()) return;
        this.sliding = true; this.sx0 = e.clientX - this.slideX; this.pulse = '';
        var ps = this.pos();
        if (!ps) {
          var s = this.sideNow(), q = this.side(s);
          this.frozen = this.effMode(s) === 'market' ? { side: s, market: true, ask: q.ask } : { side: s, cap: this.capFor(s), ask: q.ask };
        }
        else this.frozen = { exit: true, id: ps.id, bid: ps.advice.bid };
        try { e.target.setPointerCapture(e.pointerId); } catch (err) { /* older browsers */ }
      },
      slideMove: function (e) {
        if (!this.sliding) return;
        this.slideX = Math.max(0, Math.min(this.slideMax(), e.clientX - this.sx0));
        if (this.slideX > 0 && !this.pos() && this.sideNow() && this.v) this.cpChecked = this.sideNow() + ':' + this.v.window.end;
      },
      slideEnd: function () {
        if (!this.sliding) return;
        this.sliding = false;
        if (!this.checkFrozen()) return;
        if (this.slideX >= this.slideMax() * (this.pos() ? SELL_SLIDE : BUY_SLIDE) && this.slideOk()) { this.slideX = this.slideMax(); this.fire(); } else this.slideX = 0;
        this.frozen = null;
      },
      fire: function () {
        if (this.pos()) this.exitNow(this.pos(), true);
        else this.buy(this.sideNow(), true);
      },
      slideText: function () {
        if (this.busy) return 'Sending…';
        if (this.pulse) return this.pulse;
        if (!this.synced) return 'Syncing the clock…';
        var p = this.pos();
        if (p) {
          if (!this.canExit()) return 'No bid to sell into';
          var a = p.advice;
          return (a.action === 'CONSIDER EXIT' ? 'Consider exit · ' : '') + 'Slide to sell · ' + usd(a.sell_now);
        }
        if (this.tooLate() && this.cutoff() == null) return 'Entry closed: the server sent no entry cutoff';
        if (this.tooLate()) return 'Too late to enter this window — next window in ' + fmt.countdown(this.leftMs());
        var s = this.sideNow(), e = this.side(s);
        if (!s) return 'Pick a side to trade';
        if (this.worstBlock(s)) return this.worstHead(s);
        if (this.effMode(s) === 'market' && this.ok() && this.v.ready && this.side(s).buyable && !this.side(s).market) return 'No offer to buy right now';
        if (this.effMode(s) === 'market' && this.ok() && this.v.ready && this.side(s).market && !this.sized()) return 'Updating for ' + usd(this.amount()) + '…';
        if (!this.canBuy(s)) return this.v && !this.v.ready ? 'No live price' : 'Buy unavailable';
        return 'Slide to buy ' + sideName(s) + ' · ' + usd(this.amount()).replace(/\.00$/, '');
      },
      // The slider's second line. Buy: the market length first (a 5-min buy can never be mistaken for a 15-min one),
      // the price cap, and the server's payoff if it wins. Sell: contracts @ the bid, and what selling gives up.
      slideSub: function () {
        if (this.busy || this.pulse || !this.synced) return '';
        var p = this.pos();
        if (p) {
          if (!this.canExit()) return '';
          var a = p.advice;  // (spread + fee lives on the Hold vs sell card)
          return (Math.round((p.held || 0) * 100) / 100) + ' @ ' + c(a.bid) + ' bid · Est. payout ' + usd(a.sell_now);
        }
        var s = this.sideNow();
        if (s && !this.tooLate() && this.worstBlock(s)) return this.worstTail(s);
        if (!s || this.tooLate() || !this.canBuy(s)) return '';
        var pv = this.preview(s), spent = pv ? pv.n * pv.avg + pv.fee : null;
        var mk = pv ? 'could make ' + sUsd(pv.payout - spent) + ', lose ' + usd(spent) : '';
        return spanName(this.span) + ' · ' + (this.effMode(s) === 'market' ? 'Market' : 'Max price ' + c(this.capFor(s))) +
          (this.filledFor(s) ? ' · filled in' : '') + (mk ? ' · ' + mk : '');
      },
      slideAria: function () { var t = this.slideSub(); return this.slideText() + (t ? ' (' + t + ')' : '') + '. Drag right to confirm.'; },
      // The slide was the Yes: approve the server's ticket straight away.
      sendNow: function (t) {
        var self = this;
        return api.post('/api/trading/approve', { strategy: t.strategy, token: t.token, decision: 'yes', ticket: t.data }).then(function (r) {
          if (!r.ok && t.kind === 'exit' && self.alreadyClosed(r)) return;  // exitNow's finally reloads at once
          if (!r.ok) { if (t.kind === 'entry') self.buyRefused(r, t.meta); else self.say('Refused: ' + r.reason, 'red'); return; }
          if (t.kind === 'entry') { self.retry = null; self.filled = null; self.cpChecked = null; if (r.bracket && r.bracket.id) self.cpRecord(t, r.bracket.id); }
          if (r.bracket && r.bracket.id) self.mine[r.bracket.id] = true;
          self.say(t.kind === 'exit' ? 'Sold.' : 'Order sent' + (r.bracket && r.bracket.state === 'open' ? ': you are in.' : '.'), 'green');
          try { if (navigator.vibrate) navigator.vibrate(30); } catch (err) { /* no haptics */ }
        }, function (err) {
          if (t.kind === 'entry' && err && err.body && err.body.code) { self.buyRefused(err.body, t.meta); return; }
          self.say('Could not confirm what happened: ' + errText(err) + ' Check your position.', 'red');
        });
      },
      // A sell of a position its auto-sell, stop or a sale elsewhere already closed: the server answers
      // already_closed with what happened, from the recorded fills ("Your auto-sell already sold 2.65 @ 85¢ (+$1.25).")
      // and sent nothing. That is an outcome, not a refusal: a banner in the up colour (P&L > 0), down (< 0) or
      // neutral, with the server's reason verbatim. already_closing (the stop is selling right now) is an info banner.
      // The slider resets and the caller reloads /api/play/now at once; nothing else is recorded here.
      alreadyClosed: function (r) {
        if (!r || (r.code !== 'already_closed' && r.code !== 'already_closing')) return false;
        this.slideReset();
        if (!r.reason) {  // no wording of our own in place of the server's: say it sent none
          this.say('The server answered "' + r.code + '" with no reason text; nothing was sent. Check your position.', 'pl-done pl-info');
          return true;
        }
        if (r.code === 'already_closing') { this.say(r.reason, 'pl-done pl-info'); return true; }
        var pnl = closedPnl(r.reason);
        this.say(r.reason, 'pl-done ' + (pnl == null || pnl === 0 ? 'pl-done-flat' : pnl > 0 ? 'pl-done-up' : 'pl-done-down'));
        try { if (navigator.vibrate) navigator.vibrate(30); } catch (err) { /* no haptics */ }
        return true;
      },
      strategy: function () { return this.v && this.v.strategy; },
      canBuy: function (s) {
        var p = this.preview(s);
        return !!(this.ok() && this.v.ready && !this.tooLate() && this.side(s).buyable && this.amount() > 0 && !this.busy && !this.ticket && p && p.fits);
      },
      primary: function (s) { var a = this.call().action; return (a === 'ENTER UP' && s === 'UP') || (a === 'ENTER DOWN' && s === 'DOWN'); },
      buyLabel: function (s) { return 'BUY ' + arrows(s) + ' ' + c(this.side(s).ask); },
      // The side buttons, as the venues show them: "▲ Up 52¢" (+ "· the call" on the call's side).
      sideLabel: function (s) { return sideName(s) + ' ' + c(this.side(s).ask) + (this.primary(s) ? ' · the call' : ''); },
      say: function (text, cls) { this.msg = text; this.msgCls = cls || ''; Alpine.store('app').announce(text); },
      buy: function (s, direct) {
        var self = this, v = self.v, e = self.side(s), mode = self.effMode(s), fill = self.filledFor(s) ? self.filled : null;
        var cap = mode === 'market' ? null : (self.frozen && self.frozen.side === s && self.frozen.cap) || self.capFor(s);
        if (ORDERS.indexOf(mode) < 0) { self.say('Refused here: unknown order type "' + mode + '", so nothing was sent.', 'red'); return; }
        if (!self.canBuy(s) || (mode === 'limit' && cap == null)) return;
        if (!v.strategy) { self.say('Refused here: this market has no strategy to trade it, so nothing was sent.', 'red'); return; }
        self.saveStake(); self.busy = true; self.msg = '';
        var meta = { side: s, mode: mode, cap: cap, stake: self.amount(), ticker: v.window.ticker, windowEnd: v.window.end, tab: tabKey(self.venue, self.span),
          shown: self.cpSnapshot(s) };
        var body = {
          strategy: v.strategy, ticker: v.window.ticker, side: s, stake: self.amount(), price_mode: mode,
          take_profit: e.take_profit,
          // a filled-in ticket carries the rule's BTC stop (shown on the Filled in line); otherwise none, as before
          stop_index_below: fill ? fill.stop_index_below : e.stop_index_below, stop_index_above: fill ? fill.stop_index_above : e.stop_index_above,
          window_end: v.window.end, fair_up: v.call.fair_up
        };
        if (mode === 'limit') body.max_price = cap;  // a market buy sends no max_price (the server sets its limit: ask + market protection)
        api.post('/api/trading/prepare', body).then(function (r) {
          if (!r.ok) { self.buyRefused(r, meta); return; }
          var t = { kind: 'entry', strategy: v.strategy, data: r.ticket, token: r.token, got: Date.now(), meta: meta };
          if (direct) return self.sendNow(t);
          self.ticket = t;
        }, function (err) {
          if (err && err.body && err.body.code) { self.buyRefused(err.body, meta); return; }
          self.say('Refused: ' + errText(err), 'red');
        })['finally'](function () {
          self.busy = false; self.slideReset(); if (direct) { self.load(); self.loadScore(); } });
      },
      // ----- one-tap retry after a "price moved" refusal of a BUY: a card above the slide dock offers the same order
      // at the CURRENT ask (from each poll) for 15 s. Valid only for the same tab, ticker / window, side and amount; the
      // tap is one deliberate order through prepare -> approve with max_price = capFor(side) at that moment (the live
      // ask + your tolerance). No auto-retry: a second refusal shows the new reason and a fresh card. -----
      retry: null,
      buyRefused: function (r, meta) {
        var code = r && r.code, hint = code && REFUSE_HINT[code];
        this.say((r && r.reason ? 'Refused: ' + r.reason : 'Refused (code ' + (code || 'none') + ') with no reason text from the server.')
          + (hint ? ' ' + hint : ''), 'red');
        // limit orders only: a market buy is not refused for a moving price
        this.retry = r && r.code === 'price_moved' && meta && meta.mode === 'limit' ? Object.assign({ at: Date.now() }, meta) : null;
      },
      // Hard invalidations remove the card; soft ones (late, no live price, request in flight) disable it with a reason.
      retryGone: function () {
        var r = this.retry, v = this.v;
        if (!r) return true;
        if (!v || this.pos()) return true;
        if (Date.now() - r.at > RETRY_MS) return true;
        if (tabKey(this.venue, this.span) !== r.tab || v.window.ticker !== r.ticker || v.window.end !== r.windowEnd) return true;
        if (this.sideNow() !== r.side || Math.abs(this.amount() - r.stake) > 0.005) return true;
        if (this.orderMode !== 'limit') return true;
        return false;
      },
      retryExpire: function () { if (this.retry && this.retryGone()) this.retry = null; },
      retryWhy: function () {
        this.tick;
        var r = this.retry;
        if (!r || this.retryGone()) return 'gone';
        if (this.busy) return 'Sending…';
        if (this.tooLate()) return 'Entry is closed for this window.';
        if (!this.v.ready || this.side(r.side).ask == null) return 'No live price right now.';
        if (!this.canBuy(r.side) || this.capFor(r.side) == null) return 'Buy unavailable right now.';
        return '';
      },
      retryShow: function () { return this.retryWhy() !== 'gone'; },
      retryAsk: function () { var r = this.retry; return r ? this.side(r.side).ask : null; },
      retryHead: function () {
        var r = this.retry;
        if (!r) return '';
        var a = this.retryAsk();
        return (a == null ? 'No live price now' : (a > r.cap + 1e-9 ? 'Price ran to ' : 'Price is now ') + c(a)) + ' (your max price was ' + c(r.cap) + ')';
      },
      retryLabel: function () {
        var r = this.retry;
        if (!r) return '';
        return 'Buy ' + sideName(r.side) + ' at ' + c(this.retryAsk()) + ' · ' + usd(r.stake) + ' · ' + spanName(this.span).replace(' ', '-');
      },
      retryNote: function () {
        var r = this.retry;
        if (!r) return '';
        this.tick;
        var left = Math.max(0, Math.ceil((RETRY_MS - (Date.now() - r.at)) / 1000));
        return 'Max price ' + c(this.capFor(r.side)) + ' (ask + your ' + this.slip + '¢ tolerance) · offer ends in ' + left + ' s';
      },
      retryBuy: function () {
        var r = this.retry;
        if (!r || this.retryWhy()) return;
        this.frozen = null;  // max_price = capFor(side) now: the live ask + the tolerance
        this.buy(r.side, true);
      },
      exitNow: function (p, direct) {
        var self = this, strat = self.strategy();
        if (self.busy || self.ticket || !p) return;
        if (!strat) { self.say('Refused here: this market has no strategy to trade it, so nothing was sent.', 'red'); return; }
        self.busy = true; self.msg = '';
        api.post('/api/trading/prepare_exit', { strategy: strat, bracket_id: p.id }).then(function (r) {
          if (!r.ok && self.alreadyClosed(r)) { if (!direct) { self.load(); self.loadScore(); } return; }  // direct: the finally reloads
          if (!r.ok) { self.say('Refused: ' + r.reason, 'red'); return; }
          var t = { kind: 'exit', strategy: strat, data: r.ticket, token: r.token, got: Date.now(), pos: p };
          if (direct) return self.sendNow(t);
          self.ticket = t;
        }, function (err) { self.say('Refused: ' + errText(err), 'red'); })['finally'](function () {
          self.busy = false; self.slideX = 0; if (direct) { self.load(); self.loadScore(); } });
      },
      ticketLeft: function () {
        this.tick;
        var t = this.ticket;
        if (!t) return 0;
        return Math.max(0, Math.ceil((t.data.expires_ts - t.data.created_ts - (Date.now() - t.got)) / 1000));
      },
      answer: function (yes) {
        var self = this, t = self.ticket;
        if (!t || self.busy) return;
        self.busy = true;
        api.post('/api/trading/approve', { strategy: t.strategy, token: t.token, decision: yes ? 'yes' : 'no', ticket: t.data }).then(function (r) {
          if (!yes) { self.say('Cancelled. Nothing was sent.', ''); return; }
          if (!r.ok && t.kind === 'exit' && self.alreadyClosed(r)) return;  // the finally reloads at once
          if (!r.ok) { if (t.kind === 'entry') self.buyRefused(r, t.meta); else self.say('Refused: ' + r.reason, 'red'); return; }
          if (t.kind === 'entry') { self.retry = null; self.filled = null; self.cpChecked = null; if (r.bracket && r.bracket.id) self.cpRecord(t, r.bracket.id); }
          if (r.bracket && r.bracket.id) self.mine[r.bracket.id] = true;
          self.say(t.kind === 'exit' ? 'Sell sent.' : 'Order sent' + (r.bracket && r.bracket.state === 'open' ? ': you are in.' : '.'), 'green');
        }, function (err) {
          self.say('Could not confirm what happened: ' + errText(err) + ' Check your position.', 'red');
        })['finally'](function () { self.ticket = null; self.busy = false; self.load(); self.loadScore(); });
      },
      sheetTitle: function () { var t = this.ticket; return !t ? '' : t.kind === 'exit' ? 'Sell your position?' : 'Buy ' + sideName(t.data.side) + '?'; },
      sheetRows: function () {
        var t = this.ticket && this.ticket.data;
        if (!t) return [];
        if (this.ticket.kind === 'exit') {
          var p = this.ticket.pos || {}, a = p.advice || {};
          return [['Sell', t.contracts + ' contracts (' + t.side + ')'], ['At least', c(t.limit_price)],
            ['You get about', usd(t.contracts * t.limit_price)], ['P&L if sold now', sUsd(a.pnl_sell_now)]];
        }
        var stop = t.stop_index_below != null ? 'BTC ≤ ' + money0(t.stop_index_below) : t.stop_index_above != null ? 'BTC ≥ ' + money0(t.stop_index_above) : '—';
        var wc = t.worst_case || {};
        if (t.price_mode === 'market') {
          return [['Order type', 'Market'], ['Contracts', String(t.contracts)], ['Avg price', '~' + c(t.sized_at)],
            ['Cost + fees', '~' + usd(t.cost_at_sized)],
            ['At most', usd(wc.cost) + ' if the price runs (max price ' + c(wc.max_price) + ')'],
            ['To win at ' + fmt.hm(t.window_end), usd(t.contracts)], ['Stop', stop]];
        }
        return [['Order type', 'Limit'], ['Max price', c(t.limit_price)], ['Contracts', String(t.contracts)], ['Cost + fees', usd(t.max_cost)],
          ['Most you can lose', usd(t.max_cost)],
          ['To win at ' + fmt.hm(t.window_end), usd(t.contracts) + ' (' + sUsd(t.contracts - t.max_cost) + ')'], ['Stop', stop]];
      },
      confirmLabel: function () {
        var t = this.ticket;
        if (!t) return '';
        if (t.kind === 'exit') return 'Confirm sell · ' + c(t.data.limit_price);
        return 'Confirm buy · ' + (t.data.price_mode === 'market' ? '~' + usd(t.data.cost_at_sized) : usd(t.data.max_cost));
      },

      // ----- in the trade -----
      positions: function () { return (this.v && this.v.positions) || []; },
      pos: function () { return this.positions()[0] || null; },
      posPnl: function () { var p = this.pos(); return p ? p.unrealized : null; },
      advCls: function (a) { return !a ? '' : a.action === 'HOLD' ? 'pl-up' : a.action === 'CONSIDER EXIT' ? 'pl-warn' : 'pl-flat'; },
      // CONSIDER EXIT: the sell slider pulses amber once when the advice turns (not continuously).
      alertCheck: function () {
        var self = this, p = self.pos(), act = p && p.advice ? p.id + ':' + p.advice.action : null;
        if (act && act !== self.lastAction && p.advice.action === 'CONSIDER EXIT') {
          self.alert = true; clearTimeout(self.alertTimer);
          self.alertTimer = setTimeout(function () { self.alert = false; }, 1600);
        }
        self.lastAction = act;
      },
      exitLabel: function () {
        var p = this.pos(), a = p && p.advice;
        if (!a || a.bid == null) return 'EXIT NOW (no bid)';
        return 'EXIT NOW at ' + c(a.bid) + ' → ' + sUsd(a.pnl_sell_now);
      },
      // ----- in a trade: "Chance it wins now" (outlook.chance_now, the market's mid for the held side), its change
      // over ~1 min and a sparkline since entry. In-page ring of polled values keyed by the position id (a new position
      // starts a new ring); real polled values only, a null (stale quote) is a gap; the change needs a sample >= 45 s
      // old, exactly like the market trend on the call card. -----
      noteChance: function () {
        var v = this.v, p = this.pos();
        if (!v || !p || v.now == null) return;
        if (!chance || chance.id !== p.id) chance = { id: p.id, pts: [] };
        var pts = chance.pts, o = p.outlook;
        if (pts.length && v.now <= pts[pts.length - 1][0]) return;
        pts.push([v.now, o && o.chance_now != null ? o.chance_now : null]);
        if (pts.length > TREND_CAP) pts = pts.filter(function (q, i) { return q[1] == null || i % 2 === 0 || i === pts.length - 1; });
        if (pts.length > TREND_CAP) pts = pts.slice(pts.length - TREND_CAP);
        chance.pts = pts;
      },
      look: function () { var p = this.pos(); return (p && p.outlook) || {}; },
      chancePts: function () { var p = this.pos(); return p && chance && chance.id === p.id ? chance.pts : []; },
      chanceDelta: function () {
        var v = this.v, now = this.look().chance_now, best = null;
        if (!v || now == null) return null;
        this.chancePts().forEach(function (q) {
          var age = v.now - q[0];
          if (q[1] == null || age < TREND_MIN_MS) return;
          if (!best || Math.abs(age - TREND_MS) < Math.abs(v.now - best[0] - TREND_MS)) best = q;
        });
        return best ? Math.round(now * 100) - Math.round(best[1] * 100) : null;
      },
      chanceText: function () {
        var d = this.chanceDelta();
        if (d == null) return '— in 1 min';
        return (d > 0 ? '▲ +' + d : d < 0 ? '▼ −' + (-d) : '■ 0') + ' in 1 min';
      },
      chanceCls: function () { var d = this.chanceDelta(); return d == null || d === 0 ? 'pl-trend-flat' : d > 0 ? 'pl-up' : 'pl-down'; },
      // The big number's colour: at or above the entry price (the chance you paid for) = up, under it = down.
      chanceTone: function () {
        var p = this.pos(), ch = this.look().chance_now;
        if (!p || ch == null || p.entry_avg == null) return '';
        return ch >= p.entry_avg - 1e-9 ? 'pl-up' : 'pl-down';
      },
      chanceAria: function () {
        var o = this.look(), d = this.chanceDelta();
        return 'Chance it wins now ' + pct(o.chance_now) + ', ' + (d == null ? 'change over 1 minute not known yet'
          : (d === 0 ? 'unchanged' : (d > 0 ? 'up ' : 'down ') + Math.abs(d) + ' points') + ' in the last minute') +
          '. Model says ' + pct(o.model_chance) + '.';
      },
      disagreeText: function () {
        var o = this.look();
        if (o.disagree !== true || o.chance_now == null || o.model_chance == null) return '';
        return '⚠ Model and market disagree by ' + Math.round(Math.abs(o.chance_now - o.model_chance) * 100) + ' points — look closer';
      },
      // The sparkline: x from the entry (created_ts) to now, y covers the values and the entry price, >= 20 points.
      chanceScale: function () {
        var v = this.v, p = this.pos(), pts = this.chancePts(), ys = [];
        if (!v || !p) return null;
        if (p.entry_avg != null) ys.push(p.entry_avg);
        pts.forEach(function (q) { if (q[1] != null) ys.push(q[1]); });
        if (!ys.length) return null;
        var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
        if (hi - lo < 0.2) { var mid = (hi + lo) / 2; lo = mid - 0.1; hi = mid + 0.1; }
        var pad = (hi - lo) * 0.08, t0 = Number(p.created_ts), t1 = v.now;
        if (!(t0 > 0) || !(t1 > t0)) t0 = pts.length ? pts[0][0] : t1 - 1;
        var span = Math.max(1, t1 - t0);
        lo -= pad; hi += pad;
        return { x: function (t) { return Math.max(0, Math.min(SPARK_W, (t - t0) / span * SPARK_W)).toFixed(1); },
          y: function (q) { return ((hi - q) / (hi - lo) * SPARK_H).toFixed(1); } };
      },
      chancePath: function () {
        var s = this.chanceScale(), d = '', prev = null;
        if (!s) return '';
        this.chancePts().forEach(function (q) {
          if (q[1] == null) { prev = null; return; }
          var join = prev && q[0] - prev[0] <= SPARK_GAP_MS;
          d += (join ? 'L' : 'M') + s.x(q[0]) + ' ' + s.y(q[1]);
          if (!join) d += 'h0.01';
          prev = q;
        });
        return d;
      },
      chanceDot: function () {
        var s = this.chanceScale(), pts = this.chancePts(), q = pts.length ? pts[pts.length - 1] : null;
        return s && q && q[1] != null ? 'M' + s.x(q[0]) + ' ' + s.y(q[1]) + 'h0.01' : '';
      },
      chanceBase: function () { var s = this.chanceScale(), p = this.pos(); return s && p && p.entry_avg != null ? 'M0 ' + s.y(p.entry_avg) + 'H' + SPARK_W : ''; },
      chanceBaseTop: function () { var s = this.chanceScale(), p = this.pos(); return s && p && p.entry_avg != null ? (Number(s.y(p.entry_avg)) / SPARK_H * 100) + '%' : null; },
      chanceLabel: function () {
        var vals = this.chancePts().filter(function (q) { return q[1] != null; }), p = this.pos();
        var base = p && p.entry_avg != null ? ', entry price ' + pct(p.entry_avg) + ' dotted' : '';
        if (vals.length < 2) return 'Chance your side wins since entry: not enough polled prices yet' + base;
        return 'Chance your side wins since entry: from ' + pct(vals[0][1]) + ' to ' + pct(vals[vals.length - 1][1]) + base;
      },
      chanceFoot: function () {
        var p = this.pos(), pts = this.chancePts();
        if (!p) return '';
        var t = 'Your side since entry at ' + pct(p.entry_avg) + (p.created_ts ? ' · ' + fmt.hm(Number(p.created_ts)) : '');
        // the ring only holds what this page polled: say so when it starts well after the entry
        if (pts.length && p.created_ts && pts[0][0] - Number(p.created_ts) > SPARK_GAP_MS) t += ' · line from ' + fmt.hm(pts[0][0]) + ' (this screen)';
        return t;
      },
      // ----- Hold vs sell: outlook.hold_value (chance_now x contracts) against outlook.sell_now (bid x contracts -
      // the exit fee); the payoffs and the cost from advice. Unknown = "—". -----
      holdAt: function () { return 'Hold · at ' + pct(this.look().chance_now); },
      holdValue: function () { var h = this.look().hold_value; return h == null ? '—' : '~' + usd(h); },
      holdWins: function () { var a = (this.pos() || {}).advice || {}; return sUsd(a.pnl_if_wins); },
      holdLoses: function () { var a = (this.pos() || {}).advice || {}; return a.pnl_if_loses == null ? '—' : '−$' + Math.abs(a.pnl_if_loses).toFixed(2); },
      sellNow: function () { return usd(this.look().sell_now); },
      sellVsCost: function () {
        var a = (this.pos() || {}).advice || {}, s = this.look().sell_now;
        return s == null || a.cost == null ? null : Math.round((s - a.cost) * 100) / 100;
      },
      sellGiveUp: function () { return usd(this.look().give_up); },

      // ----- auto-sell after entry (the resting take profit; 99c = ride, RIDE_EXIT's recommendation) -----
      // A bracket this page sent opens the "Set an auto-sell?" sheet once, on the first poll that sees it open. The
      // sheet sits above the bottom bar (no dim), so the slide-to-sell stays usable; it closes if the position closes.
      posTp: function () {
        var p = this.pos();
        if (!p || p.take_profit == null) return 'Riding to settlement';
        return p.take_profit >= 0.99 - 1e-9 ? 'Riding to settlement' : 'Auto-sell at ' + c(p.take_profit);
      },
      posTpAction: function () { var p = this.pos(); return p && p.take_profit != null && p.take_profit < 0.99 - 1e-9 ? 'Change' : 'Set auto-sell'; },
      tpCheck: function () {
        var p = this.pos();
        if (this.tpSheet && (!p || p.id !== this.tpFor || p.state !== 'open')) this.tpSheet = false;
        if (p && p.state === 'open' && p.held > 0 && this.mine[p.id] && !this.tpOffered[p.id]) {
          this.tpOffered[p.id] = true;
          if (this.tpPrompt) this.tpOpen();
        }
      },
      // "Don't show this again" / the settings toggle: only the prompt; orders already placed are never changed.
      setTpPrompt: function (on) { this.tpPrompt = !!on; save(TP_PROMPT_KEY, on ? 'on' : 'off'); },
      tpClose: function () { if (this.tpNoMore) this.setTpPrompt(false); this.tpNoMore = false; this.tpSheet = false; },
      tpHas: function () { var p = this.pos(); return !!(p && p.take_profit != null && p.take_profit < 0.99 - 1e-9); },
      tpRemove: function () { this.tpCents = 99; this.tpSet(); },
      tpOpen: function () {
        var p = this.pos();
        if (!p || p.state !== 'open') return;
        this.tpFor = p.id; this.tpMsg = ''; this.tpSheet = true;
        this.tpCents = Math.max(this.tpLo(), Math.round((p.take_profit || 0.99) * 100));
        var b = this.v && this.v.btc && this.v.btc.btc;
        this.slOn = p.stop_level != null;
        this.slDist = p.stop_level != null && b != null ? Math.max(5, Math.min(300, Math.round(Math.abs(b - p.stop_level)))) : 25;
      },
      // The optional stop-loss: a BTC level slDist dollars against the position from BTC now.
      slLevel: function () {
        var p = this.pos(), b = this.v && this.v.btc && this.v.btc.btc;
        if (!p || b == null) return null;
        return Math.round((p.side === 'UP' ? b - this.slDist : b + this.slDist) * 100) / 100;
      },
      slLabel: function () {
        var p = this.pos(), lv = this.slLevel();
        if (!this.slOn) return 'Off: nothing sells unless you slide to sell (or your auto-sell fills).';
        if (!p || lv == null) return 'No live BTC price.';
        return 'Sells at the bid if BTC ' + (p.side === 'UP' ? 'falls to ' : 'rises to ') + money0(lv) + ' ($' + this.slDist + ' from now). Drawn in red on the chart.';
      },
      // Lowest auto-sell: above both the bid (a post-only sell must rest) and the entry price.
      tpLo: function () {
        var p = this.pos(), a = (p && p.advice) || {}, lo = Math.max(a.bid || 0, (p && p.entry_avg) || 0);
        return Math.min(99, Math.floor(lo * 100 + 1e-6) + 1);
      },
      tpLabel: function () {
        var p = this.pos(), a = (p && p.advice) || {}, st = this.v && this.v.stake, tp = this.tpCents / 100;
        if (this.tpCents >= 99) return 'Ride to settlement (recommended)';
        if (!p || !st || a.cost == null) return 'Sell at ' + c(tp);
        var gain = p.held * tp - fee(p.held, tp, st.fee_rate, st.fee_rounding === 'cent') - a.cost;
        return 'Sell at ' + c(tp) + ' → ' + sUsd(gain) + ' after fees';
      },
      // Save the sheet: the stop first (no order involved), then the auto-sell (cancel/replace) if it changed.
      sheetSave: function () {
        var self = this, p = self.pos();
        if (!p || self.tpBusy || p.id !== self.tpFor) return;
        var lv = self.slOn ? self.slLevel() : null, had = p.stop_level != null;
        var stopChange = self.slOn ? (lv != null && (!had || Math.abs(lv - p.stop_level) >= 1)) : had;
        if (!stopChange) { self.tpSet(); return; }
        if (!self.strategy()) { self.tpMsg = 'Refused here: this market has no strategy, so nothing was sent.'; return; }
        self.tpBusy = true; self.tpMsg = '';
        api.post('/api/trading/stop', { strategy: self.strategy(), bracket_id: p.id, level: lv }).then(function (r) {
          self.tpBusy = false;
          if (!r.ok) { self.tpMsg = 'Refused: ' + r.reason; return; }
          self.say(lv == null ? 'Stop-loss off.' : 'Stop-loss set at ' + money0(lv) + '.', 'green');
          self.tpSet();
        }, function (e) { self.tpBusy = false; self.tpMsg = 'Could not confirm: ' + errText(e) + ' Check the position.'; });
      },
      tpSet: function () {
        var self = this, p = self.pos(), price = self.tpCents / 100;
        if (!p || self.tpBusy || p.id !== self.tpFor) return;
        if (Math.abs((p.take_profit || 0.99) - price) < 1e-9) { self.tpClose(); self.load(); return; }
        if (!self.strategy()) { self.tpMsg = 'Refused here: this market has no strategy, so nothing was sent.'; return; }
        self.tpBusy = true; self.tpMsg = '';
        api.post('/api/trading/take_profit', { strategy: self.strategy(), bracket_id: p.id, price: price }).then(function (r) {
          if (!r.ok) { self.tpMsg = 'Refused: ' + r.reason; return; }
          self.tpClose();
          self.say(price >= 0.99 ? 'Auto-sell off: riding to settlement.' : 'Auto-sell set at ' + c(price) + '.', 'green');
        }, function (e) { self.tpMsg = 'Could not confirm: ' + errText(e) + ' Check the position.'; })['finally'](function () { self.tpBusy = false; self.load(); });
      },
      // ----- the outcome card: when a position leaves (settled or sold), its result from the scoreboard for ~8 s -----
      watchOutcome: function (was) {
        var before = was && was.positions && was.positions[0], ids = this.positions().map(function (p) { return p.id; });
        if (before && before.state === 'open' && ids.indexOf(before.id) < 0) {
          this.watchId = before.id; this.watchUntil = Date.now() + 180000; this.loadScore();
        }
      },
      findOutcome: function () {
        var id = this.watchId, rows = (this.sb && this.sb.recent) || [], self = this;
        if (!id) return;
        var t = rows.filter(function (r) { return r.id === id; })[0];
        if (!t) { if (Date.now() > this.watchUntil) this.watchId = null; return; }
        this.watchId = null;
        this.outcome = { pnl: t.pnl, side: t.side, word: t.result === 'W' ? 'WON' : t.result === 'L' ? 'LOST' : 'EVEN' };
        clearTimeout(this.outcomeTimer);
        this.outcomeTimer = setTimeout(function () { self.outcome = null; }, 8000);
      },
      outcomeText: function () { var o = this.outcome; return o ? o.word + ' ' + (o.word === 'EVEN' ? usd(0) : sUsd(o.pnl)) : ''; },
      outcomeSub: function () { var o = this.outcome; return o ? arrows(o.side) + ' · tap to close' : ''; },
      closeOutcome: function () { clearTimeout(this.outcomeTimer); this.outcome = null; },
      canExit: function () { var p = this.pos(); return !!(p && p.state === 'open' && p.held > 0 && p.advice && p.advice.bid != null && !this.busy && !this.ticket); },

      // ----- the live "price to beat" chart (web/playchart.js on Lightweight Charts): BTC since the window opened
      // against the strike, x fixed from open to close. Data: /api/play/now's btc_series (per-second index thinned to
      // <= 300 points) + the live price, redrawn on every 2 s poll. The chart object lives outside Alpine (closure). -----
      odds: load(ODDS_KEY) === '1', levels: load(LEVELS_KEY) === '1', readout: '', sigTip: '', closeX: null, oddsEmpty: true,
      near: function () { var e = this.v && this.v.exit; return e && e.near_stop_usd != null ? e.near_stop_usd : null; },
      gap: function () { var b = this.v && this.v.btc; return b && b.gap != null ? b.gap : null; },
      stopDist: function () { var p = this.pos(); return p && p.advice ? p.advice.stop_distance : null; },
      // In a trade and BTC within the CONSIDER EXIT distance of the stop (the server's NEAR_STOP_USD).
      danger: function () { var d = this.stopDist(), n = this.near(); return d != null && n != null && d <= n; },
      ptbBtc: function () { var b = this.v && this.v.btc; return b && b.btc != null ? dollars(b.btc) : '—'; },
      ptbChip: function () {
        var d = this.stopDist(), g = this.gap();
        if (this.danger()) return d <= 0 ? '⚠ past your stop' : '⚠ ' + dollars(d) + ' from your stop';
        return g == null ? '' : (g >= 0 ? '▲ ' + dollars(g) + ' above' : '▼ ' + dollars(-g) + ' below');
      },
      ptbCls: function () { var g = this.gap(); return this.danger() ? 'pl-ptb-warn' : g == null ? '' : g >= 0 ? 'pl-ptb-up' : 'pl-ptb-down'; },
      ptbSub: function () {
        var b = this.v && this.v.btc;
        return 'Price to beat ' + (b && b.strike != null ? dollars(b.strike) : '— (not known yet)') + ' · ' + this.left() + ' left';
      },
      // The line's points: the server's series, then the live price when it is newer (the line ends at the header's
      // number). Recorded prices only.
      ptbPts: function () {
        var v = this.v, s = ((v && v.btc_series) || []).slice(), b = v && v.btc;
        if (b && b.btc != null && b.source === 'live' && v.now >= v.window.start && (!s.length || v.now > s[s.length - 1][0])) s.push([v.now, b.btc]);
        return s;
      },
      ptbEmpty: function () { return !!(this.v && this.ok() && !(this.v.btc_series || []).length); },
      ptbLabel: function () {
        var k = this.coneOk();
        return 'BTC since ' + this.windowText().split('–')[0] + ' against the price to beat' + (this.ptbChip() ? ', now ' + this.ptbChip() : '') +
          (k ? '. Likely close by ' + this.endHm() + ': 1σ ' + dollars(k.bands['1'][0]) + ' to ' + dollars(k.bands['1'][1]) +
            ', 2σ ' + dollars(k.bands['2'][0]) + ' to ' + dollars(k.bands['2'][1]) : '');
      },
      // The "likely close" cone: drawn in a trade only (the in-trade design), from the server's cone. A cone that does
      // not belong to this window or is malformed is not drawn and says why (coneNote); null = simply hidden.
      coneCheck: function () {
        var v = this.v, k = v && v.cone, p = this.pos();
        if (!k || !p || Number(p.window_end) !== v.window.end) return { cone: null, err: '' };
        var pair = function (b) { return Array.isArray(b) && b.length === 2 && isFinite(b[0]) && isFinite(b[1]) && b[0] <= b[1]; };
        if (!k.bands || !pair(k.bands['1']) || !pair(k.bands['2'])) return { cone: null, err: 'the server sent no 1σ / 2σ ranges' };
        if (!isFinite(k.btc) || !isFinite(k.now_ms) || !isFinite(k.end_ms)) return { cone: null, err: 'the server sent no start point' };
        if (k.end_ms !== v.window.end) return { cone: null, err: 'it was for another window' };
        if (!(k.end_ms > k.now_ms)) return { cone: null, err: '' };  // at the close there is nothing left to draw
        return { cone: k, err: '' };
      },
      coneOk: function () { return this.coneCheck().cone; },
      coneNote: function () { var e = this.coneCheck().err; return e ? 'Likely-close range not drawn: ' + e + '.' : ''; },
      oddsOn: function () { return this.odds || !!this.pos(); },
      toggleOdds: function () { this.odds = !this.odds; save(ODDS_KEY, this.odds ? '1' : '0'); this.loadOdds(); this.drawPtb(); },
      toggleLevels: function () { this.levels = !this.levels; save(LEVELS_KEY, this.levels ? '1' : '0'); this.drawPtb(); },
      // Reference lines (opt-in, not signals): the server's running high / low / open from the full per-second series,
      // and the 3 round $50 levels nearest the price inside the line's own range. (The previous window's settle is about this
      // window's strike, so it is not drawn twice.)
      refLevels: function (pts, strike) {
        var lv = (this.v && this.v.btc_levels) || {}, out = [];
        if (lv.hi != null) out.push({ price: lv.hi, title: 'hi' });
        if (lv.lo != null) out.push({ price: lv.lo, title: 'lo' });
        if (lv.open != null && (strike == null || Math.abs(lv.open - strike) >= 1)) out.push({ price: lv.open, title: 'open' });
        var ys = pts.map(function (q) { return q[1]; }).concat(strike != null ? [strike] : []);
        if (!ys.length) return out;
        var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys), now = pts.length ? pts[pts.length - 1][1] : ys[0], r = [];
        for (var k = Math.ceil(lo / 50) * 50; k <= hi; k += 50) r.push(k);
        r.sort(function (a, b) { return Math.abs(a - now) - Math.abs(b - now); });  // the 3 nearest the price now
        r.slice(0, 3).forEach(function (k) { out.push({ price: k, title: '' }); });
        return out;
      },
      drawPtb: function () {
        var self = this, el = self.$refs.ptb, v = self.v;
        if (!el || !v || !self.ok() || !el.clientWidth) return;
        try {
          if (!pc) {
            pc = new window.PlayChart(el, self.$refs.odds);
            pc.onCross(function (r) {
              if (!r) { self.readout = ''; return; }
              var s = self.v && self.v.btc && self.v.btc.strike, d = s == null ? null : r.btc - s;
              self.readout = fmt.time(r.time * 1000) + ' · ' + dollars(r.btc) + (d == null ? '' : ' · ' + (d >= 0 ? '▲ ' + dollars(d) + ' above' : '▼ ' + dollars(-d) + ' below'));
            });
          }
          var pts = self.ptbPts(), strike = v.btc.strike, p = self.pos();
          var trade = p && Number(p.window_end) === v.window.end ? p : null;
          pc.set({ t0: v.window.start, t1: v.window.end, pts: pts, strike: strike,
            stop: trade ? trade.stop_level : null, side: trade ? trade.side : null, near: self.near(),
            entry: trade && trade.entry_btc != null ? { ts: Number(trade.created_ts), btc: trade.entry_btc } : null,
            levels: self.levels ? self.refLevels(pts, strike) : [], cone: self.coneOk() });
          window.requestAnimationFrame(function () { self.closeX = pc.closeX(); });
          if (self.oddsOn()) self.drawOdds();
        } catch (e) { self.error = 'Chart unavailable: ' + e.message; }
      },

      // ----- the odds pane: before entry (opt-in) the venue's Up mid; in a trade (always on) the held side's bid with
      // the auto-sell line. Kalshi: the recorded Kalshi quotes (/api/coach/series, every 5 s while shown) + the live
      // quote. Polymarket: the live Polymarket quote from each 2 s poll since this screen opened (nothing earlier is
      // recorded). Each is labelled with the venue whose data it is. -----
      loadOdds: function () {
        var self = this;
        if (!self.oddsOn() || self.venue !== 'kalshi' || !self.span) return;
        api.get('/api/coach/series', { window: 'current' }, 8000).then(function (s) {
          oddsK = { boundary: s.boundary, pts: (s.points || []).filter(function (q) { return q.yes_bid != null; })
            .map(function (q) { return [q.ts, { yes_bid: q.yes_bid, yes_ask: q.yes_ask, no_bid: q.no_bid, no_ask: q.no_ask }]; }) };
          self.drawPtb();
        }, function () { /* the pane keeps the live points */ });
      },
      // Every poll: the venue's own quote, kept for this window (Polymarket's only history; Kalshi's live tail).
      noteQuote: function () {
        var v = this.v, m = v && v.market;
        if (!v) return;
        var key = this.oddsKey();
        if (!oddsP || oddsP.key !== key) oddsP = { key: key, pts: [] };
        if (m && m.fresh) oddsP.pts.push([v.now, { yes_bid: m.yes_bid, yes_ask: m.yes_ask, no_bid: m.no_bid, no_ask: m.no_ask }]);
        if (oddsP.pts.length > 600) oddsP.pts = oddsP.pts.filter(function (q, i) { return i % 2 === 0; });
      },
      // A 5-min and a 15-min window can open at the same minute: the tab is part of the key.
      oddsKey: function () { return this.v ? tabKey(this.venue, this.span) + ':' + this.v.window.start : ''; },
      oddsPts: function () {
        var v = this.v, p = this.pos(), src = [];
        if (!v) return [];
        if (this.venue === 'kalshi' && oddsK && oddsK.boundary === v.window.start) src = oddsK.pts.slice();
        var tail = (oddsP && oddsP.key === this.oddsKey()) ? oddsP.pts : [];
        var lastT = src.length ? src[src.length - 1][0] : -1;
        tail.forEach(function (q) { if (q[0] > lastT) src.push(q); });
        return src.map(function (q) {
          var x = q[1], val = p ? (p.side === 'UP' ? x.yes_bid : x.no_bid)
            : (x.yes_bid != null && x.yes_ask != null ? (x.yes_bid + x.yes_ask) / 2 : null);
          return [q[0], val];
        }).filter(function (q) { return q[1] != null; });
      },
      oddsLabel: function () {
        var vn = this.venueName(), since = this.venue === 'polymarket' ? ' · since you opened this screen' : '';
        return (this.pos() ? 'Your side\'s bid · ' + vn : vn + ' Up odds (mid)') + since;
      },
      oddsNow: function () {
        var p = this.pos(), m = this.v && this.v.market;
        if (p) return p.advice && p.advice.bid != null ? c(p.advice.bid) : '';
        return m && m.fresh && m.mid_up != null ? '▲ Up ' + pct(m.mid_up) : '';
      },
      autoSell: function () { var p = this.pos(); return p && p.take_profit != null && p.take_profit < 0.99 - 1e-9 ? p.take_profit : null; },
      drawOdds: function () {
        var v = this.v, p = this.pos(), pts = this.oddsPts();
        if (!pc || !v) return;
        pc.setOdds({ t0: v.window.start, t1: v.window.end, pts: pts, autosell: p ? this.autoSell() : null,
          tone: p ? (p.side === 'UP' ? 'up' : 'down') : 'info' });
        this.oddsEmpty = pts.length < 2;
      },

      // ----- the signals strip: /api/play/now's own signals, one chip each; a tap explains it -----
      sigChips: function () {
        var self = this, sig = (self.v && self.v.signals) || [], cl = self.call();
        var ar = function (l) { return l === 'UP' ? '▲' : l === 'DOWN' ? '▼' : '■'; };
        var k$ = function (x) { return x == null ? '—' : '$' + (x >= 1000 ? (x / 1000).toFixed(x >= 10000 ? 0 : 1) + 'k' : Math.round(x)); };
        return sig.map(function (s) {
          var t;
          if (s.key === 'market') t = 'Market ' + pct(cl.mid_up) + ' ' + ar(s.lean);
          else if (s.key === 'model') t = 'Model ' + pct(cl.fair_up) + ' ' + ar(s.lean);
          else if (s.key.indexOf('pressure_') === 0) t = s.name.replace(/ pressure$/, '') + ' ' + ar(s.lean);
          else if (s.key === 'crowd') t = 'Crowd ' + ar(s.lean) + (s.up_usd != null ? ' ' + k$(s.up_usd) + '/' + k$(s.down_usd) : ' —');
          else t = s.name + ' ' + ar(s.lean);
          return { key: s.key, text: t, cls: s.lean === 'UP' ? 'pl-up' : s.lean === 'DOWN' ? 'pl-down' : 'pl-flat', tip: self.sigExplain(s) };
        });
      },
      sigExplain: function (s) {
        var vn = this.venueName();
        if (s.key === 'market') return vn + "'s Up mid now: ▲ at 55% or more, ▼ at 45% or less. The market is well calibrated; this describes it, it is not an edge.";
        if (s.key === 'model') return "Our random-walk model's Up odds from BTC vs the strike and the time left. In our tests it added nothing beyond the market price.";
        if (s.key.indexOf('pressure_') === 0) return "The Coach's " + s.name.toLowerCase() + ': ' + (s.value || 'no lean') + ' (▲ leans Up, ▼ leans Down, ■ no lean).';
        if (s.key === 'crowd') return 'Dollars bought ▲ Up / ▼ Down on ' + vn + ' in the last minute' + (s.value ? ': ' + s.value : ' (no data)') + '. ' + (s.note || '');
        return s.name + (s.value ? ': ' + s.value : '');
      },
      sigTap: function (ch) { this.sigTip = this.sigTip === ch.tip ? '' : ch.tip; },

      // ----- in-trade: the parameters bar and the "what to watch" sentence (numbers from pos() / advice only) -----
      params: function () {
        var p = this.pos();
        if (!p) return [];
        var a = p.advice || {}, d = a.stop_distance, tp = this.autoSell(), out = [];
        out.push({ k: 'side', text: arrows(p.side) + ' ' + (Math.round((p.held || 0) * 100) / 100) + ' @ ' + c(p.entry_avg), cls: p.side === 'UP' ? 'pl-up' : 'pl-down' });
        out.push({ k: 'entry', text: 'Entry BTC ' + (p.entry_btc == null ? '—' : dollars(p.entry_btc)) });
        out.push({ k: 'stop', text: p.stop_level == null ? 'Stop: off' : 'Stop BTC ' + dollars(p.stop_level) + (d == null ? '' : d <= 0 ? ' · passed' : ' · ' + dollars(d) + ' away'),
          cls: this.danger() ? 'pl-warn' : '', tap: true });
        out.push({ k: 'tp', text: tp == null ? 'Riding to settlement' : 'Auto-sell ' + c(tp) + ' (now ' + c(a.bid) + ')', tap: true });
        // (sell now / if it wins live on the Hold vs sell card)
        out.push({ k: 'left', text: this.left() + ' left', cls: this.clockCls() });
        return out;
      },
      watchLine: function () {
        var p = this.pos(), b = this.v && this.v.btc;
        if (!p || !p.advice) return '';
        var a = p.advice, d = a.stop_distance, g = this.gap(), parts = [];
        if (d != null && d <= 0) parts.push('⚠ BTC is past your stop: the stop sells at the bid.');
        else if (this.danger()) parts.push('⚠ BTC is ' + dollars(d) + ' from your stop — the stop sells automatically at ' + dollars(p.stop_level) + '.');
        else {
          var s = 'BTC ' + (g == null ? 'vs the strike unknown' : dollars(Math.abs(g)) + (g >= 0 ? ' above' : ' below') + ' the strike');
          if (d != null) s += ', ' + dollars(d) + (p.side === 'UP' ? ' above' : ' below') + ' your stop';
          parts.push(s + '.');
          if (p.entry_btc != null) parts.push('Watch the ' + dollars(p.entry_btc) + ' entry line.');
        }
        (a.reasons || []).forEach(function (r) { if (r.indexOf('stop') < 0) parts.push(r); });
        if (a.pnl_sell_now != null) parts.push('Sell now: ' + sUsd(a.pnl_sell_now) + '.');
        return parts.join(' ');
      },

      // ----- your last N -----
      att: function () { return (this.sb && this.sb.attempts) || null; },
      attStats: function () {
        var a = this.att(), pts = (a && a.points) || [], w = 0, l = 0, sum = 0, n = 0;
        pts.forEach(function (p) { if (p.result === 'W') w++; else if (p.result === 'L') l++; if (p.result !== 'P' && p.entry_avg != null) { sum += p.entry_avg; n++; } });
        var won = w + l ? w / (w + l) : null, said = n ? sum / n : null;
        return { won: won, said: said, edge: won == null || said == null ? null : Math.round((won - said) * 100) };
      },
      streakText: function () {
        var k = this.att() && this.att().streak;
        return k && k.n ? k.n + (k.kind === 'W' ? (k.n === 1 ? ' win' : ' wins') : (k.n === 1 ? ' loss' : ' losses')) + ' in a row' : '';
      },
      // Followed vs against the call at entry: the bracket's stored fair_up (our model's odds sent with the ticket).
      calLine: function () {
        var cal = this.att() && this.att().calibration;
        if (!cal) return '';
        var f = function (r) { return r.wins + '–' + r.losses + ', ' + sUsd(r.pnl); };
        return 'When you followed the call: ' + f(cal.followed) + ' · against it: ' + f(cal.against) +
          (cal.unknown ? ' · no call recorded: ' + cal.unknown : '');
      },
      pnlChart: function () {
        var a = this.att(), pts = (a && a.points) || [];
        if (!pts.length) return null;
        var cums = [0].concat(pts.map(function (p) { return p.cum; }));
        var lo = Math.min.apply(null, cums.concat([0])), hi = Math.max.apply(null, cums.concat([0]));
        if (hi - lo < 0.01) { hi += 1; lo -= 1; }
        var n = pts.length;
        var X = function (i) { return PAD + i / n * (W - 2 * PAD); };
        var Y = function (y) { return PAD + (hi - y) / (hi - lo) * (H - 2 * PAD); };
        var line = cums.map(function (y, i) { return (i ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(y).toFixed(1); }).join(' ');
        var area = line + ' L' + X(n).toFixed(1) + ' ' + Y(0).toFixed(1) + ' L' + X(0).toFixed(1) + ' ' + Y(0).toFixed(1) + ' Z';
        var dots = pts.map(function (p, i) { return { x: X(i + 1), y: Y(p.cum), p: p, last: i === n - 1 }; });
        return { line: line, area: area, zeroY: Y(0), dots: dots, up: cums[n] >= 0 };
      },
      // Dots as SVG markup (x-for cannot run inside <svg>); a tap is read from the dot's data-i.
      dotsSvg: function () {
        var ch = this.pnlChart();
        if (!ch) return '';
        return ch.dots.map(function (d, i) {
          var cls = d.p.result === 'W' ? 'pl-dot-w' : d.p.result === 'L' ? 'pl-dot-l' : 'pl-dot-p';
          return '<circle data-i="' + i + '" cx="' + d.x.toFixed(1) + '" cy="' + d.y.toFixed(1) + '" r="12" fill="transparent"></circle>' +
            '<circle data-i="' + i + '" class="' + cls + '" cx="' + d.x.toFixed(1) + '" cy="' + d.y.toFixed(1) + '" r="' + (d.last ? 6 : 4) + '"></circle>';
        }).join('');
      },
      pickDot: function (e) {
        var i = e && e.target && e.target.getAttribute ? e.target.getAttribute('data-i') : null, ch = this.pnlChart();
        if (i != null && ch && ch.dots[Number(i)]) this.pick = ch.dots[Number(i)].p;
      },
      // The tapped dot in three parts: when · which market (an unknown venue key is shown raw, as a warning) · result.
      pickWhen: function () { var p = this.pick; return p ? fmt.dateTime(p.ts).slice(5, 16) + ' · ' : ''; },
      pickVenueKnown: function () { var p = this.pick; return !!(p && TRADE_VENUES[p.venue]); },
      pickVenue: function () {
        var p = this.pick;
        if (!p) return '';
        return TRADE_VENUES[p.venue] || '⚠ unknown venue "' + (p.venue == null ? '' : p.venue) + '"';
      },
      pickRest: function () {
        var p = this.pick;
        if (!p) return '';
        return ' · ' + p.side + ' @ ' + c(p.entry_avg) + ' → ' + (p.exit || '?') + (p.exit_avg != null ? ' ' + c(p.exit_avg) : '') + ' · ' + sUsd(p.pnl);
      },

      // ----- mode (the Coach's typed confirmation for production) -----
      modeChanged: function () { return this.mode() && this.modeForm.mode && this.modeForm.mode !== this.mode().mode; },
      canSetMode: function () {
        if (!this.modeChanged() || this.busy) return false;
        return this.modeForm.mode !== 'prod' || this.modeForm.confirm.trim() === PROD_CONFIRM;
      },
      setMode: function () {
        var self = this;
        if (!self.canSetMode()) return;
        if (!self.strategy()) { self.modeMsg = 'Refused here: this market has no strategy, so its mode cannot change.'; return; }
        self.busy = true; self.modeMsg = '';
        api.post('/api/trading/mode', { strategy: self.strategy(), mode: self.modeForm.mode, confirm: self.modeForm.confirm.trim() || null }).then(function (r) {
          self.modeMsg = r.ok ? 'Mode: ' + r.mode + '.' : 'Refused: ' + r.reason;
          if (r.ok) self.modeForm.confirm = '';
        }, function (e) { self.modeMsg = 'Refused: ' + errText(e); })['finally'](function () { self.busy = false; self.load(); self.loadScore(); });
      },

      // ----- the kill switch (moved here from the retired Coach; same two calls, same confirm on Resume only) -----
      // The trading switch is shared by every strategy's service, so "you" reads it for all of them.
      loadKill: function () {
        var self = this;
        api.get('/api/trading/status', { strategy: 'you' }, 4000).then(function (s) {
          if (s && typeof s.killed === 'boolean') { self.kTrading = s.killed; self.kTradingErr = ''; }
          else { self.kTrading = null; self.kTradingErr = (s && s.reason) || 'the server sent no kill switch state'; }
        }, function (e) { self.kTrading = null; self.kTradingErr = errText(e); });
        api.get('/api/coach/pause', null, 4000).then(function (p) {
          if (p && typeof p.paused === 'boolean') { self.kCoach = p.paused; self.kCoachErr = ''; }
          else { self.kCoach = null; self.kCoachErr = 'the server sent no pause state'; }
        }, function (e) { self.kCoach = null; self.kCoachErr = errText(e); });
      },
      killed: function () { var m = this.mode(); return this.kTrading === true || this.kCoach === true || !!(m && m.killed); },
      // Quiet round KILL until engaged; engaged it is a red pill: "Killed" (a phone cannot resume) or "Resume".
      killLabel: function () { return this.killBusy ? '…' : this.killed() ? (this.phone() ? 'Killed' : 'Resume') : 'KILL'; },
      // Turning the kill switch off is PC-only; turning it on works everywhere.
      resumeLocked: function () { return this.killed() && this.phone(); },
      killState: function () {
        var t = this.kTrading === true || (this.mode() && this.mode().killed) ? 'kill switch ON' : this.kTrading === false ? 'on' : 'unknown (' + (this.kTradingErr || 'loading') + ')';
        var k = this.kCoach === true ? 'paused' : this.kCoach === false ? 'running' : 'unknown (' + (this.kCoachErr || 'loading') + ')';
        return 'Trading: ' + t + ' · Coach engine: ' + k;
      },
      // One switch for both, exactly as the Coach did it: the trading kill switch (no new orders in any mode, every
      // auto-approve session ends) and the coach pause. Turning it on needs no confirm; Resume asks first.
      toggleKill: function () {
        var self = this, kill = !self.killed(), fails = [];
        if (self.killBusy || (!kill && self.phone())) return;
        if (!kill && !window.confirm('Resume? New entries become possible again (each still needs your YES).')) return;
        self.killBusy = true; self.killMsg = '';
        var note = function (what) { return function (e) { fails.push(what + ': ' + errText(e)); }; };
        Promise.all([
          api.post(kill ? '/api/trading/kill' : '/api/trading/resume').catch(note('trading')),
          api.post('/api/coach/pause', { paused: kill }).catch(note('coach'))
        ]).then(function () {
          self.killMsg = fails.length ? 'Kill switch ' + (kill ? 'on' : 'off') + ' failed for ' + fails.join('; ') : '';
          Alpine.store('app').announce(fails.length ? self.killMsg : kill ? 'Kill switch on: no new entries.' : 'Resumed.');
        })['finally'](function () { self.killBusy = false; self.loadKill(); self.load(); });
      },
      // ----- settings sheet (the gear): the phone access card inside reloads on the pl-settings event -----
      setTheme: function (m) {
        if (!window.BTCTheme) return;
        var saved = window.BTCTheme.set(m);
        this.themeMode = m; this.themeUnknown = window.BTCTheme.unknown;
        this.themeMsg = saved ? '' : 'Could not save the theme on this device (storage blocked): it applies until you close this page.';
      },
      openSettings: function () {
        this.settings = true; this.loadKill();
        window.dispatchEvent(new CustomEvent('pl-settings'));
      },

      sideName: sideName, THEMES: THEMES, THEME_NAMES: THEME_NAMES, CHIPS: CHIPS, VENUES: VENUES, VENUE_NAMES: VENUE_NAMES, W: W, H: H, PROD_CONFIRM: PROD_CONFIRM, usd: usd, sUsd: sUsd, c: c, pct: pct, money0: money0, arrows: arrows
    };
  };

  // ----- Record (design pass 2026-10-31): nested inside Play's component (index.html .pl-scope), so "Your trades" is
  // Play's own record view and the order settings are Play's own. Its card is GET /api/record/summary?range= (the
  // Telegram daily summary's computation: closed trades after every fee, you / agent, API, by rule); the mode rows are
  // GET /api/play/limits's balance_cap (each Play market's strategy and mode now), switched with the existing
  // POST /api/trading/mode {strategy, mode, confirm}. The range is remembered on this device; an unknown stored range
  // is reported and Today is used until one is tapped (never silently swapped). -----
  var RANGE_KEY = 'record.range', RANGES = ['today', 'week', 'all'], RANGE_NAMES = { today: 'Today', week: 'Week', all: 'All' };
  // By rule | By agent (the summary's by_agent: net after API, trades, wins, API; Clone and Retire per agent)
  var BY_KEY = 'record.by', BYS = ['rule', 'agent'], BY_NAMES = { rule: 'By rule', agent: 'By agent' };
  // The Play markets by their tab key (the backend's venue_key); an unknown key is shown raw as a warning.
  var MARKET_NAMES = { kalshi: 'Kalshi · BTC 15m', polymarket: 'Polymarket · BTC 15m', polymarket_5m: 'Polymarket · BTC 5m' };
  var MARKET_ORDER = ['kalshi', 'polymarket', 'polymarket_5m'];
  window.recordView = function () {
    var raw = load(RANGE_KEY), known = RANGES.indexOf(raw) >= 0;
    return {
      RANGES: RANGES, RANGE_NAMES: RANGE_NAMES,
      rcRange: known ? raw : 'today', rcRangeUnknown: raw != null && !known ? raw : null,
      rcSum: null, rcErr: '', rcSeq: 0, rcModes: null, rcModeErr: '', rcForms: {},
      BYS: BYS, BY_NAMES: BY_NAMES, rcBy: BYS.indexOf(load(BY_KEY)) >= 0 ? load(BY_KEY) : 'rule',
      rcAsk: '', rcBusy: '', rcMsg: '', rcMsgCls: '',  // Retire: the agent asked about, the one being retired
      init: function () {
        var self = this;
        window.onView(this, 'record', function () { self.rcLoad(); self.rcLoadModes(); });
        // a fill or a settle while Record is open: the numbers refresh (and flash where they changed)
        setInterval(function () { if (Alpine.store('app').view === 'record' && document.visibilityState === 'visible') self.rcLoad(); }, 15000);
      },
      rcSet: function (r) {
        if (RANGES.indexOf(r) < 0) { this.rcErr = 'Unknown range "' + r + '".'; return; }
        this.rcRange = r; this.rcRangeUnknown = null; save(RANGE_KEY, r); this.rcSum = null; this.rcLoad();
      },
      rcSetBy: function (b) {
        if (BYS.indexOf(b) < 0) { this.rcErr = 'Unknown view "' + b + '".'; return; }
        this.rcBy = b; save(BY_KEY, b); this.rcAsk = ''; this.rcMsg = '';
      },
      agentLine: function (a) {
        return this.plural(a.trades, 'trade') + ' · ' + a.wins + ' won · API ' + this.money(a.api_usd) +
          (a.runs > 1 ? ' · ' + a.runs + ' runs' : '') + (a.archived ? ' · retired' : '');
      },
      // the best agent that can be cloned (Record's "Clone the best agent")
      bestAgent: function () { return ((this.rcSum && this.rcSum.by_agent) || []).filter(function (a) { return a.agent_id && a.trades; })[0] || null; },
      rcPhone: function () { var st = window.Alpine && Alpine.store('agent'); return !!(st && st.pc === false); },
      // Clone: New agent on the Agents tab, filled in from this agent's last launch under a new name
      rcClone: function (a) {
        if (!a || !a.agent_id) return;
        window.navTo('agent');
        window.dispatchEvent(new CustomEvent('agent-clone', { detail: { agent_id: a.agent_id } }));
      },
      // Retire: archive the agent (POST /api/agent/agents/{id}/archive, PC only); it keeps its runs
      rcRetire: function (a) {
        var self = this;
        if (!a || !a.agent_id || self.rcBusy) return;
        self.rcBusy = a.agent_id; self.rcMsg = '';
        api.post('/api/agent/agents/' + encodeURIComponent(a.agent_id) + '/archive').then(function (r) {
          if (r && r.ok) { self.rcMsg = a.name + ' is retired. Its runs stay in the record.'; self.rcMsgCls = ''; self.rcAsk = ''; }
          else { self.rcMsg = (r && r.reason) || 'Not retired.'; self.rcMsgCls = 'red'; }
        }, function (e) { self.rcMsg = 'Not retired: ' + errText(e); self.rcMsgCls = 'red'; })['finally'](function () {
          self.rcBusy = ''; self.rcLoad();
          var st = window.Alpine && Alpine.store('agent');
          if (st && st.enabled) st.loadFleet();
        });
      },
      rcLoad: function () {
        var self = this, seq = ++self.rcSeq, r = self.rcRange;
        api.get('/api/record/summary', { range: r }).then(function (x) {
          if (seq !== self.rcSeq) return;
          self.rcSum = x; self.rcErr = '';
        }, function (e) { if (seq === self.rcSeq) self.rcErr = errText(e); });
      },
      rcLoadModes: function () {
        var self = this;
        api.get('/api/play/limits').then(function (x) {
          var caps = (x && x.balance_cap) || {}, forms = Object.assign({}, self.rcForms);
          Object.keys(caps).forEach(function (k) {
            if (!forms[k]) forms[k] = { mode: caps[k].mode || '', confirm: '', msg: '', busy: false };
            else if (!forms[k].busy && !forms[k].confirm) forms[k].mode = caps[k].mode || '';
          });
          self.rcForms = forms; self.rcModes = caps; self.rcModeErr = '';
        }, function (e) { self.rcModeErr = errText(e); });
      },
      modeRows: function () {
        var caps = this.rcModes || {}, forms = this.rcForms;
        return Object.keys(caps).filter(function (k) { return !!forms[k]; }).sort(function (a, b) {
          var i = MARKET_ORDER.indexOf(a), j = MARKET_ORDER.indexOf(b);
          return (i < 0 ? 99 : i) - (j < 0 ? 99 : j) || (a < b ? -1 : 1);
        }).map(function (k) {
          var m = caps[k], w = MODE_WORDS[m.mode];
          return { key: k, strategy: m.strategy, venue: m.venue, mode: m.mode, known: !!w && !!MARKET_NAMES[k],
            label: MARKET_NAMES[k] || '⚠ unknown market "' + k + '"', word: w || '⚠ unknown mode "' + m.mode + '"' };
        });
      },
      modeOpen: function (m) { var f = this.rcForms[m.key]; if (f && !f.busy) { f.mode = m.mode; f.confirm = ''; f.msg = ''; } },
      canSetModeFor: function (m) {
        var f = this.rcForms[m.key];
        if (!f || f.busy || !f.mode || f.mode === m.mode) return false;
        return f.mode !== 'prod' || f.confirm.trim() === PROD_CONFIRM;
      },
      setModeFor: function (m) {
        var self = this, f = self.rcForms[m.key];
        if (!self.canSetModeFor(m)) return;
        if (!m.strategy) { f.msg = 'Refused here: this market has no strategy, so its mode cannot change.'; return; }
        f.busy = true; f.msg = '';
        api.post('/api/trading/mode', { strategy: m.strategy, mode: f.mode, confirm: f.confirm.trim() || null }).then(function (r) {
          f.msg = r.ok ? 'Mode: ' + (MODE_WORDS[r.mode] || r.mode) + '.' : 'Refused: ' + r.reason;
          if (r.ok) f.confirm = '';
        }, function (e) { f.msg = 'Refused: ' + errText(e); })['finally'](function () {
          f.busy = false; self.rcLoadModes();
          window.dispatchEvent(new CustomEvent('pl-limits-saved'));  // Play reloads its window (its mode pill)
        });
      },
      // ----- formatting: colour is never the only cue (every coloured number has its sign) -----
      signed: function (x) { return x == null || isNaN(x) ? '—' : (x > 0.004 ? '+' : x < -0.004 ? '−' : '') + '$' + Math.abs(x).toFixed(2); },
      money: function (x) { return x == null || isNaN(x) ? '—' : (x > 0 && x < 0.01 ? '$' + x.toFixed(4) : usd(x)); },
      tone: function (x) { return x == null || Math.abs(x) < 0.005 ? '' : x > 0 ? 'pl-up' : 'pl-down'; },
      plural: function (n, w) { return n + ' ' + w + (n === 1 ? '' : 's'); },
      tradesLine: function (x) { return this.plural(x.trades, 'trade') + ' · ' + x.wins + ' won'; }
    };
  };

  // PC-only Phone access card (in Play's settings sheet; it was on the retired Modes tab): the server answers
  // /api/access only to the paired PC browser (403 elsewhere), so the card shows only on the PC. It also signs every
  // PC browser out (/api/access/pc-signout). No polling: it loads each
  // time the settings sheet opens (the pl-settings window event).
  window.phoneAccessCard = function () {
    return {
      a: null, tried: false, refused: '', pin: '', pin2: '', msg: '', busy: false, copied: false,
      load: function () {
        var self = this;
        api.get('/api/access').then(function (r) { self.a = r; self.refused = ''; },
          function (e) { self.a = null; self.refused = errText(e); })['finally'](function () { self.tried = true; });
      },
      valid: function () { return /^[0-9]{6,8}$/.test(this.pin) && this.pin === this.pin2; },
      post: function (path, body, after) {
        var self = this;
        self.busy = true; self.msg = '';
        api.post(path, body).then(function (r) { self.a = r; self.msg = r.reason || ''; if (after) after(); },
          function (e) { self.msg = errText(e); })['finally'](function () { self.busy = false; });
      },
      save: function () { var self = this; if (!self.valid()) return; self.post('/api/access/pin', { pin: self.pin, pin_again: self.pin2 }, function () { self.pin = ''; self.pin2 = ''; }); },
      signOut: function () { if (window.confirm('Sign out every phone? They will need the PIN again.')) this.post('/api/access/signout'); },
      clear: function () { if (window.confirm('Turn phone access off? Phones are refused until a PIN is set again.')) this.post('/api/access/clear'); },
      // Every paired PC browser, this one too, is signed out (a new PC secret); this page then shows "Pair this browser".
      signOutPc: function () {
        var self = this;
        if (!window.confirm('Sign out every PC browser, this one too? Pair again with: python -m btc_signal_engine.ops pair-pc')) return;
        self.busy = true; self.msg = '';
        api.post('/api/access/pc-signout').then(function () { window.location.reload(); },
          function (e) { self.msg = errText(e); self.busy = false; });
      },
      copy: function () {
        var self = this, url = self.a && self.a.lan_url;
        if (!url || !navigator.clipboard) return;
        navigator.clipboard.writeText(url).then(function () { self.copied = true; setTimeout(function () { self.copied = false; }, 2000); }, function () {});
      }
    };
  };

  // Trade limits card (in Play's settings sheet): the phone's per-trade and per-day limits (GET/POST /api/play/limits,
  // shared by every phone strategy and perps; null = no limit). The day limit is one combined total (KD7): event
  // contract stakes plus perps committed collateral; the per-window limit does not apply to perps. Loaded each time the sheet opens (no polling). Lowering saves
  // at once; raising or removing a limit comes back confirm_required with the exact phrase to type, then a second
  // Save sends it. "Used today" is the open Play tab's now.limits.day_used (the parent's `v`).
  window.playLimitsCard = function () {
    function money(x) { return x == null ? 'No limit' : usd(x); }
    function side(o, f) { return o && o.unreadable ? 'unreadable' : money(o ? o[f] : null); }
    return {
      lm: null, lmTried: false, lmErr: '', lmBusy: false, lmMsg: '', lmMsgCls: '', lmNeed: null, lmPhrase: '',
      lmNoTrade: false, lmTrade: '', lmNoDay: false, lmDay: '', bandMsg: '', bandMsgCls: '',
      lmLoad: function () {
        var self = this;
        self.lmMsg = ''; self.lmNeed = null; self.lmPhrase = '';
        api.get('/api/play/limits').then(function (r) { self.lmFill(r); self.lmErr = ''; },
          function (e) { self.lm = null; self.lmErr = errText(e); })['finally'](function () { self.lmTried = true; });
      },
      // The form starts at the saved values; an unreadable setting starts blank (nothing is assumed).
      lmFill: function (r) {
        this.lm = r;
        var ok = r && r.readable;
        this.lmNoTrade = !!ok && r.per_trade == null; this.lmTrade = ok && r.per_trade != null ? String(r.per_trade) : '';
        this.lmNoDay = !!ok && r.per_day == null; this.lmDay = ok && r.per_day != null ? String(r.per_day) : '';
      },
      // Market protection (GET /api/play/limits .market_band; POST /api/play/market_band {cents}): cents over the ask
      // a Market buy may fill at, or null = up to 99c. Unreadable: no chip is shown as chosen.
      bandChoices: function () { var b = this.lm && this.lm.market_band; return b && b.choices ? b.choices.concat([null]) : []; },
      bandIs: function (x) { var b = this.lm && this.lm.market_band; return !!b && b.readable && b.cents === x; },
      // Would choosing x let a Market buy fill at a worse price than now (null = up to 99c)? A phone may only narrow.
      bandWider: function (x) {
        var b = this.lm && this.lm.market_band;
        if (!b || !b.readable) return true;
        return b.cents != null && (x == null || x > b.cents);
      },
      bandSave: function (x) {
        var self = this;
        self.lmBusy = true; self.bandMsg = '';
        api.post('/api/play/market_band', { cents: x }).then(function (r) {
          if (r && r.ok) {
            self.lm = Object.assign({}, self.lm, { market_band: r.market_band });
            self.bandMsg = r.changed ? 'Saved: Market buys fill ' + (x == null ? 'up to 99¢' : 'at most ' + x + '¢ over the ask') + '.' : 'No change.';
            self.bandMsgCls = 'green';
            window.dispatchEvent(new CustomEvent('pl-limits-saved'));
            return;
          }
          self.bandMsg = 'Not saved: ' + ((r && r.reason) || 'no reason text from the server (code ' + ((r && r.code) || 'none') + ').');
          self.bandMsgCls = 'red';
        }, function (e) { self.bandMsg = 'Not saved: ' + errDetail(e); self.bandMsgCls = 'red'; })['finally'](function () { self.lmBusy = false; });
      },
      lmNow: function (f) { return this.lm && this.lm.readable ? money(this.lm[f]) : 'Unreadable'; },
      lmUsed: function (v) {
        var lim = v && v.limits;
        if (!v) return '';
        if (!lim || lim.day_used == null) return 'Used today (one total across both venues and perps): not known (the server sent no day total).';
        return 'Used today (one total across both venues and perps): ' + usd(lim.day_used) + (lim.per_day != null ? ' of ' + usd(lim.per_day) : '');
      },
      lmValue: function (no, raw, name) {
        if (no) return { v: null };
        var t = String(raw == null ? '' : raw).trim().replace(/^\$/, '').replace(/,/g, ''), n = Number(t);
        if (!t || !isFinite(n) || n <= 0) return { err: 'Enter a ' + name + ' amount above $0, or tick No limit.' };
        return { v: Math.round(n * 100) / 100 };
      },
      lmEdited: function () { this.lmNeed = null; this.lmPhrase = ''; this.lmMsg = ''; },
      lmSave: function (confirmed) {
        var self = this, a = self.lmValue(self.lmNoTrade, self.lmTrade, 'per-trade'), b = self.lmValue(self.lmNoDay, self.lmDay, 'per-day');
        if (a.err || b.err) { self.lmMsg = a.err || b.err; self.lmMsgCls = 'red'; return; }
        var body = { per_trade: a.v, per_day: b.v };
        if (confirmed) {
          if (!self.lmNeed || self.lmPhrase !== self.lmNeed.confirm_phrase) return;
          body.confirm = self.lmPhrase;
        }
        self.lmBusy = true; self.lmMsg = '';
        api.post('/api/play/limits', body).then(function (r) {
          if (r && r.ok) {
            self.lmNeed = null; self.lmPhrase = ''; self.lmFill(r);
            self.lmMsg = r.changed ? 'Saved: per trade ' + money(r.per_trade) + ', per day ' + money(r.per_day) + '. The next order uses them.'
              : 'No change: these are already your limits.';
            self.lmMsgCls = 'green';
            window.dispatchEvent(new CustomEvent('pl-limits-saved'));
            return;
          }
          if (r && r.code === 'confirm_required') {
            self.lmNeed = r; self.lmPhrase = '';
            if (!r.confirm_phrase) { self.lmMsg = 'The server asked for a confirmation but sent no phrase to type: ' + (r.reason || '(no reason)'); self.lmMsgCls = 'red'; }
            return;
          }
          self.lmMsg = (r && r.reason ? 'Not saved: ' + r.reason : 'Not saved (code ' + ((r && r.code) || 'none') + ') with no reason text from the server.');
          self.lmMsgCls = 'red';
        }, function (e) { self.lmMsg = 'Not saved: ' + errDetail(e); self.lmMsgCls = 'red'; })['finally'](function () { self.lmBusy = false; });
      },
      // The last few changes (trading_audit "limits_changed"): when, raised or lowered, old -> new.
      lmHistory: function () {
        var h = (this.lm && this.lm.history) || [];
        return h.slice(0, 4).map(function (x, i) {
          var d = x.detail, when = x.ts != null ? new Date(x.ts).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'time unknown';
          if (!d || !d.new) return { k: i, when: when, word: '', text: x.reason || 'No details recorded.' };
          var up = Array.isArray(d.raised) && d.raised.length > 0;
          return { k: i, when: when, word: up ? 'Raised' : 'Lowered', up: up,
            text: 'Per trade ' + side(d.old, 'per_trade') + ' → ' + money(d.new.per_trade) + ' · per day ' + side(d.old, 'per_day') + ' → ' + money(d.new.per_day) };
        });
      }
    };
  };
})();
