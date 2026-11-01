/* Test data and fakes for tests/web/perps_harness.html (never loaded by the app).
   Replaces window.fetch and window.EventSource before the app's scripts load. GET /api/perps/view and the stream
   answer with the chosen state's View (built here, test data only); the stream first replays MARKS earlier mark prices
   so the chart has a line, then pings. POST /api/perps/prepare, /prepare_exit and /prepare_trigger answer with the
   state's ticket (they move no money in the app either); GET /api/play/now answers READ (Play's lights); every other non-GET request is refused with code
   "blocked_in_harness" and logged in window.__blocked. GET /api/access answers 403 (a phone) unless ?pc=1.
   After Alpine starts, the state's setup(comp) fills the inputs (and may start a slide) the way a user would. */
(function () {
  'use strict';
  var q = new URLSearchParams(location.search), NAME = q.get('state') || 'flat';
  var NOW = Date.now(), MARK = 7.8360, MARKS = 90;
  try {
    ['perps.collateral', 'perps.fill', 'perps.limit', 'perps.band'].forEach(function (k) { localStorage.removeItem(k); });
    localStorage.setItem('perps.open', '1');
  } catch (e) { /* storage blocked */ }

  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function base() {
    return {
      server_ms: NOW, enabled: true, enabled_reason: null,
      mode: 'perp_demo', modes_available: ['perp_demo', 'perp_prod', 'off'], ready: true, ready_reason: null, killed: false,
      exchange: { trading_active: true, reason: null, maintenance_note: 'Kalshi pauses perps trading every Thursday 08:00–10:00 ET for maintenance.' },
      market: { ticker: 'KXBTCPERP1', tick: 0.0001, contract_btc: 0.0001, count_step: 1, taker_fee: 0.0012, maker_fee: 0, max_leverage_long: 6.85, max_leverage_short: 6.38 },
      quote: { bid: 7.8352, ask: 7.8368, bid_size: 420, ask_size: 380, last: 7.8360, mark: MARK, reference: 7.8358, age_ms: 120 }, quote_reason: null,
      funding: { rate: 0.0001, next_ms: NOW + 23 * 60000 + 14000 }, funding_reason: null,
      balance: { equity: 40.12, available: 40.12, at_ms: NOW - 2000 }, balance_reason: null,
      position: null,
      trigger: { state: 'none', stop_loss: null, take_profit: null, updated_time: null, reason: null },
      unknown_order: false, foreign: [], banners: [],
      can: { entry: true, entry_reason: null, exit: true, exit_reason: null, trigger: true, trigger_reason: null },
      outcome: null,
      limits: { per_trade: null, per_day: 20, committed_today: 5, text: 'Per day $20.00 (one total across both venues and perps): $5.00 used today. No per-trade limit.' },
      settings: { band: 0.005, amber: 0.10, red: 0.05 },
      warnings_note: 'Warnings show only while this screen is open.'
    };
  }
  function position(mark, over) {
    var p = { id: 'pp_1', direction: 'long', size: 19, avg_entry: 7.8120, state: 'open', collateral: 30, leverage: 5,
      mark: mark, unrealized_pnl: Math.round(19 * (mark - 7.8120) * 10000) / 10000, realized_pnl: 0, fees: 0.1781,
      funding_total: -0.0149, liquidation_price: 6.4512, liquidation_source: 'kalshi',
      distance: (mark - 6.4512) / mark, band: 'ok', opened_ms: NOW - 41 * 60000 };
    return Object.assign(p, over || {});
  }
  function quoteAt(m) { return { bid: m - 0.0008, ask: m + 0.0008, bid_size: 400, ask_size: 400, last: m, mark: m, reference: m, age_ms: 150 }; }
  var BOUND_FLAT = { position_sign: 0, position_size: 0, trigger_updated_time: null };
  var BOUND_POS = { position_sign: 1, position_size: 19, trigger_updated_time: 1730270000000 };
  var AT_RISK = 'Your whole perps balance ($40.12) backs this position: a big enough move can lose all of it.';
  var ENTRY_TICKET = { action: 'entry', side: 'long', count: 19, btc: 0.0019, notional: 148.88, protective_price: 7.8759,
    protective_text: 'at most $7.8759', time_in_force: 'immediate or cancel', fee_est: 0.1787, worst_case: 40.4774,
    at_risk_balance: 40.12, at_risk_text: AT_RISK, liquidation_est: 6.4570, liquidation_label: 'estimate',
    funding: { rate: 0.0001, next_ms: NOW + 23 * 60000, est_amount: -0.0149 }, collateral: 30, leverage: 5,
    stop_loss: 7.5000, take_profit: 8.2000, new_avg_entry: null, new_size: null, new_liquidation_est: null,
    remaining: null, est_realized_pnl: null, chips: [], bound: BOUND_FLAT, expires_ms: NOW + 60000 };
  var ADD_TICKET = Object.assign(clone(ENTRY_TICKET), { action: 'add', count: 12, btc: 0.0012, notional: 94.03, collateral: 20,
    leverage: 4.7, worst_case: 40.3457, stop_loss: null, take_profit: null, new_avg_entry: 7.8213, new_size: 31,
    new_liquidation_est: 6.6034, chips: ['All or nothing', 'Band 1.0%'], protective_price: 7.9144, protective_text: 'at most $7.9144',
    time_in_force: 'fill or kill', bound: BOUND_POS });
  var REDUCE_TICKET = { action: 'reduce', side: 'short', count: 9, btc: 0.0009, notional: 70.17, protective_price: 7.7960,
    protective_text: 'at least $7.7960', time_in_force: 'immediate or cancel, reduce only', fee_est: 0.0843, worst_case: null,
    liquidation_est: null, liquidation_label: 'estimate', funding: null, collateral: null, leverage: null,
    stop_loss: null, take_profit: null, new_avg_entry: null, new_size: null, new_liquidation_est: null,
    remaining: 10, est_realized_pnl: 0.1317, chips: [], bound: BOUND_POS, expires_ms: NOW + 60000 };
  // Play's read for Kalshi 15m (only the fields the Perps screen uses): the call ENTER UP (long), 1m and 15m lean UP,
  // 5m quiet, crowd UP.
  var READ = { call: { mid_up: 0.61, fair_up: 0.58, action: 'ENTER UP', headline: 'ENTER UP',
    reason: 'The market prices UP at 61% (≥ 55%).', agree: { n: 5, of: 6, side: 'UP' } }, window: { end: NOW + 9 * 60000 }, btc: { strike: 78300 },
    signals: [{ key: 'market', name: 'Market mid', lean: 'UP', value: 'UP 61%' }, { key: 'model', name: 'Our model', lean: 'UP', value: 'UP 58%' },
      { key: 'pressure_60', name: '1m pressure', lean: 'UP', value: 'UP' }, { key: 'pressure_300', name: '5m pressure', lean: null, value: 'SKIP' },
      { key: 'pressure_900', name: '15m pressure', lean: 'UP', value: 'UP' },
      { key: 'crowd', name: 'Crowd, last minute', lean: 'UP', value: 'more buying Up', up_usd: 18200, down_usd: 6400, note: 'Descriptive only.' }] };
  var RECORD = { env: 'perp_demo', reason: null, wins: 2, losses: 1, pnl: 0.4212, rows: [
    { id: 'pp_3', direction: 'long', close_reason: 'take_profit', realized_pnl: 0.6446, closed_ms: NOW - 3600000 },
    { id: 'pp_2', direction: 'short', close_reason: 'stop', realized_pnl: -0.3021, closed_ms: NOW - 7200000 },
    { id: 'pp_1', direction: 'long', close_reason: 'user_close', realized_pnl: 0.0787, closed_ms: NOW - 9000000 }] };
  var TRIGGER_TICKET = { action: 'trigger', stop_loss: 7.6000, take_profit: 8.1000, chips: [], bound: BOUND_POS, expires_ms: NOW + 60000 };

  // Each state: view(v) edits the base View; ticket = what the prepare routes answer; setup(c) acts like the user.
  var STATES = {
    flat: {},
    entry_ticket: { ticket: ENTRY_TICKET, setup: function (c) { c.pickSide('long'); c.collInput = '30'; c.setLev(5); c.slInput = '7.5000'; c.tpInput = '8.2000'; c.edit(); } },
    entry_dragging: { ticket: ENTRY_TICKET, setup: function (c) { c.pickSide('long'); c.collInput = '30'; c.setLev(5); c.slInput = '7.5000'; c.tpInput = '8.2000'; c.edit(); },
      later: function (c) { c.slideStart({ clientX: 0, pointerId: 1, target: { setPointerCapture: function () {} } }); setTimeout(function () { c.slideX = Math.round(c.slideMax() * 0.6); }, 200); } },
    stop_wrong_side: { ticket: ENTRY_TICKET, setup: function (c) { c.pickSide('short'); c.collInput = '30'; c.setLev(3); c.slInput = '7.7000'; c.edit(); } },
    refused: { refuse: { ok: false, code: 'over_day_limit', reason: 'This would commit $30.00 today; your per-day limit is $20.00 across both venues and perps and $5.00 is used.' },
      setup: function (c) { c.pickSide('long'); c.collInput = '30'; c.setLev(5); c.edit(); } },
    open_protected: { view: function (v) { v.position = position(MARK); v.trigger = { state: 'active', stop_loss: 7.5000, take_profit: 8.2000, updated_time: 1730270000000, reason: null }; } },
    open_unprotected: { view: function (v) {
      v.position = position(MARK); v.trigger = { state: 'failed', stop_loss: null, take_profit: null, updated_time: null, reason: 'Kalshi refused the stop: stop_loss_past_liquidation' };
      v.banners = [{ code: 'no_stop', severity: 'red', text: 'Position open, no stop on Kalshi: setting it failed. Close it, or set a stop.' }]; } },
    reduce_ticket: { ticket: REDUCE_TICKET, view: function (v) { v.position = position(MARK); }, setup: function (c) { c.setAct('reduce'); } },
    add_ticket: { ticket: ADD_TICKET, view: function (v) { v.position = position(MARK); }, setup: function (c) { c.setAct('add'); c.collInput = '20'; c.setLev(4.7); c.setFill('all_or_nothing'); c.setBand(0.01); } },
    trigger_ticket: { ticket: TRIGGER_TICKET, view: function (v) { v.position = position(MARK); v.trigger = { state: 'active', stop_loss: 7.5000, take_profit: 8.2000, updated_time: 1730270000000, reason: null }; },
      setup: function (c) { c.setAct('protect'); c.trigSl = '7.6000'; c.trigTp = '8.1000'; c.edit(); } },
    near_amber: { mark: 6.9500, view: function (v) {
      v.quote = quoteAt(6.95); v.position = position(6.95, { band: 'amber' });
      v.banners = [{ code: 'near_liquidation', severity: 'amber', text: 'The mark is 7.2% from your liquidation price.' }]; } },
    near_red: { mark: 6.7000, view: function (v) {
      v.quote = quoteAt(6.70); v.position = position(6.70, { band: 'red' });
      v.banners = [{ code: 'near_liquidation', severity: 'red', text: 'The mark is 3.7% from your liquidation price.' }]; } },
    paused: { view: function (v) {
      v.position = position(MARK); v.exchange = { trading_active: false, reason: 'Kalshi has paused perps trading (weekly maintenance).', maintenance_note: null };
      v.can = { entry: false, entry_reason: 'Kalshi has paused perps trading (weekly maintenance).', exit: false, exit_reason: 'Kalshi has paused perps trading (weekly maintenance).', trigger: false, trigger_reason: 'Kalshi has paused perps trading (weekly maintenance).' };
      v.banners = [{ code: 'paused', severity: 'amber', text: 'Kalshi has paused perps trading (weekly maintenance). Trading resumes after Settleview re-checks your account.' }]; } },
    unknown_order: { view: function (v) {
      v.unknown_order = true; v.can.entry = false; v.can.entry_reason = 'Checking whether your order went through';
      v.banners = [{ code: 'unknown_order', severity: 'amber', text: 'Checking whether your order went through. New entries wait until it is known.' }]; } },
    diverged: { view: function (v) {
      v.position = position(MARK); v.can.entry = false; v.can.entry_reason = 'Kalshi shows a different position than Settleview recorded; re-checking.';
      v.banners = [{ code: 'diverged', severity: 'red', text: 'Kalshi shows 21 contracts; Settleview recorded 19. Re-checking the account; exits still work.' }]; } },
    two_states: { mark: 6.95, view: function (v) {
      v.quote = quoteAt(6.95); v.position = position(6.95, { band: 'amber' });
      v.trigger = { state: 'failed', stop_loss: null, take_profit: null, updated_time: null, reason: 'Kalshi did not answer the trigger write.' };
      v.banners = [{ code: 'near_liquidation', severity: 'amber', text: 'The mark is 7.2% from your liquidation price.' },
        { code: 'no_stop', severity: 'red', text: 'Position open, no stop on Kalshi: setting it failed. Close it, or set a stop.' }]; } },
    foreign: { view: function (v) {
      v.can.entry = false; v.can.entry_reason = 'A position Settleview did not open is in your perps account: adopt it or close it first.';
      v.foreign = [{ subaccount: 0, kind: 'foreign', size: -7, avg_entry: 7.9012, unrealized_pnl: 0.0456 },
        { subaccount: 64, kind: 'app_isolated', size: 3, avg_entry: 7.7001, unrealized_pnl: 0.0408 }]; } },
    outcome: { view: function (v) { v.outcome = { kind: 'stop_fired', text: 'Your stop loss closed 19 contracts at $7.4995 average: −$5.98 realized, after $0.36 fees and −$0.01 funding.', at_ms: NOW - 4000 }; } },
    stale: { streamDies: true, view: function (v) { v.position = position(MARK); } },
    disabled: { view: function (v) { v.enabled = false; v.enabled_reason = 'Perps are switched off'; } },
    tp_sheet: { ticket: TRIGGER_TICKET, view: function (v) { v.position = position(MARK); }, setup: function (c) { c.tpOpen(); c.tpOn = true; c.slOn = true; c.tpPct = 1.5; c.slPct = 1; } },
    call_prefill: { ticket: ENTRY_TICKET, setup: function (c) { c.$el.querySelector('.pl-hist').open = true; } },
    lev_too_low: { ticket: ENTRY_TICKET, setup: function (c) { c.pickColl(5); c.setLev(1); } },  // $5 at 1x: under one contract
    small_cash: { ticket: ENTRY_TICKET, view: function (v) { v.mode = 'perp_prod'; v.balance = { equity: 4.97, available: 4.97, at_ms: NOW - 2000 }; } },
    mode_off: { view: function (v) { v.mode = 'off'; v.ready = false; v.ready_reason = 'Perps trading is off.'; v.quote = null; v.quote_reason = 'Perps trading is off: no Kalshi perps price is streamed.'; v.balance = null; v.balance_reason = 'not read yet'; } },
    settings: { view: function (v) { v.mode = 'perp_prod'; }, setup: function (c) { c.openSheet(); c.modeForm.mode = 'perp_prod'; } },
    real_entry: { ticket: ENTRY_TICKET, view: function (v) { v.mode = 'perp_prod'; }, setup: function (c) { c.pickSide('long'); c.pickColl(5); c.setLev(3); } },
    // Ordering and preview checks (perps.js merge / shown). late_view: the GET answers after the stream's newer frames
    // with an older View that differs; it must be dropped (no "OLD VIEW" banner, entries stay on).
    late_view: { getDelayMs: 600, oldView: function (v) {
      v.can.entry = false; v.can.entry_reason = 'OLD VIEW: entries off';
      v.banners = [{ code: 'paused', severity: 'amber', text: 'OLD VIEW: this banner must never show' }]; } },
    // preview_resized: a reduce preview is on screen, then the stream reports 25 contracts; the preview (bound to 19)
    // must leave the screen. preview_stale: the same preview, then the stream dies; it must leave the screen.
    preview_resized: { ticket: REDUCE_TICKET, view: function (v) { v.position = position(MARK); }, setup: function (c) { c.setAct('reduce'); },
      later: function () { window.__es.emit('view', { server_ms: Date.now(), position: position(MARK, { size: 25 }) }); } },
    preview_stale: { ticket: REDUCE_TICKET, noReconnect: true, view: function (v) { v.position = position(MARK); }, setup: function (c) { c.setAct('reduce'); },
      later: function () { var es = window.__es; es.readyState = 2; if (es.onerror) es.onerror({}); } }
  };
  var S = STATES[NAME];
  if (!S) throw new Error('Unknown harness state "' + NAME + '"; known: ' + Object.keys(STATES).join(', '));
  window.__harnessStates = Object.keys(STATES);
  var VIEW = base();
  if (S.view) S.view(VIEW);

  window.__blocked = [];
  function reply(body, status) {
    return Promise.resolve({ ok: (status || 200) < 400, status: status || 200, text: function () { return Promise.resolve(JSON.stringify(body)); } });
  }
  window.fetch = function (url, opts) {
    var method = (opts && opts.method) || 'GET', path = String(url).split('?')[0];
    if (method === 'GET' && path === '/api/perps/view') {
      var gv = Object.assign(clone(VIEW), { server_ms: NOW - MARKS * 5000 });  // older than the replayed marks
      if (!S.getDelayMs) return reply(gv);
      if (S.oldView) S.oldView(gv);
      return new Promise(function (res) { setTimeout(function () { res(reply(gv)); }, S.getDelayMs); });
    }
    if (method === 'GET' && path === '/api/play/now') return reply(READ);
    if (method === 'GET' && path === '/api/perps/record') return reply(RECORD);  // Play's read: the call and the lights
    if (method === 'GET' && path === '/api/access') return q.get('pc') === '1' ? reply({ pin_set: false }) : reply({ detail: 'PC only' }, 403);
    if (method === 'POST' && /^\/api\/perps\/prepare(_exit|_trigger)?$/.test(path)) {
      if (S.refuse) return reply(S.refuse);
      if (!S.ticket) return reply({ ok: false, code: 'no_fixture', reason: 'The harness has no ticket for this state.' });
      return reply({ ok: true, token: 'tok_' + Math.random().toString(36).slice(2), ticket: clone(S.ticket), expires_ms: S.ticket.expires_ms });
    }
    window.__blocked.push(method + ' ' + path);
    return reply({ ok: false, code: 'blocked_in_harness', reason: 'Blocked in the harness: nothing is sent.' });
  };

  var opened = 0;
  function FakeES(url) {
    var self = this;
    self.url = url; self.readyState = 0; self.L = {};
    opened++;
    if ((S.streamDies || S.noReconnect) && opened > 1) return;  // the reconnect never answers: the screen stays stale
    window.__es = self;
    var marks = [];
    var end = S.mark || MARK;
    for (var i = 0; i < MARKS; i++) {
      var f = i / (MARKS - 1);
      marks.push(Math.round((MARK + (end - MARK) * f + 0.006 * Math.sin(i / 6) + 0.003 * Math.sin(i / 2.3)) * 10000) / 10000);
    }
    marks[MARKS - 1] = end;
    setTimeout(function () {
      self.readyState = 1;
      marks.forEach(function (m, i) {
        var t = NOW - (MARKS - 1 - i) * 5000;
        var d = i === 0 ? clone(VIEW) : { server_ms: t, quote: quoteAt(m) };
        if (i === 0) { d.server_ms = t; d.quote = quoteAt(m); }
        if (i === MARKS - 1) d = Object.assign(clone(VIEW), { server_ms: NOW });
        self.emit('view', d);
      });
      if (S.streamDies) setTimeout(function () { self.readyState = 2; if (self.onerror) self.onerror({}); }, 50);
    }, 30);
  }
  FakeES.prototype.addEventListener = function (n, fn) { (this.L[n] = this.L[n] || []).push(fn); };
  FakeES.prototype.emit = function (n, d) { (this.L[n] || []).forEach(function (fn) { fn({ data: JSON.stringify(d) }); }); };
  FakeES.prototype.close = function () { this.readyState = 2; };
  window.EventSource = FakeES;

  document.addEventListener('alpine:initialized', function () {
    var el = document.getElementById('panel-perps'), c = window.Alpine.$data(el);
    setTimeout(function () {
      if (S.setup) S.setup(c);
      if (S.later) setTimeout(function () {
        // What the ticket card showed just before `later` acted (read by a DOM dump: data-ticket-before on <body>).
        document.body.setAttribute('data-ticket-before', document.querySelector('.pv-ticket .pl-rows') ? 'shown' : 'none');
        S.later(c);
      }, 900);
    }, 200);
  });
})();
