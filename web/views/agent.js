/* Agents: the fleet, one agent's screen, launching, talking to and stopping agents
   (docs/plans/2026-10-30-1402-feat-agent-mode-mcp-plan.md U9; docs/plans/2026-10-31-0010-feat-multi-agent-plan.md U6
   and U6b; the approved fleet wireframe; API: docs/api-agents.md).
   Data (agent_routes.py; the routes exist only with BTCFLOW_AGENT_ENABLED, so GET /api/agent/status answering 404
   means agent mode is off):
   - GET /api/agent/status: availability, key status (never the key), models and rates, venues {key: {label,
     strategy, mode, rules, also_traded_by, next_agent}}, prompt versions, the shared daily total, global limits,
     server_ms. Read on start, on entering the tab, after every action.
   - GET /api/agent/sessions (every live session's row and the fleet summary) and GET /api/agent/agents (every agent
     with its last run): read every FLEET_MS while the page is visible, on every tab (the tab bar's accessory and
     Play's "agents here" line read them), and after every action.
   - One agent's screen shows its live run, else its last: GET /api/agent/sessions/{id} (view, row, positions) and its
     own stream EventSource('/api/agent/sessions/{id}/stream'): AG-UI events (agent_feed.py), one JSON object per
     `data:` frame, `type` names
     it. STATE_SNAPSHOT {session, positions, prompts} first, then STATE_DELTA (RFC 6902 JSON Patch). One run per wake
     (runId "<session id>:<wake #>"): RUN_STARTED (trigger, server_ms), REASONING_* / TEXT_MESSAGE_* deltas,
     TOOL_CALL_START / ARGS / END / RESULT, then RUN_FINISHED (result: status, est_usd, trigger, ...) or RUN_ERROR
     (runId, code, message). CUSTOM {name, value}: heartbeat ({server_ms}, every 15 s on a quiet stream), decision (a
     decision row), session_state, note (queued -> delivered), tool_call (an external agent's call), feed_trimmed
     (this slow client lost events: reconnect for a fresh snapshot). Liveness: some event arrives at least every 15 s
     (the heartbeat when nothing else happens, session or not); quiet past STALE_MS = lost, closed, reopened.
   - GET /api/agent/sessions/{id}/journal: on every (re)connect, the session's rows after the last one seen fill what
     the stream missed (wake rows rebuild finished runs from their ordered text / tool `segments`; decision, note and
     tool rows), deduplicated by run id / row id.
   - All activity spans every run of the agent: GET /api/agent/agents/{agent_id}/journal (newest first, paged back by
     before_id, filtered by kinds, with run dividers from `runs`); the fleet's audit is GET /api/agent/journal/all.
   The selected agent's stream stays open while the page is visible; it closes when the page is hidden. The feed is plain DOM built here (not Alpine): text deltas are queued and appended
   to text nodes once per animation frame; the last MAX_RUNS runs are kept. Nothing on this screen is made up: every
   figure is a field from the API or arithmetic on fields; a missing one shows "—" or says so. */
(function () {
  'use strict';
  var B = window.BTC, api = B.api, fmt = B.fmt;

  var OPEN_STATES = ['sending', 'unknown', 'open', 'stopping', 'settling'];  // settling: window closed, result pending
  var LIVE = ['running', 'paused'];
  var MAX_RUNS = 50, MAX_DECISIONS = 300, STALE_MS = 45000, RECONNECT_MS = 3000, KILL_POLL_MS = 5000;
  var FLEET_MS = 4000;  // the fleet list and summary (GET /api/agent/sessions, /api/agent/agents)
  var END_WORDS = {
    stopped: 'stopped by you', time: 'reached its end time', spend_cap: 'reached its API-spend cap',
    agent: 'the agent ended it', invalid_key: 'the API key was found invalid', restart: 'the app restarted'
  };
  // a run divider's "ended by …" (All activity): the same reasons, said after "ended"
  var ENDED_BY = {
    stopped: 'stopped by you', time: 'ended at its end time', spend_cap: 'ended at its API cap',
    agent: 'ended by the agent', invalid_key: 'ended: invalid API key', restart: 'ended by restart'
  };
  var ACTION_WORDS = { ENTRY: 'Entry', EXIT: 'Exit', REDUCE: 'Reduce', HEDGE: 'Hedge', HOLD: 'Hold', NO_TRADE: 'No trade', UNRECORDED: 'No decision recorded' };

  function load(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function save(key, v) { try { if (v == null) window.localStorage.removeItem(key); else window.localStorage.setItem(key, String(v)); } catch (e) { /* storage blocked */ } }
  function ok(x) { return typeof x === 'number' && isFinite(x); }
  var usd = fmt.usd, signedUsd = fmt.signedUsd, pct = fmt.pct, cents = fmt.cents;  // app.js: the shared formatters
  // API spend: a wake on a cheap model costs a fraction of a cent, which "$0.00" would hide.
  function spend(x) { return ok(x) && x > 0 && x < 0.01 ? '$' + x.toFixed(4) : usd(x); }
  function errText(e) { return (e && e.message) || String(e); }
  function refusal(r) { return (r && (r.reason || r.detail)) || 'The server refused without a reason.'; }
  function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  function json(x) { try { return JSON.stringify(x); } catch (e) { return String(x); } }
  function clone(x) { return x == null ? x : JSON.parse(JSON.stringify(x)); }
  function left(sec) {
    if (!ok(sec)) return '—';
    sec = Math.max(0, Math.floor(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? h + 'h ' + String(m).padStart(2, '0') + 'm' : m + ':' + String(s).padStart(2, '0');
  }

  // ---------- RFC 6902 (the subset agent_feed.json_patch sends: add / remove / replace) ----------
  function unptr(t) { return t.replace(/~1/g, '/').replace(/~0/g, '~'); }
  function applyPatch(doc, ops) {
    var root = { v: doc };
    ops.forEach(function (op) {
      var parts = op.path === '' ? [] : op.path.split('/').slice(1).map(unptr);
      var keys = ['v'].concat(parts), parent = root;
      for (var i = 0; i < keys.length - 1; i++) {
        parent = parent[keys[i]];
        if (parent == null || typeof parent !== 'object') throw new Error('patch path not found: ' + op.path);
      }
      var k = keys[keys.length - 1];
      if (Array.isArray(parent) && k === '-') k = parent.length;
      if (op.op === 'remove') { if (Array.isArray(parent)) parent.splice(+k, 1); else delete parent[k]; }
      else if (op.op === 'add' && Array.isArray(parent)) parent.splice(+k, 0, op.value);
      else if (op.op === 'add' || op.op === 'replace') parent[k] = op.value;
      else throw new Error('patch op not supported: ' + op.op);
    });
    return root.v;
  }

  // ---------- the feed: plain DOM, one card per run (wake), newest first ----------
  var runs = {}, order = [], decByRun = {}, pend = [], raf = 0, ext = null, feedSid = null;

  function h(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }
  function feedEl() { return document.getElementById('ag-feed'); }
  function store() { return window.Alpine ? Alpine.store('agent') : null; }
  function runOf(id) { var m = /^(.*:\d+)(?::[mt]\d+.*)?$/.exec(id || ''); return m ? m[1] : null; }

  function resetFeed(sid) {
    runs = {}; order = []; decByRun = {}; pend = []; ext = null; feedSid = sid;
    var f = feedEl();
    if (f) f.textContent = '';
    var s = store();
    if (s) { s.runCount = 0; s.lastJid = 0; s.notes = []; s.decision = null; s.endText = ''; s.decisions = []; s.wakes = []; }
  }
  // The run's one-line summary for the activity list (the full card stays in #ag-feed): kept in the store.
  function summary(r, patch) { var s = store(); if (s && r.kind !== 'rule') s.wakeUp(r.id, patch); }
  function setTime(r, ms) { r.timeEl.textContent = fmt.time(ms); r.ts = ms; summary(r, { ts: ms }); }

  function makeRun(id, title) {
    var r = {
      id: id, finished: false, el: h('article', 'ag-run is-live'), msgs: {}, tools: {}, thinkText: '', textCount: 0,
      kind: id.indexOf('rule:') === 0 ? 'rule' : id.indexOf('ext:') === 0 ? 'ext' : 'wake'
    };
    r.el.setAttribute('aria-label', title);
    r.el.setAttribute('data-run', id);
    var head = h('header', 'ag-run-h');
    r.titleEl = h('b', null, title);
    r.trigEl = h('span', 'ag-trig');
    r.stEl = h('span', 'ag-run-st', 'working…');
    var s0 = store();
    r.ts = Date.now() + ((s0 && s0.skewMs) || 0);
    r.timeEl = h('time', 'num', fmt.time(r.ts));
    head.appendChild(r.titleEl); head.appendChild(r.trigEl); head.appendChild(r.stEl); head.appendChild(r.timeEl);
    r.el.appendChild(head);
    r.think = h('details', 'ag-think');
    r.think.open = true;
    r.thinkSum = h('summary', null, 'Thinking…');
    r.thinkBody = h('div', 'ag-think-body');
    r.stick = true;
    r.thinkBody.addEventListener('scroll', function () {
      var b = r.thinkBody;
      r.stick = b.scrollTop + b.clientHeight >= b.scrollHeight - 8;
    }, { passive: true });
    r.think.appendChild(r.thinkSum); r.think.appendChild(r.thinkBody);
    r.think.hidden = true;
    r.el.appendChild(r.think);
    r.items = h('div', 'ag-items');
    r.el.appendChild(r.items);
    r.decEl = h('div', 'ag-dec');
    r.decEl.hidden = true;
    r.el.appendChild(r.decEl);
    r.footEl = h('p', 'ag-run-foot');
    r.footEl.hidden = true;
    r.el.appendChild(r.footEl);
    runs[id] = r; order.push(id);
    var f = feedEl();
    if (f) {
      // A new card goes on top; if the user has scrolled into the feed, keep what they are reading where it is.
      var above = f.getBoundingClientRect().top < 0;
      f.insertBefore(r.el, f.firstChild);
      if (above) window.scrollBy(0, r.el.offsetHeight + 10);
    }
    while (order.length > MAX_RUNS) {
      var old = runs[order.shift()];
      if (old) { if (old.el.parentNode) old.el.parentNode.removeChild(old.el); delete runs[old.id]; }
    }
    var s = store();
    if (s) { s.runCount = order.length; s.trimWakes(order); }
    summary(r, { id: id, title: title, ts: r.ts, status: 'working…', live: true, err: false, trig: '', thought: '' });
    if (decByRun[id]) { showDecision(r, decByRun[id]); delete decByRun[id]; }
    return r;
  }
  function wakeRun(id) {
    var r = runs[id];
    if (r) return r;
    var n = id.split(':').pop();
    return makeRun(id, 'Wake ' + n);
  }

  // Text deltas: queued, appended once per frame (no layout work per delta).
  function queue(node, text, run) { pend.push([node, text, run]); if (!raf) raf = window.requestAnimationFrame(flush); }
  function flush() {
    raf = 0;
    var touched = [];
    for (var i = 0; i < pend.length; i++) {
      pend[i][0].appendData(pend[i][1]);
      if (pend[i][2] && touched.indexOf(pend[i][2]) < 0) touched.push(pend[i][2]);
    }
    pend = [];
    touched.forEach(function (r) { if (r.stick && r.think.open) r.thinkBody.scrollTop = r.thinkBody.scrollHeight; });
  }

  function startMessage(r, mid, kind) {
    var node = document.createTextNode('');
    if (kind === 'reasoning') {
      r.think.hidden = false;
      if (r.thinkBody.childNodes.length) r.thinkBody.appendChild(document.createTextNode('\n\n'));
      if (r.thinkText) r.thinkText += '\n\n';  // as in the body and the journal rebuild: the folded line keeps the break
      r.thinkBody.appendChild(node);
    } else {
      var p = h('p', 'ag-text');
      p.appendChild(node);
      r.items.appendChild(p);
    }
    r.msgs[mid] = { node: node, kind: kind };
    return r.msgs[mid];
  }
  function delta(mid, text, kind) {
    var id = runOf(mid), r = id && wakeRun(id);
    if (!r) return;
    var m = r.msgs[mid] || startMessage(r, mid, kind);
    if (m.kind === 'reasoning') r.thinkText += text;
    queue(m.node, text, m.kind === 'reasoning' ? r : null);
  }

  function argLine(args) {
    if (!args || typeof args !== 'object') return args == null ? '' : String(args);
    return Object.keys(args).filter(function (k) { return k !== 'note' && k !== 'request_id'; })
      .map(function (k) { var v = args[k]; return k + '=' + (typeof v === 'string' ? v : json(v)); }).join(', ');
  }
  function toolCard(r, tid, name) {
    var t = { el: h('div', 'ag-tool'), name: name };
    t.head = h('div', 'ag-tool-h');
    t.head.appendChild(h('b', 'mono', name || '?'));
    t.argsEl = h('span', 'ag-tool-args mono');
    t.head.appendChild(t.argsEl);
    t.el.appendChild(t.head);
    t.noteEl = h('p', 'ag-tool-note'); t.noteEl.hidden = true; t.el.appendChild(t.noteEl);
    t.resEl = h('p', 'ag-tool-res', 'running…'); t.el.appendChild(t.resEl);
    r.items.appendChild(t.el);
    r.tools[tid] = t;
    return t;
  }
  function toolArgs(t, args) {
    if (typeof args === 'string') { try { args = JSON.parse(args); } catch (e) { /* shown as sent */ } }
    t.argsEl.textContent = clip(argLine(args), 240);
    if (args && typeof args === 'object' && args.note) { t.noteEl.hidden = false; t.noteEl.textContent = 'Note: ' + args.note; }
  }
  function toolResult(t, res) {
    if (typeof res === 'string') { try { res = JSON.parse(res); } catch (e) { /* shown as sent */ } }
    t.resEl.textContent = '';
    if (res && typeof res === 'object' && res.ok === false) {
      t.el.classList.add('ag-tool-no');
      t.resEl.appendChild(h('b', null, 'The app said no: '));
      t.resEl.appendChild(document.createTextNode((res.reason || 'no reason given') + (res.code ? ' (' + res.code + ')' : '')));
      return;
    }
    if (res == null) { t.resEl.textContent = 'no result recorded'; return; }
    var d = h('details', 'ag-tool-more');
    d.appendChild(h('summary', null, '→ ' + clip(typeof res === 'string' ? res : json(res), 140)));
    d.appendChild(h('pre', 'ag-pre mono', typeof res === 'string' ? res : JSON.stringify(res, null, 1)));
    t.resEl.appendChild(d);
  }

  function decisionWords(d, venues) {
    var bits = [ACTION_WORDS[d.action] || String(d.action || '—')];
    if (d.side) bits[0] += ' ' + d.side;
    if (d.source === 'rule' && d.rule) bits.push('rule: ' + d.rule);  // opened by an armed rule, not the model
    if (d.market) bits.push((venues && venues[d.market] && venues[d.market].label) || d.market);
    if (d.pass_reason) bits.push(d.pass_reason);
    return bits.join(' · ');
  }
  function showDecision(r, d) {
    var s = store(), venues = s && s.status && s.status.venues;
    r.decEl.hidden = false;
    r.decEl.textContent = '';
    r.decEl.className = 'ag-dec' + (d.action === 'UNRECORDED' ? ' ag-dec-none' : '');
    r.decEl.appendChild(h('span', 'pl-eyebrow', 'Decision'));
    r.decEl.appendChild(h('b', null, decisionWords(d, venues)));
    var nums = [];
    if (d.p_estimate != null) nums.push('p ' + pct(d.p_estimate) + (d.p_low != null && d.p_high != null ? ' (' + pct(d.p_low) + '–' + pct(d.p_high) + ')' : '') + (d.p_source ? ' · ' + d.p_source : ''));
    if (d.market_p != null) nums.push('market ' + cents(d.market_p));
    if (d.edge_cents != null) nums.push('edge ' + (d.edge_cents > 0 ? '+' : '') + d.edge_cents + '¢');
    if (nums.length) r.decEl.appendChild(h('span', 'num ag-dec-nums', nums.join(' · ')));
    if (d.thesis) r.decEl.appendChild(h('p', null, d.thesis));
    if (d.invalidation) r.decEl.appendChild(h('p', 'ag-dim', 'Wrong if: ' + d.invalidation));
    if (d.next_check) r.decEl.appendChild(h('p', 'ag-dim', 'Next check: ' + d.next_check));
    if (d.note) r.decEl.appendChild(h('p', 'ag-dim', d.note + (d.wake_status ? ' (' + d.wake_status + ')' : '')));
  }
  function attachDecision(d, sid) {
    var s = store();
    if (s && (!s.decision || (d.id || 0) >= (s.decision.id || 0))) s.decision = d;
    if (s) s.addDecision(d);
    if (d.source === 'rule') {  // an armed rule's entry: its own card (no wake, no model)
      var rid = 'rule:' + sid + ':' + d.id;
      if (runs[rid]) return;
      var rr = makeRun(rid, 'Rule ' + (d.rule || ''));
      setTime(rr, d.ts);
      showDecision(rr, d);
      finishRun(rr, { status: 'opened by the app' });
      return;
    }
    if (d.wake_id == null) {  // an external agent's decision closes its open activity card
      var r = ext || extRun(sid);
      showDecision(r, d);
      finishRun(r, null);
      ext = null;
      return;
    }
    var id = sid + ':' + d.wake_id;
    if (runs[id]) showDecision(runs[id], d); else decByRun[id] = d;
  }

  function finishRun(r, result, error) {
    if (!r || r.finished) return;
    r.finished = true;
    r.el.classList.remove('is-live');
    if (error) r.el.classList.add('is-err');
    if (result && result.trigger && result.trigger.length) r.trigEl.textContent = result.trigger.join(', ');
    var st = error ? 'ended: ' + (error.code || 'error') : (result && result.status) || 'done';
    if (result && ok(result.est_usd)) st += ' · ' + spend(result.est_usd) + ' est.';
    r.stEl.textContent = st;
    summary(r, { status: st, live: false, err: !!error, trig: r.trigEl.textContent,
      thought: r.thinkText ? clip(r.thinkText.replace(/\s+/g, ' ').trim(), 160) : '' });
    if (error && error.message) { r.footEl.hidden = false; r.footEl.textContent = error.message; }
    if (result && result.refusal && result.refusal.text) { r.footEl.hidden = false; r.footEl.textContent = 'Model refusal: ' + result.refusal.text; }
    // The thinking folds to one line once the run is over (tap to reopen).
    if (r.thinkText) {
      r.think.open = false;
      r.thinkSum.textContent = 'Thought: ' + clip(r.thinkText.replace(/\s+/g, ' ').trim(), 110);
    }
  }

  function extRun(sid) {
    var n = order.filter(function (id) { return id.indexOf('ext:') === 0; }).length + 1;
    ext = makeRun('ext:' + sid + ':' + Date.now() + ':' + n, 'External agent');
    return ext;
  }

  // A finished wake rebuilt from its journal row (page load, reconnect).
  function runFromJournal(sid, row) {
    var rec = row.data || {}, id = sid + ':' + rec.wake_id;
    if (runs[id]) return;
    var r = makeRun(id, 'Wake ' + rec.wake_id);
    setTime(r, row.ts);
    var th = (rec.thinking || []).join('\n\n');
    if (th) { r.think.hidden = false; r.thinkBody.textContent = th; r.thinkText = th; }
    // The text and tool calls in the order the model wrote them (a row without `segments`: tools, then the last text).
    var segs = rec.segments || (rec.tools || []).map(function (t, i) { return { kind: 'tool', index: i }; })
      .concat(rec.decision ? [{ kind: 'text', text: rec.decision }] : []);
    segs.forEach(function (sg) {
      if (sg.kind === 'text') { if (sg.text) r.items.appendChild(h('p', 'ag-text', sg.text)); return; }
      var t = (rec.tools || [])[sg.index];
      if (!t) return;
      var c = toolCard(r, id + ':t' + (sg.index + 1), t.name);
      toolArgs(c, t.args);
      toolResult(c, t.result != null ? t.result : (t.ok === false ? { ok: false, code: t.code } : null));
    });
    var err = ['invalid_key', 'unavailable', 'bad_request', 'misconfigured', 'cancelled'].indexOf(rec.status) >= 0;  // agent_feed.ERROR_STATUSES
    finishRun(r, { status: rec.status, est_usd: rec.est_usd, refusal: rec.refusal,
      trigger: (rec.trigger || []).map(function (e) { return e && e.type; }) },
      err ? { code: rec.status, message: rec.error } : null);
    if (err && rec.trigger) { r.trigEl.textContent = rec.trigger.map(function (e) { return e && e.type; }).join(', '); summary(r, { trig: r.trigEl.textContent }); }
  }
  // An external session's tool row (journal) or CUSTOM tool_call: into the open external activity card.
  function extTool(sid, t, ts) {
    var r = ext || extRun(sid);
    if (ts) setTime(r, ts);
    var c = toolCard(r, r.id + ':t' + Object.keys(r.tools).length, t.name);
    toolArgs(c, t.args);
    toolResult(c, t.result != null ? t.result : { ok: t.ok, code: t.code });
  }

  // ---------- the stream ----------
  var es = null, reconnectT = null, gen = 0;

  function handle(ev, s) {
    var t = ev.type;
    if (t === 'STATE_SNAPSHOT') { s.applyState(ev.snapshot, true); return; }
    if (!s.state) return;  // deltas before a snapshot cannot apply
    if (t === 'STATE_DELTA') {
      try { s.applyState(applyPatch(clone(s.state), ev.delta || []), false); }
      catch (e) { s.streamNote = 'The live state could not be updated (' + errText(e) + '); reloading it.'; s.restart(); }
      return;
    }
    var sid = s.state.session && s.state.session.id;
    switch (t) {
      case 'RUN_STARTED': {
        if (sid && ev.threadId !== sid) break;
        var rs = wakeRun(ev.runId);
        if (ev.trigger && ev.trigger.length) { rs.trigEl.textContent = ev.trigger.join(', '); summary(rs, { trig: rs.trigEl.textContent }); }
        if (ok(ev.server_ms)) setTime(rs, ev.server_ms);
        break;
      }
      case 'REASONING_MESSAGE_CONTENT': delta(ev.messageId, ev.delta || '', 'reasoning'); break;
      case 'TEXT_MESSAGE_CONTENT': delta(ev.messageId, ev.delta || '', 'text'); break;
      case 'REASONING_START': case 'REASONING_MESSAGE_START': case 'TEXT_MESSAGE_START': {
        var id0 = runOf(ev.messageId), r0 = id0 && wakeRun(id0);
        if (r0 && !r0.msgs[ev.messageId]) startMessage(r0, ev.messageId, t === 'TEXT_MESSAGE_START' ? 'text' : 'reasoning');
        break;
      }
      case 'TOOL_CALL_START': { var id1 = runOf(ev.toolCallId), r1 = id1 && wakeRun(id1); if (r1) toolCard(r1, ev.toolCallId, ev.toolCallName); break; }
      case 'TOOL_CALL_ARGS': { var r2 = runs[runOf(ev.toolCallId)], c2 = r2 && r2.tools[ev.toolCallId]; if (c2) toolArgs(c2, ev.delta); break; }
      case 'TOOL_CALL_RESULT': { var r3 = runs[runOf(ev.toolCallId)], c3 = r3 && r3.tools[ev.toolCallId]; if (c3) toolResult(c3, ev.content); break; }
      case 'RUN_FINISHED': finishRun(runs[ev.runId], ev.result || {}); break;
      case 'RUN_ERROR': finishRun(runs[ev.runId], null, { code: ev.code, message: ev.message }); break;
      case 'CUSTOM': s.custom(ev.name, ev.value || {}); break;
      default: break;  // *_END events need nothing: a run's message ends when the next starts or the run finishes
    }
  }

  // The selected agent's run: its own stream (GET /api/agent/sessions/{id}/stream); nothing selected, no stream.
  function connect() {
    var s = store();
    if (!s || s.enabled !== true || es || !s.selSid || document.visibilityState !== 'visible') return;
    clearTimeout(reconnectT); reconnectT = null;
    var my = ++gen, url = '/api/agent/sessions/' + encodeURIComponent(s.selSid) + '/stream';
    try { es = new EventSource(url); } catch (e) { s.streamNote = 'Could not open the live feed: ' + errText(e); reconnect(); return; }
    es.onopen = function () { if (my !== gen) return; s.live = true; s.everLive = true; s.lastMsg = Date.now(); };
    es.onmessage = function (m) {
      if (my !== gen) return;
      s.lastMsg = Date.now(); s.live = true; s.everLive = true;
      var ev;
      try { ev = JSON.parse(m.data); } catch (e) { s.streamNote = 'The live feed sent a frame that is not JSON; it is ignored.'; return; }
      try { handle(ev, s); } catch (e) { s.streamNote = 'A live event could not be shown: ' + errText(e); }
    };
    es.onerror = function () {
      if (my !== gen) return;
      if (s.live) s.liveAt = s.lastMsg;
      s.live = false;
      if (es && es.readyState === 2) { es = null; reconnect(); }  // CLOSED: the browser will not retry by itself
    };
  }
  function disconnect() {
    var s = store();
    gen++;
    if (es) { es.close(); es = null; }
    clearTimeout(reconnectT); reconnectT = null;
    if (s) { if (s.live) s.liveAt = s.lastMsg; s.live = false; }
  }
  function reconnect() {
    if (reconnectT) return;
    reconnectT = setTimeout(function () { reconnectT = null; connect(); }, RECONNECT_MS);
  }

  // ---------- the shared store (the Agents tab, the tab bar's accessory, Play's "agents here" line) ----------
  document.addEventListener('alpine:init', function () {
    Alpine.store('agent', {
      enabled: null,        // null = not known yet; false = the routes answer 404 (agent mode off)
      status: null, statusErr: '', state: null, pc: null, killed: null, killErr: '',
      live: false, everLive: false, lastMsg: 0, liveAt: null, streamNote: '', trimmed: 0, skewMs: 0,
      decision: null, notes: [], runCount: 0, lastJid: 0, endText: '',
      decisions: [],        // every decision row of the shown run seen (journal + live), oldest first, deduplicated
      wakes: [],            // one summary per run card in #ag-feed: {id, title, ts, status, live, err, trig, thought}
      leftS: null, leftAt: 0, tick: 0,
      busy: false, msg: '', msgCls: '',
      pending: [],          // the shown run's Assist proposals waiting for an answer ({id, created_ts}; views/proposal.js)
      card: null, cardErr: '',  // the rule scorekeeper (GET /api/agent/scorecard)
      // the fleet: GET /api/agent/sessions ({sessions: [row], summary}) and GET /api/agent/agents ([agent])
      fleet: null, fleetErr: '', agents: null, agentsErr: '',
      // the agent on screen (its id) and the run shown (its live session, else its last)
      sel: null, selSid: null, selView: null, selErr: '',
      stateSeq: 0,          // bumped by every applied state (stream, action answer, read): a read sent before one is late
      stopAsk: false,       // the accessory's on-screen "Stop all?" (the server asks nothing: R18)

      init: function () {
        var self = this;
        self.loadStatus();
        api.get('/api/access').then(function () { self.pc = true; }, function (e) { if (e && e.status === 403) self.pc = false; });
        document.addEventListener('visibilitychange', function () {
          if (document.visibilityState !== 'visible') { disconnect(); return; }
          if (self.enabled) { self.loadStatus(); self.loadFleet(); connect(); }
        });
        setInterval(function () {
          if (!self.enabled || document.visibilityState !== 'visible') return;
          if (self.sessionLive() || self.fleetCount()) self.tick++;
          if (!self.selSid) return;
          // The server sends an event at least every 15 s (a heartbeat when nothing else), so a quiet stream is dead.
          if (self.live && Date.now() - self.lastMsg > STALE_MS) {
            self.liveAt = self.lastMsg; self.live = false; disconnect(); reconnect();
          } else if (!es && !reconnectT) reconnect();
        }, 1000);
        setInterval(function () {
          var s = self.session();
          if (self.enabled && s && s.state === 'paused' && document.visibilityState === 'visible') self.loadKill();
        }, KILL_POLL_MS);
        setInterval(function () { if (self.enabled && document.visibilityState === 'visible') self.loadFleet(); }, FLEET_MS);
        // The status carries settle_warnings (a position settling > 60 min after the close): refresh it now and then.
        setInterval(function () {
          if (self.enabled && document.visibilityState === 'visible') self.loadStatus();
        }, 60000);
      },
      loadStatus: function () {
        var self = this;
        return api.get('/api/agent/status').then(function (r) {
          self.status = r; self.statusErr = '';
          if (ok(r.server_ms)) self.skewMs = r.server_ms - Date.now();
          var was = self.enabled;
          self.enabled = true;
          if (!was) { self.loadFleet(); connect(); }
          return r;
        }, function (e) {
          if (e && e.status === 404) { self.enabled = false; disconnect(); return; }
          self.statusErr = errText(e);
        });
      },
      // The fleet: every live session's row and the summary, and every agent with its last run.
      loadFleet: function () {
        var self = this;
        if (self.enabled !== true) return Promise.resolve();
        var a = api.get('/api/agent/sessions').then(function (r) {
          self.fleet = r; self.fleetErr = '';
        }, function (e) { if (!(e && e.status === 404)) self.fleetErr = errText(e); });
        var b = api.get('/api/agent/agents').then(function (r) {
          self.agents = (r && r.agents) || []; self.agentsErr = '';
        }, function (e) { if (!(e && e.status === 404)) self.agentsErr = errText(e); });
        return Promise.all([a, b]).then(function () { self.followSel(); });
      },
      loadCard: function () {
        var self = this;
        return api.get('/api/agent/scorecard').then(function (r) { self.card = r; self.cardErr = ''; return r; },
          function (e) { self.cardErr = errText(e); });
      },
      loadKill: function () {
        var self = this;
        api.get('/api/trading/status', { strategy: 'you' }, 4000).then(function (r) {
          if (r && typeof r.killed === 'boolean') { self.killed = r.killed; self.killErr = ''; }
          else { self.killed = null; self.killErr = (r && r.reason) || 'the server sent no kill switch state'; }
        }, function (e) { self.killed = null; self.killErr = errText(e); });
      },
      restart: function () { disconnect(); connect(); },

      // ----- which agent is on screen -----
      agentOf: function (id) { return (this.agents || []).filter(function (a) { return a.id === id; })[0] || null; },
      rowOf: function (sid) { return this.fleetRows().filter(function (r) { return r.id === sid; })[0] || null; },
      // One agent's screen: its live run, else its last (`sid` when the caller knows it).
      select: function (agentId, sid) {
        if (!agentId) throw new Error('agent: select needs an agent id');
        var a = this.agentOf(agentId);
        this.sel = agentId;
        this.showRun(sid || (a && (a.running_session_id || (a.last_run && a.last_run.session_id))) || null);
      },
      // The agent started another run (or the list learned of one): the screen follows its live run.
      followSel: function () {
        var a = this.sel && this.agentOf(this.sel);
        if (a && a.running_session_id && a.running_session_id !== this.selSid) this.showRun(a.running_session_id);
      },
      showRun: function (sid) {
        if (sid === this.selSid) { if (sid) this.loadSel(); return; }
        disconnect();
        this.selSid = sid; this.selView = null; this.selErr = ''; this.state = null; this.pending = [];
        this.everLive = false; this.live = false; this.streamNote = ''; this.killed = null;
        resetFeed(sid);
        if (!sid) return;
        this.loadSel();
        connect();
        try { window.dispatchEvent(new CustomEvent('agent-selected', { detail: { session_id: sid } })); } catch (e) { /* no listener */ }
      },
      unselect: function () { this.sel = null; this.showRun(null); },
      loadSel: function () {
        var self = this, sid = self.selSid, seq = self.stateSeq;
        if (!sid) return Promise.resolve();
        return api.get('/api/agent/sessions/' + encodeURIComponent(sid)).then(function (r) {
          if (sid !== self.selSid) return;
          self.selErr = '';
          // A state applied after this read was sent (the stream's pause, a stop's answer) is newer: a late answer
          // would put the run back as it was when the read started (running, no pause reason) until the next change.
          if (seq !== self.stateSeq) return;
          self.selView = r;
          if (!self.state) self.applyState({ session: r.session, positions: r.positions || null, prompts: self.status && self.status.prompts }, true, true);
          else if (r.session) self.applyState({ session: r.session }, false, true);
        }, function (e) { if (sid === self.selSid) self.selErr = errText(e); });
      },

      // A snapshot (or a patched state, or a session view from an HTTP answer when `partial`).
      applyState: function (st, snapshot, partial) {
        var prev = this.state && this.state.session, next = st && st.session;
        if (next && this.selSid && next.id !== this.selSid) return;  // another run's answer, after a switch
        this.stateSeq++;
        if (partial && this.state) { this.state = Object.assign({}, this.state, { session: next }); }
        else this.state = st;
        if (next && (!prev || prev.seconds_left !== next.seconds_left || snapshot)) { this.leftS = next.seconds_left; this.leftAt = Date.now(); }
        var sid = next ? next.id : null;
        // An external session's live tool calls carry no journal id, so a reconnect rebuilds its feed from the journal.
        if (sid !== feedSid || (snapshot && !partial && next && next.kind === 'external')) resetFeed(sid);
        if (sid && (snapshot || !prev || prev.id !== sid)) this.fillJournal(sid);
        if (next && next.state === 'paused' && (!prev || prev.state !== 'paused')) this.loadKill();
        if (prev && next && prev.id === next.id && prev.state !== next.state) { this.loadStatus(); this.loadFleet(); }
      },
      fillJournal: function (sid) {
        var self = this;
        api.get('/api/agent/sessions/' + encodeURIComponent(sid) + '/journal', { after_id: self.lastJid || null, limit: 200 }).then(function (r) {
          if (!r || r.session_id !== sid || feedSid !== sid) return;
          var kind = self.session() && self.session().kind;
          (r.rows || []).forEach(function (row) {
            self.lastJid = Math.max(self.lastJid, row.id || 0);
            var d = row.data || {};
            if (row.kind === 'wake') runFromJournal(sid, row);
            else if (row.kind === 'decision') attachDecision(Object.assign({ id: row.id }, d), sid);
            else if (row.kind === 'note') self.upsertNote(Object.assign({ id: row.id, ts: row.ts }, d));
            else if (row.kind === 'tool' && kind === 'external') extTool(sid, d, row.ts);
            else if (row.kind === 'state' && d.state === 'ended') self.endText = d.reason || '';
          });
        }, function (e) { self.streamNote = 'The journal could not be read: ' + errText(e); });
      },
      custom: function (name, v) {
        // every CUSTOM event, for listeners outside this file (views/proposal.js: proposal_new / proposal_resolved)
        try { window.dispatchEvent(new CustomEvent('agent-custom', { detail: { name: name, value: v } })); } catch (e) { /* no listener */ }
        var sid = this.state && this.state.session && this.state.session.id;
        if (name === 'heartbeat') { if (ok(v.server_ms)) this.skewMs = v.server_ms - Date.now(); }
        else if (name === 'decision') { if (!v.session_id || v.session_id === sid) attachDecision(v, v.session_id || sid); }
        else if (name === 'note') { if (!v.session_id || v.session_id === sid) this.upsertNote(v); }
        else if (name === 'tool_call') { if (v.session_id === sid) extTool(sid, v); }
        else if (name === 'proposal_new' || name === 'proposal_resolved') this.loadFleet();
        else if (name === 'rule_status') {
          Alpine.store('app').announce('Rule ' + v.rule + ' ' + (v.status === 'retired' ? 'retired' + (v.by === 'auto' ? ' automatically' : '') : 'reactivated') + (v.reason ? ': ' + v.reason : ''));
          this.loadCard(); this.loadStatus();
        }
        else if (name === 'session_state') {
          if (v.state === 'ended') this.endText = '';
          var who = (this.session() && this.session().name) || 'Agent';
          var words = v.state === 'paused' ? who + ' paused: ' + (v.pause_reason || 'no reason sent')
            : v.state === 'ended' ? who + ' ended: ' + (END_WORDS[v.end_reason] || v.end_reason || 'no reason sent')
            : who + ' ' + v.state;
          Alpine.store('app').announce(words);
          if (v.state === 'ended' && sid) this.fillJournal(sid);
          if (v.state === 'ended') this.loadCard();
          this.loadFleet();
        } else if (name === 'feed_trimmed') {
          this.trimmed += v.dropped || 0;
          this.streamNote = (v.dropped || 'Some') + ' live events were dropped on this slow connection; the feed was reloaded from the journal.';
          this.restart();
        }
      },
      addDecision: function (d) {
        var key = d.id != null ? 'j' + d.id : 'w' + d.wake_id + ':' + d.ts;
        var list = this.decisions.slice(), i = list.findIndex(function (x) { return x._key === key; });
        var row = Object.assign({ _key: key }, d);
        if (i >= 0) list[i] = row; else list.push(row);
        this.decisions = list.slice(-MAX_DECISIONS);
      },
      wakeUp: function (id, patch) {
        var list = this.wakes.slice(), i = list.findIndex(function (x) { return x.id === id; });
        if (i >= 0) list[i] = Object.assign({}, list[i], patch); else list.push(Object.assign({ id: id }, patch));
        this.wakes = list;
      },
      trimWakes: function (keep) {
        if (this.wakes.some(function (w) { return keep.indexOf(w.id) < 0; })) this.wakes = this.wakes.filter(function (w) { return keep.indexOf(w.id) >= 0; });
      },
      // bracket id -> the rule decision that opened it (a rule's ENTRY row carries its bracket_id)
      ruleByBracket: function () {
        var m = {};
        this.decisions.forEach(function (d) { if (d.bracket_id) m[d.bracket_id] = d; });
        return m;
      },
      upsertNote: function (n) {
        var list = this.notes.slice(), i = list.findIndex(function (x) { return x.id === n.id; });
        if (i >= 0) list[i] = Object.assign({}, list[i], n); else list.push(n);
        this.notes = list.slice(-20);
      },

      // ----- the fleet: rows, totals, the accessory, Play's line -----
      fleetRows: function () { return (this.fleet && this.fleet.sessions) || []; },
      fleetLive: function () { return this.fleetRows().filter(function (r) { return LIVE.indexOf(r.state) >= 0; }); },
      summary: function () { return (this.fleet && this.fleet.summary) || null; },
      fleetCount: function () { var s = this.summary(); return s ? s.count : 0; },
      needCount: function () { var s = this.summary(); return s ? (s.needs_you || []).length : 0; },
      todayNet: function () { var s = this.summary(); return s && s.today ? s.today.net : null; },
      accShow: function () { return this.enabled === true && this.fleetCount() > 0; },
      accHead: function () { var n = this.fleetCount(); return n + (n === 1 ? ' agent' : ' agents'); },
      accLine: function () {
        var k = this.needCount();
        return '· ' + signedUsd(this.todayNet()) + (k ? ' · ' + k + (k === 1 ? ' needs you' : ' need you') : '');
      },
      // one agent: Stop at once (R18: stopping only lowers risk); several: the on-screen "Stop all?" first
      accStop: function () {
        var live = this.fleetLive();
        if (live.length === 1) { this.stop(live[0].id); return; }
        if (live.length > 1) this.stopAsk = true;
      },
      // Play's quiet line: the agents on the market you are looking at, and what they hold
      here: function (market) {
        var rows = this.fleetLive().filter(function (r) { return r.market === market; });
        if (!rows.length) return null;
        var n = rows.length, holders = rows.filter(function (r) { return (r.holding || []).length; });
        var head = n === 1 ? '1 agent here' : n + ' agents here', tail;
        if (!holders.length) tail = 'watching';
        else if (holders.length === 1) {
          var h = holders[0].holding[holders[0].holding.length - 1];
          tail = (n === 1 ? 'it holds ' : 'one holds ') + (h.side === 'UP' ? 'Up' : h.side === 'DOWN' ? 'Down' : String(h.side || '—')) + ' ' + usd(h.cost) + (h.rule ? ' (' + h.rule + ')' : '');
        } else tail = holders.length + ' hold positions';
        return { head: head, tail: tail, market: market };
      },

      // ----- reads for the agent on screen -----
      // positions whose result is not booked 60+ min after the close (GET /api/agent/status settle_warnings)
      settleWarnings: function () { return (this.status && this.status.settle_warnings) || []; },
      session: function () { return (this.state && this.state.session) || (this.selView && this.selView.session) || null; },
      sessionLive: function () { var s = this.session(); return !!(s && LIVE.indexOf(s.state) >= 0); },
      ended: function () { var s = this.session(); return !!(s && s.state === 'ended'); },
      // The session's money kind, in Play's words: Live = real money, Paper = demo money, Shadow = no money.
      pill: function () {
        var s = this.session(), modes = s && s.pinned ? Object.keys(s.pinned).map(function (k) { return s.pinned[k]; }) : [];
        if (!modes.length) return { text: '—', cls: '', aria: 'Mode not known' };
        if (modes.indexOf('prod') >= 0) return { text: 'Live', cls: 'nv-pill-prod', aria: 'Live: real money' };
        if (modes.indexOf('sandbox') >= 0) return modes.indexOf('shadow') >= 0
          ? { text: 'Paper + shadow', cls: 'nv-pill-sandbox', aria: 'Paper (demo money) and shadow (no money)' }
          : { text: 'Paper', cls: 'nv-pill-sandbox', aria: 'Paper: Kalshi demo money' };
        return { text: 'Shadow', cls: 'nv-pill-shadow', aria: 'Shadow: no money' };
      },
      // The state chip in words, with the time it began (design pass 2026-10-31): Watching · Thinking (a wake is
      // running) · Waiting for you (an Assist proposal waits) · Placing (an order is being sent) · Paused · Stopped ·
      // Ended. Every time is the server's (a row's ts), shown on this device's clock.
      chip: function () {
        var s = this.session(), self = this;
        if (!s) return { key: 'none', word: '', since: '' };
        var hm = function (ms) { return ok(ms) ? fmt.hm(ms - self.skewMs) : ''; };
        if (s.state === 'ended') return { key: 'ended', word: s.end_reason === 'stopped' ? 'Stopped' : 'Ended', since: hm(s.ended_ts) ? 'at ' + hm(s.ended_ts) : '' };
        if (s.state === 'paused') return { key: 'paused', word: 'Paused', since: hm(s.updated_ts) ? 'since ' + hm(s.updated_ts) : '' };
        var placing = this.positions().filter(function (p) { return p.state === 'sending'; })[0];
        if (placing) return { key: 'placing', word: 'Placing', since: hm(placing.created_ts) ? 'since ' + hm(placing.created_ts) : '' };
        if (this.pending.length) {
          var first = Math.min.apply(null, this.pending.map(function (p) { return ok(p.created_ts) ? p.created_ts : Infinity; }));
          return { key: 'waiting', word: 'Waiting for you', since: isFinite(first) ? 'since ' + hm(first) : '' };
        }
        var run = this.wakes.filter(function (w) { return w.live; })[0];
        if (run) return { key: 'thinking', word: 'Thinking', since: hm(run.ts) ? 'since ' + hm(run.ts) : '' };
        var last = s.started_ts;
        this.wakes.forEach(function (w) { if (ok(w.ts) && w.ts > last) last = w.ts; });
        if (this.decision && ok(this.decision.ts) && this.decision.ts > last) last = this.decision.ts;
        return { key: 'watching', word: 'Watching', since: hm(last) ? 'since ' + hm(last) : '' };
      },
      // The "now" line says what is next: the position, then the agent's next check (its last decision's), the rules
      // it is watching for, or the external agent it waits for.
      nextLine: function () {
        var s = this.session();
        if (!s) return '';
        if (s.state === 'paused') return 'Paused: ' + (s.pause_reason || 'no reason sent');
        if (s.state === 'ended') return 'Ended: ' + this.endWords();
        var n = this.openPositions().length, pos = n ? n + (n === 1 ? ' open position.' : ' open positions.') : 'No position.';
        var d = this.decision, rules = (s.limits && s.limits.rules) || [];
        if (this.pending.length) return pos + ' A proposal waits for your answer.';
        if (d && d.next_check) return pos + ' Next check: ' + d.next_check + '.';
        if (rules.length) return pos + ' Watching ' + rules.join(', ') + ' for a setup.';
        if (s.kind === 'external') return pos + ' Waiting for the external agent.';
        return pos + (this.runCount ? ' Next look at the next market event.' : ' First look at the next market event.');
      },
      levelWord: function () {
        var s = this.session();
        if (!s) return '';
        if (s.kind === 'external') return 'External';
        return s.limits && s.limits.level === 'assist' ? 'Assist' : 'Auto';
      },
      // session net = minus the realized net loss of closed positions (budget_parts.realized: gains negative)
      netNow: function () { var b = this.budget(); return b && b.realized != null ? -b.realized : null; },
      netWord: function () { return signedUsd(this.netNow()); },
      tradesCount: function () { return this.positions().filter(function (p) { return ok(p.filled) && p.filled > 0; }).length; },
      pillCls: function () { var s = this.session(); return this.pill().cls + (s && s.state === 'paused' ? ' ag-pill-paused' : ''); },
      stateWord: function () { var s = this.session(); return s ? String(s.state).toUpperCase() : ''; },
      nowLine: function () {
        var s = this.session();
        if (!s) return '';
        if (s.state === 'paused') return 'Paused: ' + (s.pause_reason || 'no reason sent');
        if (s.state === 'ended') return 'Ended: ' + (this.endText || END_WORDS[s.end_reason] || s.end_reason || 'no reason sent');
        var d = this.decision;
        if (!d) return s.kind === 'external' ? 'No decision recorded yet; waiting for the external agent.' : this.runCount ? 'No decision recorded yet.' : 'Waiting for the first event (no wake yet).';
        var line = decisionWords(d, this.status && this.status.venues);
        if (d.next_check) line += ' · next check: ' + d.next_check;
        return line + ' (' + fmt.time(d.ts) + ')';
      },
      positions: function () { return (this.state && this.state.positions) || (this.selView && this.selView.positions) || []; },
      openPositions: function () { return this.positions().filter(function (p) { return OPEN_STATES.indexOf(p.state) >= 0; }); },
      // budget_used = realized net loss + the worst case of open positions (the session view's budget_parts).
      budget: function () {
        var s = this.session();
        if (!s || !s.limits) return null;
        var p = s.budget_parts || {};
        return { limit: s.limits.budget, used: s.budget_used, left: s.budget_left,
          risk: ok(p.at_risk) ? p.at_risk : null, realized: ok(p.realized) ? p.realized : null };
      },
      budgetLine: function () {
        var b = this.budget();
        if (!b) return '';
        var r = b.realized == null ? 'realized —' : b.realized > 0 ? 'lost ' + usd(b.realized) : b.realized < 0 ? 'won ' + usd(-b.realized) : 'realized $0.00';
        return r + ' · at risk ' + (b.risk == null ? '—' : usd(b.risk)) + ' · left ' + usd(b.left) + ' of ' + usd(b.limit);
      },
      spendLine: function () {
        var s = this.session();
        if (!s) return '';
        if (s.kind === 'external') return 'API spend: on the external agent';
        var u = s.usage || {};
        return 'API ' + spend(ok(u.api_usd) ? u.api_usd : 0) + ' of ' + usd(s.limits && s.limits.spend_cap) + ' est.';
      },
      rulesLine: function () {
        var s = this.session(), r = s && s.limits && s.limits.rules;
        if (!r || !r.length) return '';
        var one = s.limits.one_bet_per_window !== false;  // a session stored before the option had it on
        return 'rules: ' + r.join(', ') + (one ? ' · one bet per window' : ' · may bet the same window again');
      },
      wakesLine: function () { var s = this.session(), u = (s && s.usage) || {}; return s && s.kind === 'in_app' ? 'wakes ' + (u.wakes || 0) + (s.model ? ' · ' + s.model : '') : ''; },
      timeLeft: function () {
        this.tick;
        var s = this.session();
        if (!s || !this.sessionLive() || !ok(this.leftS)) return '—';
        return left(this.leftS - (Date.now() - this.leftAt) / 1000) + ' left';
      },
      stale: function () { return this.enabled === true && !!this.selSid && this.everLive && !this.live; },
      staleText: function () {
        this.tick;
        if (!this.stale()) return '';
        var ago = this.liveAt ? Math.round((Date.now() - this.liveAt) / 1000) : null;
        return 'Live feed lost: reconnecting' + (ago != null ? ', last update ' + ago + ' s ago' : '') + '. What you see may be out of date.';
      },
      endWords: function () { var s = this.session(); return s ? (this.endText || END_WORDS[s.end_reason] || s.end_reason || 'no reason sent') : ''; },
      posLine: function (p) {
        var px = ok(p.entry_avg) ? (p.entry_avg <= 1 ? cents(p.entry_avg) : usd(p.entry_avg)) : '—';
        return (p.ticker || p.strategy || '—') + ' · ' + (p.side || '—') + ' ' + (ok(p.filled) ? p.filled : '—') + ' @ ' + px +
          (ok(p.take_profit) ? ' · take profit ' + (p.take_profit <= 1 ? cents(p.take_profit) : usd(p.take_profit)) : '') +
          ' · worst case ' + usd(p.max_cost) + ' · ' + p.state;
      },
      // ----- paused: the one action each reason allows -----
      pauseKind: function () {
        var s = this.session();
        switch (s && s.pause_code) {
          case 'kill_switch': return this.killed === false ? 'kill_off' : 'kill';
          case 'mode_changed': return 'mode';
          default: return 'other';  // model_unavailable, runtime_misconfigured, bad_request, global_limit, other
        }
      },

      // ----- actions: every one names its session (POST /api/agent/sessions/{id}/...) -----
      // An action's outcome: a toast (read out); a failure also stays on the Agents tab until the next action.
      say: function (text, cls) { this.msg = text; this.msgCls = cls || ''; Alpine.store('app').toast(text, cls); },
      after: function () { this.busy = false; this.loadStatus(); this.loadFleet(); this.loadSel(); },
      stop: function (sid) {
        var self = this;
        sid = sid || self.selSid;
        if (self.busy || !sid) return;
        self.busy = true; self.msg = '';
        api.post('/api/agent/sessions/' + encodeURIComponent(sid) + '/stop').then(function (r) {
          if (r && r.ok) {
            if (sid === self.selSid) self.applyState({ session: r.session }, false, true);
            self.say(((r.session && r.session.name) || 'The agent') + ' stopped. Open positions keep their take profit and stops and are yours in Play.', '');
          } else self.say(refusal(r), 'red');
        }, function (e) { self.say('Stop failed: ' + errText(e), 'red'); })['finally'](function () { self.after(); });
      },
      stopAll: function () {
        var self = this;
        if (self.busy) return;
        self.busy = true; self.stopAsk = false; self.msg = '';
        api.post('/api/agent/stop_all').then(function (r) {
          if (r && r.ok) {
            var all = r.stopped || [], n = all.filter(function (x) { return x.ok; }).length, bad = all.filter(function (x) { return !x.ok; });
            self.say('Stopped ' + n + (n === 1 ? ' agent' : ' agents') + '. Open positions keep their take profit and stops and are yours in Play.' +
              (bad.length ? ' Not stopped: ' + bad.map(function (x) { return x.name + ' (' + (x.reason || x.code) + ')'; }).join(', ') + '.' : ''), bad.length ? 'red' : '');
          } else self.say(refusal(r), 'red');
        }, function (e) { self.say('Stop all failed: ' + errText(e), 'red'); })['finally'](function () { self.after(); });
      },
      resume: function (confirm) {
        var self = this, sid = self.selSid;
        if (self.busy || !sid) return;
        self.busy = true; self.msg = '';
        api.post('/api/agent/sessions/' + encodeURIComponent(sid) + '/resume', { confirm: confirm || null }).then(function (r) {
          if (r && r.ok) { self.applyState({ session: r.session }, false, true); self.say('Resumed.', ''); }
          else self.say(refusal(r), 'red');
        }, function (e) { self.say('Resume failed: ' + errText(e), 'red'); })['finally'](function () { self.after(); });
      }
    });
  });

  // Opens the Agents tab (the accessory, Play's "agents here" line): the fleet, or one market's agents.
  window.agentOpen = function (market) {
    try { window.localStorage.removeItem('perps.open'); } catch (e) { /* storage blocked */ }
    try { history.replaceState(null, '', location.pathname + location.search + '#agent'); } catch (e) { /* keep the hash */ }
    try { window.dispatchEvent(new CustomEvent('agent-fleet', { detail: { market: market || null } })); } catch (e) { /* no listener */ }
    Alpine.store('app').view = 'agent';
  };
  function leaveTo(hash) { try { history.replaceState(null, '', location.pathname + location.search + hash); } catch (e) { /* keep the hash */ } }

  // DELETE (app.js's api has get / post only): the same error shape (status, message, body).
  function del(path) {
    return fetch(path, { method: 'DELETE', cache: 'no-store' }).then(function (res) {
      return res.text().then(function (t) {
        var d = null;
        try { d = t ? JSON.parse(t) : null; } catch (e) { d = t; }
        if (!res.ok) {
          var e = new Error((d && (d.reason || d.detail)) || 'The server returned an error (' + res.status + ').');
          e.status = res.status; e.body = d; throw e;
        }
        return d;
      });
    }, function () { var e = new Error('Could not reach the server.'); e.status = 0; throw e; });
  }
  function cap(x) { x = String(x || ''); return x ? x.charAt(0).toUpperCase() + x.slice(1).toLowerCase() : '—'; }
  // exact figures from the agent state (position_view): entry_cost = contracts x price + the entry fee; realized =
  // proceeds - entry_cost - take-profit and exit fees (closed and filled only)
  function paidOf(p) { return ok(p.entry_cost) ? p.entry_cost : null; }
  function won(p) { return p.state === 'closed' && ok(p.realized) && p.realized > 0; }
  function netCls(x) { return !ok(x) || Math.abs(x) < 0.005 ? '' : x > 0 ? 'green' : 'red'; }
  function dur(ms) {
    if (!ok(ms) || ms < 0) return '—';
    var m = Math.round(ms / 60000), hh = Math.floor(m / 60);
    return hh ? hh + ' h ' + String(m % 60).padStart(2, '0') : m + ' min';
  }
  var SHEETS = ['note', 'settings', 'prompts', 'telegram'];
  var SCREENS = ['home', 'activity', 'score'].concat(SHEETS);
  // the Agents tab's pages: the fleet, one agent (its live or last run), New agent, Fleet limits, All agents (audit)
  var PAGES = ['fleet', 'agent', 'new', 'limits', 'audit'];
  var FILTERS = ['trades', 'decisions', 'thinking', 'everything'];
  // All activity's filters as journal kinds (GET .../journal?kinds=): Trades = rule entries and acting decisions,
  // Decisions, Thinking = the wakes, Everything = every row
  var FILTER_KINDS = { trades: 'rule,decision', decisions: 'decision', thinking: 'wake', everything: '' };
  var REAL = ['ENTRY', 'EXIT', 'REDUCE', 'HEDGE', 'HOLD'];  // the decisions "Latest" shows (not NO_TRADE / UNRECORDED)
  var TRADE_ACTIONS = ['ENTRY', 'EXIT', 'REDUCE', 'HEDGE'];  // the decisions All activity's Trades shows
  var ALERTS = [['proposals', 'Assist proposals'], ['trades', 'Trades'], ['activity', 'Session activity'], ['pauses', 'Pauses'],
    ['session_end', 'Session end'], ['settle_warnings', 'Late settlements'], ['setups', 'Setups forming'],
    ['data', 'Data recorders'], ['daily', 'Daily summary']];
  var WIDE = '(min-width: 1000px)';
  var GROUP_AT = 8;     // more live agents than this: the healthy ones fold into markets and one sentence
  var JR_PAGE = 50, JR_HEAD_MS = 10000;
  var MODE_WORDS = { prod: 'Live · real money', sandbox: 'Paper', shadow: 'Shadow' };
  function plural(n, one) { return n + ' ' + (n === 1 ? one : one + 's'); }
  function blankForm() {
    return { kind: 'in_app', level: 'auto', proposal_ttl_s: '', model: '', market: '', name: '', agent_id: '', rules: {},
      one_bet: true, budget: '', per_trade: '', minutes: '', spend_cap: '', confirm: '' };
  }

  // ---------- the Agents tab: the fleet; one agent (its live run, else its last); New agent; limits; the audit ----------
  window.agentView = function () {
    return {
      page: 'fleet', screen: 'home', filter: 'trades', wide: false, mkt: null,  // mkt: the fleet shows one market
      // level: 'auto' | 'assist' (the server requires it). proposal_ttl_s: assist only (empty: the server's 20 s)
      form: blankForm(),
      nameAuto: true,  // the name is still the server's default for the market (sent as no name: the server picks it)
      touched: false, filledFrom: '',  // the user changed the form / it was prefilled from ('last', 'run', 'clone', a preset name)
      launching: false, launchMsg: '', token: '', copied: '',
      slideX: 0, sliding: false, sx0: 0,
      // All activity across runs (or every agent's, the audit): newest first, paged back by before_id
      jr: { key: '', rows: [], runs: {}, next: null, busy: false, err: '', done: false, loaded: false },
      gl: { max_loss_day: '', api_day: '', busy: false, msg: '', cls: '' },
      rsTyped: {}, rsBusy: '', rsMsg: '', rsMsgCls: '',
      noteText: '', noteBusy: false, noteMsg: '', resumeTyped: '',
      pr: null, prErr: '', drafts: { system: '', user: '' }, prBusy: false, prMsg: '', prMsgCls: '', histOpen: { system: false, user: false },
      // presets (GET/POST /api/agent/presets, DELETE /api/agent/presets/{name}); avail null = not read yet, false = 404
      presets: [], presetsAvail: null, presetsErr: '', presetName: '', presetBusy: false, presetMsg: '', presetMsgCls: '',
      // Telegram (GET /api/telegram/status, POST pair / unpair / alerts); avail false = 404
      tg: null, tgAvail: null, tgErr: '', tgCode: null, tgBusy: false, tgSeq: 0, tgMsg: '', tgMsgCls: '', tgNow: Date.now(), tgT: null,
      alerts: ALERTS,

      init: function () {
        var self = this;
        try {
          var mq = window.matchMedia(WIDE);
          self.wide = mq.matches;
          var on = function (e) { self.wide = e.matches; };
          if (mq.addEventListener) mq.addEventListener('change', on); else mq.addListener(on);
        } catch (e) { self.wide = false; }
        window.onView(this, 'agent', function () {
          var st = Alpine.store('agent');
          st.loadStatus(); st.loadFleet();
          st.loadCard();
          self.loadPrompts();
          self.loadPresets();
          self.loadTg();
        });
        // Play's "agents here" line and the accessory open the fleet (one market's agents); Record's Clone opens
        // New agent filled in from that agent's last launch
        window.addEventListener('agent-fleet', function (e) { self.toFleet(e.detail && e.detail.market); });
        window.addEventListener('agent-clone', function (e) { if (e.detail && e.detail.agent_id) self.cloneAgent(e.detail.agent_id); });
        // the shown run's new rows reach All activity (a decision, a note, a state change); a quiet minute: a read
        window.addEventListener('agent-custom', function (e) {
          var n = e.detail && e.detail.name;
          if (n === 'decision' || n === 'note' || n === 'session_state' || n === 'overlap_warning') self.jrHead();
        });
        window.addEventListener('agent-selected', function () { self.jrReset(); if (self.activityOn()) self.jrLoad(); });
        setInterval(function () { if (self.activityOn() && document.visibilityState === 'visible') self.jrHead(); }, JR_HEAD_MS);
      },
      st: function () { return Alpine.store('agent'); },
      phone: function () { return this.st().pc === false; },
      goVenue: function (x) {
        leaveTo('#play');
        Alpine.store('app').view = 'play';
        window.dispatchEvent(new CustomEvent('pl-venue', { detail: x }));
      },
      goPerps: function () { leaveTo('#perps'); window.perpsOpen(); },

      // ----- pages and screens -----
      go: function (page) {
        if (PAGES.indexOf(page) < 0) throw new Error('agent: unknown page ' + page);
        this.page = page; this.screen = 'home';
        window.scrollTo(0, 0);
      },
      toFleet: function (market) {
        if (market != null && !this.marketKnown(market)) { this.st().say('There is no market "' + market + '" in this app; every agent is shown.', 'red'); market = null; }
        this.mkt = market || null;
        this.st().unselect();
        this.go('fleet');
      },
      openAgent: function (agentId, sid) {
        this.st().select(agentId, sid);
        this.filter = 'trades';
        this.jrReset();
        this.go('agent');
        if (this.activityOn()) this.jrLoad();
      },
      openRow: function (r) { if (r && r.agent_id) this.openAgent(r.agent_id, r.id); },
      openAudit: function () { this.st().unselect(); this.filter = 'everything'; this.jrReset(); this.go('audit'); this.jrLoad(); },
      openLimits: function () { this.glFill(); this.go('limits'); },
      // 'fleet' | 'live' (running or paused) | 'ended' (its last run) | 'new' | 'limits' | 'audit' | '' (not read yet)
      mode: function () {
        var st = this.st();
        if (!st.enabled || !st.status) return '';
        if (this.page === 'agent') return st.session() ? (st.sessionLive() ? 'live' : 'ended') : '';
        return this.page;
      },
      wideLive: function () { return this.wide && this.mode() === 'live'; },
      open: function (name) {
        if (SCREENS.indexOf(name) < 0) throw new Error('agent: unknown screen ' + name);
        this.screen = name;
        if (name === 'telegram') this.loadTg();
        if (name === 'score') this.st().loadCard();
        if (name === 'activity') this.jrLoad();
        if (!this.wideLive()) window.scrollTo(0, 0);
      },
      back: function () { this.screen = 'home'; window.scrollTo(0, 0); },
      backWord: function () {
        if (this.page === 'agent') { var s = this.st().session(); return '‹ ' + ((s && s.name) || 'Agent'); }
        return '‹ Agents';
      },
      sheet: function (name) { return this.mode() !== '' && this.screen === name; },
      mainOn: function () { return SHEETS.indexOf(this.screen) < 0 && (this.screen === 'home' || this.wideLive()); },
      runOn: function () { return this.mode() === 'live' || this.mode() === 'ended'; },
      activityOn: function () {
        if (this.mode() === 'audit') return this.screen === 'home';
        return this.runOn() && (this.wideLive() || this.screen === 'activity');
      },
      scoreOn: function () {
        if (this.page !== 'agent') return this.mode() !== '' && this.screen === 'score';
        return this.runOn() && (this.wideLive() || this.screen === 'score');
      },
      setFilter: function (f) {
        if (FILTERS.indexOf(f) < 0) throw new Error('agent: unknown filter ' + f);
        if (f === this.filter && this.jr.loaded) return;
        this.filter = f;
        this.jrReset();
        if (this.activityOn()) this.jrLoad();
      },
      showRun: function (id) {
        this.setFilter('thinking');
        this.$nextTick(function () {
          var el = document.querySelector('#ag-feed [data-run="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
          if (el) el.scrollIntoView({ block: 'start' });
        });
      },
      openActivity: function (f) { this.setFilter(f || 'trades'); this.open('activity'); },
      titleText: function () {
        var m = this.mode(), s = this.st().session();
        if (m === 'live' || m === 'ended') return (s && s.name) || 'Agent';
        if (m === 'new') return 'New agent';
        if (m === 'limits') return 'Fleet limits';
        if (m === 'audit') return 'All agents';
        return 'Agents';
      },
      subText: function () {
        var m = this.mode(), s = this.st().session();
        if ((m === 'live' || m === 'ended') && s) return (s.kind === 'external' ? 'External agent (MCP)' : 'In-app agent') + ' · ' + this.levelWord() + ' · ' + this.marketsWord();
        if (m === 'audit') return 'Every agent’s activity, newest first';
        return '';
      },

      // ----- the fleet: the summary card, "Needs you", the agents (by market when there are many) -----
      sum: function () { return this.st().summary(); },
      marketKnown: function (k) { return !!this.venue(k); },
      liveRows: function () { var m = this.mkt; return this.st().fleetLive().filter(function (r) { return !m || r.market === m; }); },
      needRows: function () { return this.liveRows().filter(function (r) { return (r.needs_you || []).length; }); },
      okRows: function () { return this.liveRows().filter(function (r) { return !(r.needs_you || []).length; }); },
      grouped: function () { return !this.mkt && this.st().fleetLive().length > GROUP_AT; },
      byMarket: function () { var s = this.sum(); return (s && s.by_market) || []; },
      normalLine: function () {
        var s = this.sum(), n = this.okRows().length;
        if (!s) return '';
        var bits = [n + ' running normally'];
        if (s.best) bits.push('Best: ' + s.best.name + ' ' + signedUsd(s.best.net));
        if (s.worst && (!s.best || s.worst.id !== s.best.id)) bits.push('Worst: ' + s.worst.name + ' ' + signedUsd(s.worst.net));
        return bits.join(' · ') + '.';
      },
      // agents not running (unarchived, with a run before): tap for their last run
      idleAgents: function () {
        var m = this.mkt;
        return (this.st().agents || []).filter(function (a) { return !a.running_session_id && a.last_run && (!m || a.market === m); });
      },
      idleSub: function (a) {
        var r = a.last_run || {};
        return 'Not running · last run ' + (ok(r.ended_ts) ? 'ended ' + fmt.hm(r.ended_ts - this.st().skewMs) + ' · ' : '') +
          (ENDED_BY[r.end_reason] || r.end_reason || 'ended') + ' · ' + plural(a.runs || 0, 'run');
      },
      needSub: function (r) { var n = r.needs_you || []; return n.length ? n.map(function (x) { return x.text; }).join(' ') : r.line; },
      needRight: function (r) { return (r.needs_you || []).some(function (x) { return x.code === 'proposal'; }) ? 'Review' : signedUsd(r.net); },
      dotCls: function (r) { return r.state === 'paused' || (r.needs_you || []).length ? 'is-warn' : r.state === 'ended' ? 'is-off' : ''; },
      netOf: function (x) { return signedUsd(x); },
      clsOf: function (x) { return netCls(x); },
      // the row's tiny trend: cumulative net after each closed trade (the row's spark, from $0)
      sparkPath: function (pts) {
        var p = [0].concat((pts || []).filter(ok));
        if (p.length < 2) return 'M0 9 L56 9';
        var lo = Math.min.apply(null, p), hi = Math.max.apply(null, p), span = hi - lo || 1;
        return p.map(function (v, i) {
          return (i ? 'L' : 'M') + (56 * i / (p.length - 1)).toFixed(1) + ' ' + (16 - 14 * (v - lo) / span).toFixed(1);
        }).join(' ');
      },
      sparkCls: function (r) { var n = r.net; return !ok(n) || Math.abs(n) < 0.005 ? 'is-flat' : n > 0 ? 'is-up' : 'is-down'; },
      sparkAria: function (r) { var n = (r.spark || []).length; return n ? 'Trend over ' + plural(n, 'closed trade') + ', now ' + signedUsd(r.net) : 'No closed trade yet'; },
      heroNet: function () { var s = this.sum(); return s && s.today ? s.today.net : null; },
      heroSub: function () { var n = this.st().fleetCount(); return 'today · ' + plural(n, 'agent'); },
      tileWin: function () { var s = this.sum(); return s ? String(s.winning) : '—'; },
      tileOf: function () { var s = this.sum(); return s ? 'of ' + s.count : ''; },
      // budget used = realized loss + worst case at risk; a gain lowers it below $0, shown as $0.00 (the gain is in net)
      tileBudget: function () { var s = this.sum(); return s ? usd(Math.max(0, s.budget.used)) : '—'; },
      tileBudgetOf: function () { var s = this.sum(); return s ? 'of ' + usd(s.budget.total) : ''; },
      tileApi: function () { var s = this.sum(); return s ? spend(s.api.used) : '—'; },
      tileApiOf: function () { var s = this.sum(); return s ? (s.api.cap != null ? 'of ' + usd(s.api.cap) : 'no cap (external)') : ''; },
      mktLabel: function (k) { return this.venueLabel(k); },
      limitsVal: function () {
        var g = this.st().status && this.st().status.global_limits, l = (g && g.limits) || {};
        var bits = [];
        if (ok(l.max_loss_day)) bits.push(usd(l.max_loss_day) + ' loss');
        if (ok(l.api_day)) bits.push(usd(l.api_day) + ' API');
        return bits.length ? bits.join(' · ') + ' a day' : 'off';
      },

      // ----- the agent's run: status card, tiles, positions, Latest -----
      levelWord: function () {
        var s = this.st().session();
        if (!s) return '';
        if (s.kind === 'external') return 'External (MCP)';
        return s.limits && s.limits.level === 'assist' ? 'Assist' : 'Auto';
      },
      marketsWord: function () {
        var s = this.st().session(), k = s && (s.market || (s.limits && s.limits.market));
        return k ? this.venueLabel(k) : '—';
      },
      alsoLine: function () {
        var s = this.st().session(), a = (s && s.also_traded_by) || [];
        return a.length ? 'Also traded by ' + a.map(function (x) { return x.name; }).join(', ') + '. Each agent touches only its own positions.' : '';
      },
      settingsVal: function () {
        var s = this.st().session(), n = (s && s.limits && s.limits.rules && s.limits.rules.length) || 0;
        return this.levelWord() + (n ? ' · ' + n + (n === 1 ? ' rule' : ' rules') : '');
      },
      stateTitle: function () { var s = this.st().session(); return s ? cap(s.state) : ''; },
      budgetPct: function () {
        var b = this.st().budget();
        if (!b || !ok(b.used) || !ok(b.limit) || b.limit <= 0) return 0;
        return Math.max(0, Math.min(100, Math.round(100 * b.used / b.limit)));
      },
      // session net = minus the realized net loss of closed positions (budget_parts.realized: gains negative)
      net: function () { var b = this.st().budget(); return b && b.realized != null ? -b.realized : null; },
      netText: function () { var n = this.net(); return n == null ? '—' : (n > 0.004 ? '+' : '') + usd(n); },
      netCls: function () { var n = this.net(); return n == null || Math.abs(n) < 0.005 ? '' : n > 0 ? 'green' : 'red'; },
      traded: function () { return this.st().positions().filter(function (p) { return ok(p.filled) && p.filled > 0; }); },
      tradesWord: function () { var n = this.traded().length; return n + (n === 1 ? ' trade' : ' trades'); },
      // won: a closed position whose realized net (after every fee) is above $0
      wonCount: function () { return this.traded().filter(won).length; },
      leftShort: function () { return this.st().timeLeft().replace(/ left$/, ''); },
      endsClock: function () { var s = this.st().session(); return s && ok(s.ends_ts) ? 'at ' + fmt.hm(s.ends_ts - this.st().skewMs) : ''; },
      apiLine: function () { var st = this.st(), w = st.wakesLine(); return st.spendLine() + (w ? ' · ' + w : ''); },
      posTitle: function (p) {
        var px = ok(p.entry_avg) ? (p.entry_avg <= 1 ? cents(p.entry_avg) : usd(p.entry_avg)) : '—';
        return (ok(p.filled) ? p.filled : '—') + ' @ ' + px;
      },
      posSub: function (p) {
        var d = this.st().ruleByBracket()[p.id], bits = [];
        if (d && d.market) bits.push(this.venueLabel(d.market));
        else if (p.ticker) bits.push(p.ticker);
        if (d && d.rule) bits.push(d.rule);
        bits.push(ok(p.take_profit) ? 'take profit ' + (p.take_profit <= 1 ? cents(p.take_profit) : usd(p.take_profit)) : 'rides to settlement');
        bits.push('worst case ' + usd(p.max_cost));
        if (p.state !== 'open') bits.push(p.state);
        return bits.join(' · ');
      },
      sideCls: function (p) { var x = String(p.side || '').toUpperCase(); return x === 'UP' || x === 'YES' ? 'green' : x === 'DOWN' || x === 'NO' ? 'red' : ''; },
      sideWord: function (p) { return cap(p.side); },

      // ----- the run's rows (Latest; All activity's Trades for the run on screen): trades, decisions; newest first -----
      tradeItem: function (p, d) {
        var paid = paidOf(p), side = cap(p.side), filled = ok(p.filled) && p.filled > 0, title, cls = '', state = String(p.state || '');
        var buy = filled ? 'Bought ' + side + ' ' + usd(paid) + ' at ' + cents(p.entry_avg) : side + ' order: ' + (state || 'state unknown');
        var sub = [], lines = [this.st().posLine(p)];
        if (d && d.rule) sub.push(d.rule);
        if (d && d.market) sub.push(this.venueLabel(d.market)); else if (p.ticker) sub.push(p.ticker);
        if (state === 'closed' && filled) {
          title = (p.exit_reason === 'settled' ? 'Settled ' + side : 'Closed ' + side + (p.exit_reason ? ' (' + p.exit_reason + ')' : '')) + ' · ' + signedUsd(p.realized);
          cls = netCls(p.realized);
          sub.unshift(buy);
          var later = (ok(p.tp_fees) ? p.tp_fees : 0) + (ok(p.exit_fees) ? p.exit_fees : 0);
          lines.push('Paid ' + usd(paid) + (ok(p.entry_fees) ? ' (' + p.filled + ' × ' + cents(p.entry_avg) + ' + ' + usd(p.entry_fees) + ' fees)' : '') +
            ', returned ' + usd(p.proceeds) + (later > 0.00005 ? ' less ' + usd(later) + ' exit fees' : '') + ': net ' + signedUsd(p.realized) + '.');
        } else if (state === 'settling') {
          title = buy; sub.push('settling: result pending');
        } else if (!filled && OPEN_STATES.indexOf(state) < 0) {
          title = side + ' order not filled'; sub.push(state);
        } else {
          title = buy; sub.push(state);
        }
        if (d && d.thesis) lines.push(d.thesis);
        return { key: 't' + p.id, kind: 'trade', ts: p.created_ts, title: title, cls: cls, sub: sub.join(' · '), lines: lines, run: null };
      },
      tradeItems: function () {
        var self = this, m = this.st().ruleByBracket();
        return this.st().positions().map(function (p) { return self.tradeItem(p, m[p.id]); });
      },
      decisionItem: function (d, sid) {
        var st = this.st(), venues = st.status && st.status.venues, nums = [], lines = [];
        if (d.p_estimate != null) nums.push('p ' + pct(d.p_estimate) + (d.p_low != null && d.p_high != null ? ' (' + pct(d.p_low) + '–' + pct(d.p_high) + ')' : '') + (d.p_source ? ' · ' + d.p_source : ''));
        if (d.market_p != null) nums.push('market ' + cents(d.market_p));
        if (d.edge_cents != null) nums.push('edge ' + (d.edge_cents > 0 ? '+' : '') + d.edge_cents + '¢');
        if (d.thesis) lines.push(d.thesis);
        if (d.invalidation) lines.push('Wrong if: ' + d.invalidation);
        if (d.next_check) lines.push('Next check: ' + d.next_check);
        if (d.note) lines.push(d.note + (d.wake_status ? ' (' + d.wake_status + ')' : ''));
        sid = sid || (st.session() && st.session().id);
        // the full run card exists only for the run on screen (its live feed)
        var run = d.wake_id != null && sid && sid === st.selSid ? sid + ':' + d.wake_id : null;
        return { key: 'd' + d._key, kind: 'decision', ts: d.ts, title: decisionWords(d, venues), cls: d.action === 'UNRECORDED' ? 'amber' : '',
          sub: nums.join(' · ') || (d.source === 'rule' ? 'opened by the app' : d.wake_id != null ? 'wake ' + d.wake_id : 'external agent'),
          lines: lines, run: run };
      },
      byTime: function (a) { return a.sort(function (x, y) { return (y.ts || 0) - (x.ts || 0); }); },
      // Latest: trades and the decisions that act (entries, exits, holds), newest 3; a rule's entry shows as its trade row
      latestAll: function () {
        var self = this, pos = {};
        this.st().positions().forEach(function (p) { pos[p.id] = true; });
        var dec = this.st().decisions.filter(function (d) { return REAL.indexOf(d.action) >= 0 && !(d.bracket_id && pos[d.bracket_id]); })
          .map(function (d) { return self.decisionItem(d); });
        return this.byTime(this.tradeItems().concat(dec));
      },
      latest: function () { return this.latestAll().slice(0, 3); },
      emptyText: function (f) {
        if (this.jr.err) return '';
        if (!this.jr.loaded) return 'Reading the activity…';
        return f === 'trades' ? 'No trades yet.' : f === 'decisions' ? 'No decisions recorded yet.' : f === 'thinking' ? 'No wakes recorded yet.' : 'Nothing recorded yet.';
      },
      hm: function (ms) { return ok(ms) ? fmt.hm(ms) : ''; },
      usdOf: function (x) { return usd(x); },
      promptsShort: function () {
        var s = this.st().status, p = s && s.prompts;
        return p ? 'system v' + p.system + ' · user v' + p.user : '';
      },

      // ----- All activity across runs (GET /api/agent/agents/{id}/journal) or every agent's (GET /api/agent/journal/all) -----
      jrUrl: function () {
        if (this.page === 'audit') return '/api/agent/journal/all';
        var a = this.st().sel;
        return a ? '/api/agent/agents/' + encodeURIComponent(a) + '/journal' : null;
      },
      jrKey: function () { return (this.jrUrl() || '') + '|' + this.filter; },
      jrReset: function () { this.jr = { key: this.jrKey(), rows: [], runs: {}, next: null, busy: false, err: '', done: false, loaded: false }; },
      jrQuery: function (before) { return { limit: JR_PAGE, kinds: FILTER_KINDS[this.filter] || null, before_id: before || null }; },
      // the first page (more: the next older one)
      jrLoad: function (more) {
        var self = this, url = self.jrUrl();
        if (!url) return;
        if (self.jr.key !== self.jrKey()) self.jrReset();
        var j = self.jr;
        if (j.busy || (more && (j.done || j.next == null)) || (!more && j.loaded)) return;
        j.busy = true;
        api.get(url, self.jrQuery(more ? j.next : null)).then(function (r) {
          if (self.jr.key !== j.key || self.jr !== j) return;
          if (!r || r.ok === false) { j.err = refusal(r); return; }
          self.jrTake(r);
          j.next = r.next_before_id; j.done = r.next_before_id == null; j.err = ''; j.loaded = true;
        }, function (e) { if (self.jr === j) j.err = errText(e); })['finally'](function () { j.busy = false; });
      },
      // newer rows on top (the first page again, merged by row id; the paging cursor stays)
      jrHead: function () {
        var self = this, url = self.jrUrl(), j = self.jr;
        if (!url || !j.loaded || j.busy || j.key !== self.jrKey()) return;
        api.get(url, self.jrQuery(null)).then(function (r) {
          if (self.jr === j && r && r.ok !== false) self.jrTake(r);
        }, function () { /* the next read tries again */ });
      },
      jrTake: function (r) {
        var j = this.jr, have = {};
        j.rows.forEach(function (x) { have[x.id] = true; });
        var rows = j.rows.concat((r.rows || []).filter(function (x) { return !have[x.id]; }));
        rows.sort(function (a, b) { return b.id - a.id; });
        var runs = Object.assign({}, j.runs);
        (r.runs || []).forEach(function (x) { runs[x.session_id] = x; });
        j.runs = runs; j.rows = rows;
      },
      // infinite scroll: the sentinel under the list asks for the next older page when it comes into view
      watchMore: function (el) {
        var self = this;
        if (!('IntersectionObserver' in window)) return;
        new IntersectionObserver(function (es) {
          if (es.some(function (e) { return e.isIntersecting; }) && self.activityOn() && self.jr.loaded) self.jrLoad(true);
        }, { rootMargin: '200px' }).observe(el);
      },
      // one divider per run: "Run 3 · 22:35–23:41 · ended by restart · +$2.52 · 6 trades" (the audit names the agent)
      runDivider: function (sid) {
        var r = this.jr.runs[sid], skew = this.st().skewMs;
        if (!r) return { key: 'r' + sid, kind: 'run', title: 'Run ' + sid.slice(0, 8), sub: '', lines: [] };
        var live = r.state === 'running' || r.state === 'paused';
        var when = live ? 'since ' + fmt.hm(r.started_ts - skew) : fmt.hm(r.started_ts - skew) + '–' + (ok(r.ended_ts) ? fmt.hm(r.ended_ts - skew) : '?');
        var how = live ? r.state : (ENDED_BY[r.end_reason] || (r.end_reason ? 'ended: ' + r.end_reason : 'ended'));
        var bits = [(this.page === 'audit' ? r.name + ' · ' : '') + 'Run ' + (r.run || '?'), when, how, signedUsd(r.net), plural(r.trades || 0, 'trade')];
        return { key: 'r' + sid, kind: 'run', title: bits.join(' · '), sub: '', lines: [], live: live };
      },
      jrItem: function (row) {
        var d = row.data || {}, f = this.filter, st = this.st(), venues = st.status && st.status.venues, k = row.kind;
        var base = { key: 'j' + row.id, kind: k, ts: row.ts, cls: '', sub: '', lines: [], run: null };
        if (k === 'decision') {
          if (f === 'trades' && TRADE_ACTIONS.indexOf(d.action) < 0) return null;
          return this.decisionItem(Object.assign({ _key: 'j' + row.id, id: row.id, ts: row.ts }, d), row.session_id);
        }
        if (k === 'rule') {
          if (f === 'trades' && !d.ok) return null;
          var mk = d.market ? (venues && venues[d.market] && venues[d.market].label) || d.market : '';
          return Object.assign(base, { title: 'Rule ' + (d.rule || '?') + (d.ok ? ' entry' : ': no entry') + (mk ? ' · ' + mk : ''),
            sub: d.ok ? 'opened by the app' : (d.reason || d.code || ''), cls: d.ok ? '' : 'amber' });
        }
        if (k === 'wake') {
          var trig = (d.trigger || []).map(function (e) { return e && e.type; }).filter(Boolean).join(', ');
          var th = (d.thinking || []).join(' ').replace(/\s+/g, ' ').trim();
          return Object.assign(base, { title: 'Wake ' + (d.wake_id != null ? d.wake_id : '?') + (trig ? ' · ' + trig : ''),
            sub: (d.status || '') + (ok(d.est_usd) ? ' · ' + spend(d.est_usd) + ' est.' : ''),
            lines: th ? ['Thought: ' + clip(th, 300)] : (d.decision ? [clip(d.decision, 300)] : []),
            cls: ['invalid_key', 'unavailable', 'bad_request', 'misconfigured', 'cancelled'].indexOf(d.status) >= 0 ? 'amber' : '' });
        }
        if (k === 'note') return Object.assign(base, { title: 'Your note: “' + (d.text || '') + '”', sub: this.noteState(d) });
        if (k === 'state') return Object.assign(base, { title: d.reason || ('State: ' + (d.state || (d.renamed ? 'renamed ' + d.renamed : '—'))), sub: d.state || '' ,
          cls: d.state === 'paused' ? 'amber' : '' });
        if (k === 'tool') return Object.assign(base, { title: 'Tool ' + (d.name || '?') + (d.ok === false ? ': the app said no' : ''),
          sub: clip(argLine(d.args), 160), cls: d.ok === false ? 'amber' : '' });
        if (k === 'overlap') return Object.assign(base, { title: d.text || 'Opposite sides of one window with another agent', cls: 'amber' });
        if (k === 'rule_status') return Object.assign(base, { title: 'Rule ' + (d.rule || '?') + ' ' + (d.status || ''), sub: d.reason || '' });
        var text = d.text || d.line || d.reason || d.outcome || d.state || '';
        var words = { proposal: 'Proposal', proposal_outcome: 'Proposal answered', proposal_scored: 'Proposal scored', notes: 'Agent notes' };
        return Object.assign(base, { title: (words[k] || k) + (text ? ': ' + clip(text, 200) : ''), sub: d.rule || '' });
      },
      // All activity: run dividers between the rows of each run. Trades shows the run on screen from its positions
      // (exact cost, fees and net per trade), the earlier runs from their journal.
      jrItems: function () {
        var self = this, out = [], prev = null, sid = this.st().selSid, f = this.filter;
        var mine = f === 'trades' && this.page === 'agent' && sid;
        if (mine) {
          var t = this.byTime(this.tradeItems());
          if (t.length || this.jr.runs[sid]) { out.push(this.runDivider(sid)); out = out.concat(t); }
          prev = sid;
        }
        this.jr.rows.forEach(function (row) {
          if (mine && row.session_id === sid) return;
          if (f === 'thinking' && self.page === 'agent' && row.session_id === sid) return;  // the live feed above shows it
          var it = self.jrItem(row);
          if (!it) return;
          // a divider wherever the run changes (the audit interleaves agents: one per stretch, keyed by its first row)
          if (row.session_id !== prev) { out.push(Object.assign(self.runDivider(row.session_id), { key: 'r' + row.session_id + ':' + row.id })); prev = row.session_id; }
          out.push(it);
        });
        return out;
      },
      jrMore: function () { return !!this.jr.next && !this.jr.done; },

      // ----- ended: the run's summary -----
      ranText: function () { var s = this.st().session(); return s ? dur((s.ended_ts || s.ends_ts) - s.started_ts) : '—'; },
      plannedText: function () { var s = this.st().session(); return s ? 'of ' + dur(s.ends_ts - s.started_ts) : ''; },
      apiUsd: function () { var s = this.st().session(), u = (s && s.usage) || {}; return s && s.kind === 'external' ? '—' : spend(ok(u.api_usd) ? u.api_usd : 0); },
      wakesWord: function () { var s = this.st().session(), u = (s && s.usage) || {}; return s && s.kind === 'in_app' ? (u.wakes || 0) + ' wakes' : 'on the external agent'; },
      endedAt: function () { var s = this.st().session(); return s && s.ended_ts ? fmt.hm(s.ended_ts - this.st().skewMs) : '—'; },
      ruleResults: function () {
        var s = this.st().session(), m = this.st().ruleByBracket(), traded = this.traded();
        var rules = (s && s.limits && s.limits.rules) || [];
        return rules.map(function (rid) {
          var mine = traded.filter(function (p) { return m[p.id] && m[p.id].rule === rid; });
          if (!mine.length) return { rule: rid, text: 'no match' };
          var open = mine.filter(function (p) { return p.state !== 'closed'; }).length;
          var net = mine.reduce(function (a, p) { return a + (ok(p.realized) ? p.realized : 0); }, 0);
          return { rule: rid, cls: open === mine.length ? '' : netCls(net),
            text: mine.length + (mine.length === 1 ? ' trade' : ' trades') + ' · ' + mine.filter(won).length + ' won' +
              (open === mine.length ? '' : ' · ' + signedUsd(Math.round(net * 100) / 100)) + (open ? ' · ' + open + ' not closed' : '') };
        });
      },
      handedBack: function () {
        if (!this.st().state || !this.st().state.positions) return '—';
        var n = this.st().openPositions().length;
        return n ? n + ' open, yours in Play' : 'none';
      },
      archivedSel: function () { var a = this.st().sel && this.st().agentOf(this.st().sel); return !a; },

      // ----- New agent: one market first, then name, level, rules, limits, model; a slide starts it -----
      // Prefilled from the newest run's launch (until the user edits the form). Without one no model is picked for
      // the user (the rates differ 40x); the server refuses a launch without one.
      openNew: function () {
        this.launchMsg = ''; this.form.confirm = ''; this.slideX = 0; this.token = '';
        this.go('new');
        if (!this.touched && this.filledFrom !== 'last') this.fillLast();
      },
      newest: function () {
        var best = null;
        (this.st().agents || []).forEach(function (a) { if (a.last_run && (!best || a.last_run.started_ts > best.started_ts)) best = a.last_run; });
        return best;
      },
      fillLast: function () {
        var r = this.newest();
        if (r) this.fillFromRun(r.session_id, 'last', {});
      },
      // fill from a run's launch settings (GET /api/agent/sessions/{id}); who: {agent_id} (run again) or {name}
      fillFromRun: function (sid, from, who) {
        var self = this;
        return api.get('/api/agent/sessions/' + encodeURIComponent(sid)).then(function (r) {
          // the prefill is only until the user edits the form: a read that lands after their first pick is dropped
          if (from === 'last' && self.touched) return;
          var s = r && r.session;
          if (!s) { self.launchMsg = 'That run could not be read.'; return; }
          var lim = s.limits || {};
          self.fillFrom({ kind: s.kind, market: s.market, budget: lim.budget, per_trade: lim.per_trade,
            minutes: ok(s.ends_ts) && ok(s.started_ts) ? Math.round((s.ends_ts - s.started_ts) / 60000) : null,
            spend_cap: lim.spend_cap, model: s.model, rules: lim.rules, one_bet_per_window: lim.one_bet_per_window,
            level: lim.level, proposal_ttl_s: lim.proposal_ttl_s }, from);
          self.setWho(who || {});
        }, function (e) { self.launchMsg = 'The last launch could not be read: ' + errText(e); });
      },
      setWho: function (who) {
        var f = this.form;
        f.agent_id = who.agent_id || '';
        if (who.agent_id) { var a = this.st().agentOf(who.agent_id); f.name = (a && a.name) || ''; this.nameAuto = false; }
        else if (who.name) { f.name = who.name; this.nameAuto = false; }
        else { f.name = this.nextName(f.market); this.nameAuto = true; }
      },
      runAgain: function () {
        var st = this.st();
        if (!st.sel || !st.selSid) return;
        this.go('new');
        this.launchMsg = ''; this.form.confirm = ''; this.slideX = 0;
        this.fillFromRun(st.selSid, 'run', { agent_id: st.sel });
      },
      cloneName: function (name) {
        var taken = {};
        (this.st().agents || []).forEach(function (a) { taken[a.name.toLowerCase()] = true; });
        var base = String(name || 'Agent').slice(0, 33) + ' copy', n = base, i = 2;
        while (taken[n.toLowerCase()]) n = base + ' ' + (i++);
        return n;
      },
      // Record's Clone: the agent's last launch, a new name (a new agent); nothing starts until the slide
      cloneAgent: function (agentId) {
        var self = this, st = this.st();
        var go = function () {
          var a = st.agentOf(agentId);
          if (!a || !a.last_run) { st.say('That agent has no run to clone.', 'red'); return; }
          self.st().unselect();
          self.go('new');
          self.launchMsg = ''; self.form.confirm = ''; self.slideX = 0;
          self.fillFromRun(a.last_run.session_id, 'clone', { name: self.cloneName(a.name) });
        };
        if (st.agents) go(); else st.loadFleet().then(go);
      },
      fillFrom: function (l, from) {
        var f = this.form, str = function (x) { return x == null ? '' : String(x); };
        if (!l || typeof l !== 'object') throw new Error('agent: no launch settings to fill from');
        if (l.kind !== 'in_app' && l.kind !== 'external') throw new Error('agent: unknown kind ' + l.kind);
        if (l.level != null && l.level !== 'auto' && l.level !== 'assist') throw new Error('agent: unknown level ' + l.level);
        f.kind = l.kind;
        f.level = l.level || 'auto';  // a session stored before levels existed was auto
        f.proposal_ttl_s = f.level === 'assist' ? str(l.proposal_ttl_s) : '';
        f.model = str(l.model);
        // one market (a preset saved before agents had one market: its only venue; several: the user picks)
        var m = l.market || (l.venues && l.venues.length === 1 ? l.venues[0] : '');
        f.market = m && this.marketOn(m) ? m : '';
        this.launchMsg = m && !this.marketOn(m) ? this.venueLabel(m) + ' is off in this app; pick a market.' :
          !m && l.venues && l.venues.length > 1 ? 'That launch traded several markets; an agent trades one. Pick its market.' : '';
        var r = {}; (l.rules || []).forEach(function (k) { r[k] = true; }); f.rules = r;
        f.one_bet = l.one_bet_per_window !== false;
        f.budget = str(l.budget); f.per_trade = str(l.per_trade); f.minutes = str(l.minutes); f.spend_cap = str(l.spend_cap);
        f.confirm = ''; f.agent_id = '';
        f.name = this.nextName(f.market); this.nameAuto = true;
        this.filledFrom = from;
        this.touched = false;
      },
      pickMarket: function (k) {
        if (!this.marketOn(k)) return;
        var f = this.form;
        if (f.agent_id && this.st().agentOf(f.agent_id) && this.st().agentOf(f.agent_id).market !== k) { f.agent_id = ''; this.nameAuto = true; }
        f.market = k; this.touched = true; this.launchMsg = '';
        if (this.nameAuto) f.name = this.nextName(k);
      },
      typedName: function () { this.nameAuto = false; this.form.agent_id = ''; this.touched = true; },
      nextName: function (k) { var v = k && this.venue(k); return (v && v.next_agent && v.next_agent.name) || ''; },
      marketOn: function (k) { var v = this.venue(k); return !!(v && v.mode); },
      marketSub: function (k) {
        var v = this.venue(k);
        if (!v) return '';
        if (!v.mode) return 'Off in this app';
        var n = this.marketRules(k).length, bits = [];
        bits.push(n ? plural(n, 'rule') : 'No rules yet: the model’s judgment only');
        bits.push(MODE_WORDS[v.mode] || 'mode: ' + v.mode);
        var also = v.also_traded_by || [];
        if (also.length) bits.push('also traded by ' + also.map(function (x) { return '“' + x.name + '”'; }).join(', '));
        return bits.join(' · ');
      },
      marketRules: function (k) {
        var s = this.st().status;
        return ((s && s.rules) || []).filter(function (x) { return !(x.status && x.status.status === 'retired') && (x.markets || []).indexOf(k) >= 0; });
      },
      setLevel: function (lv) { if (lv !== 'auto' && lv !== 'assist') throw new Error('agent: unknown level ' + lv); this.form.level = lv; this.touched = true; },
      setKind: function (k) { if (k !== 'in_app' && k !== 'external') throw new Error('agent: unknown kind ' + k); this.form.kind = k; this.touched = true; },
      levelFoot: function () {
        return this.form.level === 'assist'
          ? 'Assist: each rule match is proposed to you; only your Approve sends it. An unanswered proposal expires and nothing is sent.'
          : 'Auto: the agent trades on its own inside these limits.';
      },
      filledLine: function () {
        if (this.filledFrom === 'last') return 'Filled in from your last launch.';
        if (this.filledFrom === 'run') return 'Runs this agent again with its last launch’s settings.';
        if (this.filledFrom === 'clone') return 'A clone: the same setup under a new name (a new agent).';
        if (this.filledFrom) return 'Filled in from the preset “' + this.filledFrom + '”.';
        return '';
      },
      nameFoot: function () {
        var f = this.form;
        if (f.agent_id) return 'Runs this agent again: its activity continues from its earlier runs.';
        if (this.nameAuto) return 'The server’s name for the next ' + (f.market ? this.venueLabel(f.market) + ' ' : '') + 'agent; type another to make a new one.';
        return 'A new agent with this name.';
      },
      advancedLine: function () {
        var f = this.form;
        if (f.kind === 'external') return 'External agent (MCP)';
        var c = this.num(f.spend_cap);
        return (f.model || 'no model picked') + (typeof c === 'number' ? ' · ' + usd(c) + ' API cap' : '');
      },
      limitsLine: function () {
        var f = this.form, b = this.num(f.budget), p = this.num(f.per_trade), m = this.num(f.minutes), bits = [];
        if (typeof b === 'number') bits.push(usd(b));
        if (typeof p === 'number') bits.push(usd(p) + ' a trade');
        if (typeof m === 'number') bits.push(m >= 60 && m % 60 === 0 ? (m / 60) + ' h' : m + ' min');
        return bits.join(' · ');
      },
      ruleRecord: function (rid) {
        var c = this.st().card;
        if (!c) return '';
        var w = 0, t = 0;
        (c.rows || []).forEach(function (r) { if (r.rule === rid && r.live) { w += r.live.wins || 0; t += r.live.trades || 0; } });
        return t ? w + ' of ' + t + ' won live' : 'no live trades';
      },
      venue: function (k) { var s = this.st().status; return (s && s.venues && s.venues[k]) || null; },
      venueKeys: function () { var s = this.st().status; return s && s.venues ? Object.keys(s.venues) : []; },
      // the rules that trade the market picked on the form (none picked: none shown)
      ruleList: function () { return this.form.market ? this.marketRules(this.form.market) : []; },
      venueLabel: function (k) { var v = this.venue(k); return (v && v.label) || k; },
      modeText: function (k) {
        var v = this.venue(k);
        if (!v) return 'mode: not known on this screen';
        if (!v.mode) return 'not available in this app';
        return (v.mode === 'prod' ? 'REAL MONEY · ' : '') + 'mode: ' + v.mode;
      },
      modelLine: function (m) {
        var p = m.usd_per_mtok || {};
        return m.id + ' · $' + p.input + ' in / $' + p.output + ' out per MTok';
      },
      keyLine: function () {
        var s = this.st().status, k = s && s.key;
        if (!k) return '—';
        if (!k.present) return 'missing';
        if (k.valid === false) return 'invalid: ' + (k.reason || 'no reason sent');
        if (k.valid === true) return 'present ✓ · valid at ' + fmt.time(k.checked_ts);
        return 'present ✓ · checked with a cheap call at launch';
      },
      dailyLine: function () {
        var s = this.st().status, d = s && s.daily;
        if (!d) return 'The shared daily total is not available from the server.';
        if (d.error) return d.error;
        var used = d.used || {}, parts = Object.keys(used).map(function (k) { return k + ' ' + usd(used[k]); });
        return 'Shared daily limit (you and the agents): ' + (ok(d.per_day) ? usd(d.per_day) + ' per day' : 'no per-day limit set') +
          (parts.length ? ' · used today: ' + parts.join(', ') : '');
      },
      promptsLine: function () {
        var s = this.st().status, p = s && s.prompts;
        return p ? 'Uses system prompt v' + p.system + ' and user prompt v' + p.user + (p.user === 0 ? ' (empty)' : '') + '.' : '';
      },
      num: function (v) { v = String(v == null ? '' : v).trim(); if (!v) return null; var n = Number(v.replace(/^\$/, '')); return isFinite(n) ? n : v; },
      endsAt: function () {
        var m = this.num(this.form.minutes);
        // The server sets the end from its own clock; shown on that clock (status server_ms).
        return typeof m === 'number' && m > 0 ? 'Ends about ' + fmt.hm(Date.now() + this.st().skewMs + m * 60000) + '.' : '';
      },
      // the slide unlocks once a market is picked and START is typed (the server requires the typed START)
      canLaunch: function () { return !this.launching && !this.inAppBlocked() && !!this.form.market && this.marketOn(this.form.market) && this.form.confirm.trim() === 'START'; },
      slideHint: function () {
        if (this.launching) return '';
        if (!this.form.market) return 'Pick a market first.';
        if (this.form.confirm.trim() !== 'START') return 'Type START, then slide.';
        return '';
      },
      inAppBlocked: function () { var s = this.st().status; return this.form.kind === 'in_app' && s && s.in_app && !s.in_app.available ? (s.in_app.reason || 'The in-app agent is unavailable.') : ''; },
      // the launch settings as POST /api/agent/launch takes them (without the typed confirmation)
      launchSpec: function () {
        var self = this, f = self.form;
        var spec = {
          kind: f.kind, market: f.market || null,
          budget: self.num(f.budget), per_trade: self.num(f.per_trade),
          minutes: self.num(f.minutes),  // the server computes the end time from its own clock
          spend_cap: f.kind === 'in_app' ? self.num(f.spend_cap) : null,
          model: f.kind === 'in_app' ? (f.model || null) : null, sell_on_stop: false,
          // only the rules shown for the market picked (a rule of another market is not sent)
          rules: f.kind === 'in_app' ? self.ruleList().map(function (x) { return x.id; }).filter(function (k) { return f.rules[k]; }) : [],
          one_bet_per_window: !!f.one_bet,
          level: f.level,  // required by the server: 'auto' (the default here) or 'assist'
          proposal_ttl_s: f.level === 'assist' ? self.num(f.proposal_ttl_s) : null  // empty: the server's 20 s
        };
        // who runs: this agent again (agent_id), a new agent with the typed name, or the server's default (neither)
        if (f.agent_id) spec.agent_id = f.agent_id;
        else if (!self.nameAuto && f.name.trim()) spec.name = f.name.trim();
        return spec;
      },
      launch: function () {
        var self = this, f = self.form, st = self.st();
        if (!self.canLaunch()) { self.slideX = 0; return; }
        var body = Object.assign(self.launchSpec(), { confirm: f.confirm.trim() });
        self.launching = true; self.launchMsg = ''; self.token = ''; self.copied = '';
        api.post('/api/agent/launch', body).then(function (r) {
          if (r && r.ok) {
            f.confirm = ''; self.touched = false; self.filledFrom = '';
            if (r.token) self.token = r.token;  // shown once, kept only in this page's memory
            st.say(((r.session && r.session.name) || 'The agent') + ' started.', '');
            st.loadFleet().then(function () { self.openAgent(r.agent_id, r.id); });
          } else self.launchMsg = refusal(r);
        }, function (e) { self.launchMsg = 'Launch failed: ' + errText(e); })['finally'](function () {
          self.launching = false; self.slideX = 0; self.sliding = false; st.loadStatus();
        });
      },
      // the slide: a deliberate drag to the end starts the agent (Enter or Space on the knob does too)
      slideMax: function () { var t = this.$refs.agtrack; return t ? Math.max(60, t.clientWidth - 56) : 200; },
      slideStart: function (e) {
        if (!this.canLaunch()) return;
        this.sliding = true; this.sx0 = e.clientX - this.slideX;
        try { e.target.setPointerCapture(e.pointerId); } catch (err) { /* capture not supported */ }
      },
      slideMove: function (e) { if (this.sliding) this.slideX = Math.max(0, Math.min(this.slideMax(), e.clientX - this.sx0)); },
      slideEnd: function () {
        if (!this.sliding) return;
        this.sliding = false;
        if (this.slideX >= this.slideMax() * 0.9) { this.slideX = this.slideMax(); this.launch(); } else this.slideX = 0;
      },
      slideText: function () { return this.launching ? (this.form.kind === 'in_app' ? 'Checking the key…' : 'Starting…') : 'Slide to start agent'; },
      copyToken: function () {
        var self = this;
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(self.token).then(function () { self.copied = 'Copied.'; }, function () { self.copied = 'Copy failed: select the token and copy it by hand.'; });
        } else self.copied = 'Copying is not available here: select the token and copy it by hand.';
      },

      // ----- Fleet limits (KD3: off by default; set on the PC) -----
      glFill: function () {
        var g = this.st().status && this.st().status.global_limits, l = (g && g.limits) || {};
        var str = function (x) { return x == null ? '' : String(x); };
        this.gl.max_loss_day = str(l.max_loss_day); this.gl.api_day = str(l.api_day); this.gl.msg = '';
      },
      glUsed: function () {
        var g = this.st().status && this.st().status.global_limits, u = (g && g.used) || {};
        return 'Used today: lost ' + usd(ok(u.loss_day) ? u.loss_day : 0) + ' · API ' + spend(ok(u.api_day) ? u.api_day : 0) + ' est.';
      },
      glSave: function () {
        var self = this, g = self.gl, body = {}, bad = '';
        if (self.phone() || g.busy) return;
        ['max_loss_day', 'api_day'].forEach(function (k) {
          var v = self.num(g[k]);
          if (typeof v === 'string' || (typeof v === 'number' && v <= 0)) bad = 'Enter a dollar amount above $0, or leave it empty for off.';
          body[k] = v;
        });
        if (bad) { g.msg = bad; g.cls = 'red'; return; }
        g.busy = true; g.msg = '';
        api.post('/api/agent/global_limits', body).then(function (r) {
          if (r && r.ok) { g.msg = 'Saved.'; g.cls = ''; }
          else { g.msg = refusal(r); g.cls = 'red'; }
        }, function (e) { g.msg = 'Not saved: ' + errText(e); g.cls = 'red'; })['finally'](function () {
          g.busy = false; var m = g.msg, c = g.cls;
          self.st().loadStatus().then(function () { self.glFill(); g.msg = m; g.cls = c; }); self.st().loadFleet();
        });
      },

      // ----- presets (the launch settings saved under a name; saving and deleting on the PC only) -----
      loadPresets: function () {
        var self = this;
        return api.get('/api/agent/presets').then(function (r) {
          if (!r || r.ok === false || !Array.isArray(r.presets)) { self.presetsAvail = true; self.presetsErr = refusal(r); return; }
          self.presetsAvail = true; self.presetsErr = ''; self.presets = r.presets;
        }, function (e) {
          if (e && e.status === 404) { self.presetsAvail = false; self.presets = []; return; }
          self.presetsErr = errText(e);
        });
      },
      startFrom: function (p) {
        this.fillFrom(p.launch, p.name);
        this.setWho(this.presetTarget((p.launch || {}).name, this.form.market));
        this.presetMsg = 'Filled in from “' + p.name + '”. Check it, type START, then slide.'; this.presetMsgCls = '';
      },
      // A preset's name template (as the server's preset_target): the idle agent with that name on that market runs
      // again; a name taken elsewhere gets the next free number; no template: the market's default agent.
      presetTarget: function (template, market) {
        if (!template) return {};
        var agents = this.st().agents || [], low = String(template).toLowerCase();
        var same = agents.filter(function (a) { return a.name.toLowerCase() === low; })[0];
        if (same && same.market === market && !same.running_session_id) return { agent_id: same.id };
        if (!same) return { name: template };
        var taken = {}; agents.forEach(function (a) { taken[a.name.toLowerCase()] = true; });
        var n = 2;
        while (taken[(template + ' ' + n).toLowerCase()]) n++;
        return { name: template + ' ' + n };
      },
      // the presets of the market picked first (each preset trades one market)
      presetsShown: function () {
        var m = this.form.market;
        return this.presets.slice().sort(function (a, b) {
          var x = (a.launch || {}).market === m ? 0 : 1, y = (b.launch || {}).market === m ? 0 : 1;
          return x - y || (a.name < b.name ? -1 : 1);
        });
      },
      presetLine: function (p) {
        var l = p.launch || {}, self = this, bits = [];
        bits.push(l.kind === 'external' ? 'External' : l.level === 'assist' ? 'Assist' : 'Auto');
        var mk = l.market ? [l.market] : (l.venues || []);
        if (mk.length) bits.push(mk.map(function (k) { return self.venueLabel(k); }).join(', '));
        if (l.name) bits.push('“' + l.name + '”');
        if (ok(l.budget)) bits.push('max loss ' + usd(l.budget));
        if (l.rules && l.rules.length) bits.push(l.rules.join(', '));
        return bits.join(' · ');
      },
      savePreset: function () {
        var self = this, name = self.presetName.trim();
        if (!name || self.presetBusy || self.phone()) return;
        self.presetBusy = true; self.presetMsg = '';
        api.post('/api/agent/presets', { name: name, launch: self.presetSpec() }).then(function (r) {
          if (r && r.ok) { self.presetMsg = 'Saved as “' + name + '”.'; self.presetMsgCls = ''; self.presetName = ''; }
          else { self.presetMsg = refusal(r); self.presetMsgCls = 'red'; }
        }, function (e) { self.presetMsg = 'Not saved: ' + errText(e); self.presetMsgCls = 'red'; })['finally'](function () { self.presetBusy = false; self.loadPresets(); });
      },
      // a preset keeps the market and a name template (the agent's name), never an agent id
      presetSpec: function () {
        var spec = this.launchSpec(), f = this.form;
        delete spec.agent_id; delete spec.name;
        if (f.agent_id || !this.nameAuto) spec.name = f.name.trim() || null;
        return spec;
      },
      deletePreset: function (p) {
        var self = this;
        if (self.presetBusy || self.phone()) return;
        self.presetBusy = true; self.presetMsg = '';
        del('/api/agent/presets/' + encodeURIComponent(p.name)).then(function (r) {
          if (r && r.ok === false) { self.presetMsg = refusal(r); self.presetMsgCls = 'red'; }
          else { self.presetMsg = 'Deleted “' + p.name + '”.'; self.presetMsgCls = ''; }
        }, function (e) { self.presetMsg = 'Not deleted: ' + errText(e); self.presetMsgCls = 'red'; })['finally'](function () { self.presetBusy = false; self.loadPresets(); });
      },

      // ----- Telegram (alerts to your phone; pairing and alert changes on the PC only) -----
      // a status read applies only if no newer read or change started since it was sent (tgSeq): a slow poll that
      // landed after a change would put the old settings back on screen (and into the next change's value)
      loadTg: function () {
        var self = this, n = ++self.tgSeq;
        return api.get('/api/telegram/status').then(function (r) {
          if (n !== self.tgSeq) return;
          self.tgAvail = true; self.tgErr = ''; self.tg = r;
          if (r && r.paired) self.tgCode = null;
        }, function (e) {
          if (n !== self.tgSeq) return;
          if (e && e.status === 404) { self.tgAvail = false; self.tg = null; return; }
          self.tgErr = errText(e);
        });
      },
      tgLine: function () {
        var t = this.tg;
        if (this.tgAvail === false) return 'Not available';
        if (!t) return this.tgErr ? 'Unknown' : '';
        if (!t.configured) return 'Not set up';
        return t.paired ? 'Paired' : 'Not paired';
      },
      tgPaired: function () { var t = this.tg; return !!(t && t.paired); },
      tgPair: function () {
        var self = this;
        if (self.tgBusy || self.phone()) return;
        self.tgBusy = true; self.tgMsg = ''; self.tgSeq++;
        api.post('/api/telegram/pair').then(function (r) {
          if (r && r.code) {
            self.tgCode = { code: r.code, expires_ts: r.expires_ts };
            self.tgNow = Date.now();
            clearInterval(self.tgT);
            var n = 0;
            self.tgT = setInterval(function () {
              self.tgNow = Date.now();
              var left = self.tgLeftMs();
              if (++n % 3 === 0 && document.visibilityState === 'visible') self.loadTg();  // paired yet?
              if (!self.tgCode || (left != null && left <= 0) || self.tgPaired()) { clearInterval(self.tgT); self.tgT = null; }
            }, 1000);
          } else { self.tgMsg = refusal(r); self.tgMsgCls = 'red'; }
        }, function (e) { self.tgMsg = 'Pairing failed: ' + errText(e); self.tgMsgCls = 'red'; })['finally'](function () { self.tgBusy = false; });
      },
      // the code's expiry on the server's clock (expires_ts) against this device's (agent status server_ms skew)
      tgLeftMs: function () {
        var c = this.tgCode;
        if (!c || !ok(c.expires_ts)) return null;
        return c.expires_ts - (this.tgNow + this.st().skewMs);
      },
      tgLeft: function () {
        var ms = this.tgLeftMs();
        if (ms == null) return 'no expiry sent';
        return ms > 0 ? 'expires in ' + fmt.countdown(ms) : 'expired: pair again for a new code';
      },
      tgUnpair: function () {
        var self = this;
        if (self.tgBusy || self.phone()) return;
        self.tgBusy = true; self.tgMsg = ''; self.tgSeq++;
        api.post('/api/telegram/unpair').then(function (r) {
          if (r && r.ok === false) { self.tgMsg = refusal(r); self.tgMsgCls = 'red'; }
          else { self.tgMsg = 'Unpaired.'; self.tgMsgCls = ''; self.tgCode = null; }
        }, function (e) { self.tgMsg = 'Unpair failed: ' + errText(e); self.tgMsgCls = 'red'; })['finally'](function () { self.tgBusy = false; self.loadTg(); });
      },
      tgAlert: function (kind) { var a = this.tg && this.tg.alerts; return !!(a && a[kind]); },
      tgDailyAt: function () { var t = this.tg; return (t && typeof t.daily_at === 'string') ? t.daily_at : ''; },
      tgSetDaily: function (v) {
        var self = this;
        if (self.tgBusy || self.phone() || !/^([01]\d|2[0-3]):[0-5]\d$/.test(v || '')) return;
        self.tgBusy = true; self.tgMsg = ''; self.tgSeq++;
        api.post('/api/telegram/alerts', { daily_at: v }).then(function (r) {
          if (r && r.ok === false) { self.tgMsg = refusal(r); self.tgMsgCls = 'red'; }
          else { self.tgMsg = 'Daily summary at ' + v + '.'; self.tgMsgCls = ''; }
        }, function (e) { self.tgMsg = 'Not changed: ' + errText(e); self.tgMsgCls = 'red'; })['finally'](function () { self.tgBusy = false; self.loadTg(); });
      },
      tgSetAlert: function (kind, on) {
        var self = this, body = {};
        if (self.tgBusy || self.phone()) return;
        body[kind] = !!on;
        self.tgBusy = true; self.tgMsg = ''; self.tgSeq++;
        api.post('/api/telegram/alerts', body).then(function (r) {
          if (r && r.ok === false) { self.tgMsg = refusal(r); self.tgMsgCls = 'red'; }
        }, function (e) { self.tgMsg = 'Not changed: ' + errText(e); self.tgMsgCls = 'red'; })['finally'](function () { self.tgBusy = false; self.loadTg(); });
      },

      // ----- the rule scorecard (live vs backtest; retire / reactivate on the PC) -----
      cardRules: function () { var c = this.st().card; return (c && c.rules) || []; },
      cardRows: function (rid) { var c = this.st().card; return ((c && c.rows) || []).filter(function (r) { return r.rule === rid; }); },
      isRetired: function (x) { return !!(x && x.status && x.status.status === 'retired'); },
      statusWord: function (x) {
        var st = x.status || {};
        if (st.status !== 'retired') return 'active' + (st.since ? ' · reactivated ' + fmt.dateTime(st.since) : '');
        return 'retired ' + (st.by === 'auto' ? 'automatically' : 'by you') + (st.at ? ' · ' + fmt.dateTime(st.at) : '');
      },
      winLine: function (t) {
        if (!t || !t.trades) return 'no closed trades';
        return t.wins + '/' + t.trades + ' won (' + t.win_pct.toFixed(1) + '%) vs ' + t.break_even_pct.toFixed(1) + '% break-even';
      },
      netLine: function (t) {
        if (!t || !t.trades) return '—';
        return usd(t.net_usd) + ' · ' + (t.net_c >= 0 ? '+' : '−') + Math.abs(t.net_c).toFixed(1) + '¢/contract';
      },
      extraLine: function (t) {
        if (!t) return '';
        var parts = [];
        if (t.trades) parts.push('max drawdown ' + usd(t.max_drawdown_usd));
        if (t.pending) parts.push(t.pending + ' settling');
        if (t.open) parts.push(t.open + ' open');
        if (t.last_trade_ts) parts.push('last ' + fmt.dateTime(t.last_trade_ts));
        return parts.join(' · ');
      },
      btLine: function (b) {
        if (!b) return 'backtest: none';
        var net = ok(b.net_c) ? (b.net_c >= 0 ? '+' : '−') + Math.abs(b.net_c).toFixed(2) + '¢' : 'no net';
        return 'backtest (' + b.row + '): ' + net + (ok(b.n) ? ' · n ' + b.n : '') +
          (ok(b.won_pct) ? ' · won ' + b.won_pct + '%' : '') + ' vs ' + b.break_even_pct + '% break-even';
      },
      barPct: function (x) { var b = x.bar || {}; return b.of ? Math.round(100 * b.trades / b.of) : 0; },
      barLine: function (x) {
        var b = x.bar || {}, t = x.trial || {};
        return b.trades + ' / ' + b.of + ' trades to the review bar' + (t.trades ? ' · trial net ' + usd(t.net_usd) : '') +
          ' (retires at ' + b.of + ' with net ≤ $0, or at once at −$5)';
      },
      phrase: function (rid) { return 'REACTIVATE ' + rid; },
      setRule: function (rid, status) {
        var self = this, st = self.st();
        if (self.phone() || self.rsBusy) return;
        var body = { rule: rid, status: status };
        if (status === 'active') body.confirm = String(self.rsTyped[rid] || '').trim();
        self.rsBusy = rid; self.rsMsg = '';
        api.post('/api/agent/rules/status', body).then(function (r) {
          if (r && r.ok) { self.rsMsg = 'Rule ' + rid + (status === 'retired' ? ' retired.' : ' reactivated.'); self.rsMsgCls = ''; self.rsTyped[rid] = ''; }
          else { self.rsMsg = refusal(r); self.rsMsgCls = 'red'; }
        }, function (e) { self.rsMsg = errText(e); self.rsMsgCls = 'red'; })['finally'](function () { self.rsBusy = ''; st.loadCard(); st.loadStatus(); });
      },

      // ----- note -----
      sendNote: function () {
        var self = this, text = self.noteText.trim(), st = self.st();
        if (!text || self.noteBusy) return;
        self.noteBusy = true; self.noteMsg = '';
        var sid = st.selSid;
        if (!sid) { self.noteBusy = false; return; }
        api.post('/api/agent/sessions/' + encodeURIComponent(sid) + '/note', { text: text }).then(function (r) {
          if (r && r.ok) { self.noteText = ''; if (r.note) st.upsertNote(r.note); }
          else self.noteMsg = refusal(r);
        }, function (e) { self.noteMsg = 'Not sent: ' + errText(e); })['finally'](function () { self.noteBusy = false; });
      },
      noteState: function (n) { return n.state === 'delivered' ? 'delivered ' + fmt.time(n.delivered_ts) : (n.state || 'queued'); },

      // ----- prompts -----
      loadPrompts: function () {
        var self = this;
        api.get('/api/agent/prompts').then(function (r) { self.takePrompts(r, true); self.prErr = ''; }, function (e) { self.prErr = errText(e); });
      },
      takePrompts: function (r, keepDirty) {
        var self = this, was = self.pr;
        self.pr = r;
        ['system', 'user'].forEach(function (l) {
          var dirty = was && self.drafts[l] !== was[l].text;
          if (!keepDirty || !dirty) self.drafts[l] = r[l].text;
        });
      },
      dirty: function (l) { return !!(this.pr && this.drafts[l] !== this.pr[l].text); },
      layerHead: function (l) {
        var p = this.pr && this.pr[l];
        if (!p) return '';
        return (l === 'system' ? 'System prompt' : 'User prompt') + ' v' + p.version + (p.is_default ? ' · default' : '') + (l === 'user' && !p.text ? ' · empty' : '') + (this.dirty(l) ? ' · unsaved changes' : '');
      },
      // the run on screen's own versions (its view's prompt_versions)
      sessionVersions: function () {
        var s = this.page === 'agent' && this.st().session(), v = s && s.prompt_versions;
        return v ? (s.name || 'This agent') + ' runs with system v' + v.system + ' and user v' + v.user + '; edits apply to the next run.' : '';
      },
      prWrite: function (path, body) {
        var self = this;
        if (self.prBusy || self.phone()) return;
        self.prBusy = true; self.prMsg = '';
        api.post(path, body).then(function (r) {
          if (r && r.ok) { self.takePrompts(r, false); self.prMsg = r.changed === false ? 'No change: the text is the same as the current version.' : 'Saved as ' + body.layer + ' v' + r[body.layer].version + '.'; self.prMsgCls = ''; }
          else { self.prMsg = refusal(r); self.prMsgCls = 'red'; }
        }, function (e) { self.prMsg = errText(e); self.prMsgCls = 'red'; })['finally'](function () { self.prBusy = false; self.st().loadStatus(); });
      },
      savePrompt: function (l) { this.prWrite('/api/agent/prompts/save', { layer: l, text: this.drafts[l] }); },
      restorePrompt: function (l, v) {
        if (this.dirty(l) && !window.confirm('Discard your unsaved changes to the ' + l + ' prompt?')) return;
        this.prWrite('/api/agent/prompts/restore', { layer: l, version: v });
      },
      resetPrompt: function (l) {
        if (!window.confirm('Reset the ' + l + ' prompt to the default? This saves the default as a new version' + (this.dirty(l) ? ' and discards your unsaved changes' : '') + '.')) return;
        this.prWrite('/api/agent/prompts/reset', { layer: l });
      },
      discard: function (l) { if (this.pr && window.confirm('Discard your unsaved changes?')) this.drafts[l] = this.pr[l].text; },
      history: function (l) { return (this.pr && this.pr.history && this.pr.history[l]) || []; },
      histLine: function (x) { return 'v' + x.version + ' · ' + (x.created_ts ? fmt.dateTime(x.created_ts) : 'built in') + (x.is_default ? ' · default' : '') + ' · ' + (x.preview ? clip(x.preview, 80) : '(empty)'); },
      current: function (l, x) { return this.pr && this.pr[l].version === x.version; },

      // ----- what index.html reads (Alpine's CSP build evaluates property paths only: test_alpine_csp.py) -----
      // A getter is a value, a method an event handler (it gets the event). Inside an x-for, `this` holds the loop
      // variable too: this.r (a fleet row or a scorecard row), this.a (an idle agent, or an alert [kind, label]),
      // this.m (a market group or a model), this.p (a position or a preset), this.k (a market key), this.x (a rule),
      // this.l (a prompt layer), this.it (an activity row), this.n (a note), this.f (a filter chip), this.w (a warning).
      layers: ['system', 'user'],
      filterChips: [['trades', 'Trades'], ['decisions', 'Decisions'], ['thinking', 'Thinking'], ['everything', 'Everything']],
      noop: function () { /* @submit.prevent / @keydown.enter.prevent only stop the browser's default */ },
      get isAgentView() { return this.$store.app.view === 'agent'; },
      get notFleetPage() { return this.page !== 'fleet'; },
      get plusOn() { return this.page === 'fleet' && !!this.st().enabled; },
      get agentOff() { return this.st().enabled === false; },
      get statusReading() { return this.st().enabled === null && !this.st().statusErr; },
      get statusErrText() { return 'Could not read the agent status: ' + this.st().statusErr; },
      get fleetErrText() { return 'Could not read the agents: ' + this.st().fleetErr; },
      get selErrOn() { return this.page === 'agent' && !!this.st().selErr; },
      get selErrText() { return 'Could not read this agent: ' + this.st().selErr; },
      get selReading() { return this.page === 'agent' && !this.mode() && !this.st().selErr; },
      get settleWarnings() { return this.st().settleWarnings(); },
      get wText() { return this.w.venue_label + ': ' + this.w.message; },
      get msgErrOn() { return !!this.st().msg && this.st().msgCls === 'red'; },
      get pageCols() { return this.wideLive() ? 'ag-3col' : 'ag-1col'; },
      get isFleet() { return this.mode() === 'fleet'; },
      get isLive() { return this.mode() === 'live'; },
      get isEnded() { return this.mode() === 'ended'; },
      get isNew() { return this.mode() === 'new'; },
      get isLimits() { return this.mode() === 'limits'; },
      get notWideLive() { return !this.wideLive(); },
      get notPhone() { return !this.phone(); },
      toFleetAll: function () { this.toFleet(null); },
      openScore: function () { this.open('score'); },
      openPrompts: function () { this.open('prompts'); },
      openTelegram: function () { this.open('telegram'); },
      openNote: function () { this.open('note'); },
      openRunSettings: function () { this.open('settings'); },
      openTrades: function () { this.openActivity('trades'); },
      get sheetNote() { return this.sheet('note'); },
      get sheetSettings() { return this.sheet('settings'); },
      get sheetPrompts() { return this.sheet('prompts'); },
      get sheetTelegram() { return this.sheet('telegram'); },
      // the fleet
      get heroCls() { return netCls(this.heroNet()); },
      get heroText() { return signedUsd(this.heroNet()); },
      get mktOnly() { return 'Only ' + this.mktLabel(this.mkt); },
      showAllMkts: function () { this.mkt = null; },
      pickThisMkt: function () { this.mkt = this.m.market; },
      get needAny() { return this.needRows().length; },
      get needHead() { return 'Needs you · ' + this.needRows().length; },
      openThisRow: function () { this.openRow(this.r); },
      get rNeedAria() { return this.r.name + ': ' + this.needSub(this.r); },
      get rNeedSub() { return this.needSub(this.r); },
      get rNeedRight() { return this.needRight(this.r); },
      get rNeedCls() { return this.needRight(this.r) === 'Review' ? '' : netCls(this.r.net); },
      get okRowsOn() { return !this.grouped() && this.okRows().length > 0; },
      get rDotCls() { return this.dotCls(this.r); },
      get rNetCls() { return netCls(this.r.net); },
      get rNetText() { return signedUsd(this.r.net); },
      get rSparkCls() { return this.sparkCls(this.r); },
      get rSparkAria() { return this.sparkAria(this.r); },
      get rSparkPath() { return this.sparkPath(this.r.spark); },
      get mCountText() { return this.m.count + ' · '; },
      get mNetCls() { return netCls(this.m.net); },
      get mNetText() { return signedUsd(this.m.net); },
      get noLiveOn() { return !!this.st().fleet && !this.liveRows().length; },
      get noLiveText() { return this.mkt ? 'No agent is running on ' + this.mktLabel(this.mkt) + '.' : 'No agent is running.'; },
      get idleAny() { return this.idleAgents().length; },
      openThisAgent: function () { this.openAgent(this.a.id); },
      get aIdleSub() { return this.idleSub(this.a); },
      get aLastCls() { return netCls(this.a.last_run && this.a.last_run.net); },
      get aLastText() { return signedUsd(this.a.last_run && this.a.last_run.net); },
      get tokenOn() { return !!this.token && this.page === 'agent'; },
      hideToken: function () { this.token = ''; this.copied = ''; },
      // one agent: running or paused
      mountProposals: function () {
        var st = this.st();
        window.proposalCards.mount(this.$el, { session: function () { return st.selSid; } });
      },
      get stale() { return this.st().stale(); },
      get staleText() { return this.st().staleText(); },
      get staleCls() { return this.st().stale() ? 'is-stale' : ''; },
      get chipCls() { return 'is-' + this.st().chip().key; },
      get chipWord() { return this.st().chip().word; },
      get chipSince() { return this.st().chip().since; },
      get pillClass() { return this.st().pill().cls; },
      get pillAria() { return this.st().pill().aria; },
      get pillText() { return this.st().pill().text; },
      stopRun: function () { this.st().stop(); },
      get stopAria() { var s = this.st().session(); return 'Stop ' + ((s && s.name) || 'this agent') + ' now'; },
      get nextLine() { return this.st().nextLine(); },
      get lastLine() { return 'Last: ' + this.st().nowLine(); },
      get budgetUsed() { var b = this.st().budget(); return b ? usd(b.used) : '—'; },
      get budgetOf() { var b = this.st().budget(); return b ? 'of ' + usd(b.limit) : ''; },
      get budgetWidth() { return 'width:' + this.budgetPct() + '%'; },
      get isPaused() { var s = this.st().session(); return !!s && s.state === 'paused'; },
      get pauseReason() { var s = this.st().session(); return (s && s.pause_reason) || 'No reason sent.'; },
      get pauseKill() { return this.st().pauseKind() === 'kill'; },
      get pauseMode() { return this.st().pauseKind() === 'mode'; },
      get pauseResumable() { var k = this.st().pauseKind(); return k === 'kill_off' || k === 'other'; },
      get killUnknown() { return this.st().killed === null && !!this.st().killErr; },
      get killUnknownText() { return 'Kill switch state unknown: ' + this.st().killErr; },
      get resumeTypedModel() { return BTC.model(this, 'resumeTyped'); },
      resumeRun: function () { this.st().resume(this.phone() ? this.resumeTyped.trim() : null); },
      get resumeOff() { return this.st().busy || (this.phone() && this.resumeTyped.trim() !== 'RESUME'); },
      get openPositions() { return this.st().openPositions(); },
      get openPosAny() { return this.st().openPositions().length; },
      get pSideCls() { return this.sideCls(this.p); },
      get pSideWord() { return this.sideWord(this.p); },
      get pTitle() { return this.posTitle(this.p); },
      get pSub() { return this.posSub(this.p); },
      get pLine() { return this.st().posLine(this.p); },
      get itKindCls() { return 'is-' + this.it.kind; },
      get itTime() { return this.hm(this.it.ts); },
      get itIsRun() { return this.it.kind === 'run'; },
      get itNotRun() { return this.it.kind !== 'run'; },
      get itLiveCls() { return this.it.live ? 'is-live' : ''; },
      showThisRun: function () { this.showRun(this.it.run); },
      get latestNone() { return !this.latest().length; },
      get latestEmpty() {
        var s = this.st().session();
        return s && s.kind === 'external' ? 'No trades or decisions yet; waiting for the external agent.' : 'No trades or decisions yet. The agent’s wakes are under All activity.';
      },
      get notesVal() { var n = this.st().notes.length; return n ? n + ' sent' : ''; },
      // one agent: its last run ended
      get endWords() { return this.st().endWords(); },
      get tradedCount() { return this.traded().length; },
      get wonText() { return this.wonCount() + ' won'; },
      get ruleResultsAny() { return this.ruleResults().length; },
      get xCls() { return this.x.cls || ''; },
      get notArchived() { return !this.archivedSel(); },
      // New agent
      markTouched: function () { this.touched = true; },
      get presetsBoxOn() { return this.presetsAvail === false || this.presets.length > 0 || !!this.presetsErr; },
      get presetsOff() { return this.presetsAvail === false; },
      get presetsErrText() { return 'Presets could not be read: ' + this.presetsErr; },
      startFromThis: function () { this.startFrom(this.p); },
      deleteThisPreset: function () { this.deletePreset(this.p); },
      get presetStartAria() { return 'Start from preset ' + this.p.name; },
      get presetDeleteAria() { return 'Delete preset ' + this.p.name; },
      get pPresetLine() { return this.presetLine(this.p); },
      get presetsPhone() { return this.presetsAvail !== false && this.phone(); },
      get presetsPc() { return this.presetsAvail !== false && !this.phone(); },
      get presetNameModel() { return BTC.model(this, 'presetName'); },
      get presetSaveOff() { return this.presetBusy || !this.presetName.trim() || this.presetsAvail === null; },
      get kChecked() { return this.form.market === this.k ? 'true' : 'false'; },
      get kOff() { return !this.marketOn(this.k) || (!!this.form.agent_id && this.form.market !== this.k); },
      get kLabel() { return this.venueLabel(this.k); },
      get kSub() { return this.marketSub(this.k); },
      pickThisMarket: function () { this.pickMarket(this.k); },
      get nameModel() { return BTC.model(this.form, 'name'); },
      get nameReadonly() { return !!this.form.agent_id; },
      get levelAssistChecked() { return this.form.level === 'assist' ? 'true' : 'false'; },
      get levelAutoChecked() { return this.form.level === 'auto' ? 'true' : 'false'; },
      setAssist: function () { this.setLevel('assist'); },
      setAuto: function () { this.setLevel('auto'); },
      get isAssist() { return this.form.level === 'assist'; },
      get ttlModel() { return BTC.model(this.form, 'proposal_ttl_s'); },
      get assistFootOn() { return !!this.form.market && this.form.level === 'assist'; },
      get rulesOn() { return !!this.form.market && this.form.kind === 'in_app' && this.ruleList().length > 0; },
      get noRulesOn() { return !!this.form.market && this.form.kind === 'in_app' && !this.ruleList().length; },
      get ruleModel() { return BTC.model(this.form.rules, this.x.id); },
      get xDesc() { return this.x.description || ''; },
      get xRuleRecord() { return this.ruleRecord(this.x.id); },
      get oneBetModel() { return BTC.model(this.form, 'one_bet'); },
      get budgetModel() { return BTC.model(this.form, 'budget'); },
      get perTradeModel() { return BTC.model(this.form, 'per_trade'); },
      get minutesModel() { return BTC.model(this.form, 'minutes'); },
      get endsAtOn() { return !!this.form.market && !!this.endsAt(); },
      get paperOnlyOn() { var s = this.st().status; return !!this.form.market && !!s && s.allow_production === false; },
      get kindInAppChecked() { return this.form.kind === 'in_app' ? 'true' : 'false'; },
      get kindExternalChecked() { return this.form.kind === 'external' ? 'true' : 'false'; },
      setInApp: function () { this.setKind('in_app'); },
      setExternal: function () { this.setKind('external'); },
      get isInApp() { return this.form.kind === 'in_app'; },
      get isExternal() { return this.form.kind === 'external'; },
      get modelPickModel() { return BTC.model(this.form, 'model'); },
      get modelChoices() { var s = this.st().status; return (s && s.models) || []; },
      get mModelLine() { return this.modelLine(this.m); },
      get spendCapModel() { return BTC.model(this.form, 'spend_cap'); },
      get confirmModel() { return BTC.model(this.form, 'confirm'); },
      get slideCls() { return this.canLaunch() || this.launching ? '' : 'off'; },
      get slideFillStyle() { return 'width:' + (this.slideX + 52) + 'px'; },
      get slideKnobStyle() { return 'transform:translateX(' + this.slideX + 'px)'; },
      get cannotLaunch() { return !this.canLaunch(); },
      slideCancel: function () { this.slideX = 0; this.sliding = false; },
      // Fleet limits
      get glLossModel() { return BTC.model(this.gl, 'max_loss_day'); },
      get glApiModel() { return BTC.model(this.gl, 'api_day'); },
      // sheets: note, run settings, prompts, Telegram
      get sessionLive() { return this.st().sessionLive(); },
      get notSessionLive() { return !this.st().sessionLive(); },
      get noteTextModel() { return BTC.model(this, 'noteText'); },
      get noteSendOff() { return this.noteBusy || !this.noteText.trim(); },
      get noteSendText() { return this.noteBusy ? 'Sending…' : 'Send note'; },
      get notesNewest() { return this.st().notes.slice().reverse(); },
      get nQuote() { return '“' + this.n.text + '”'; },
      get nStateCls() { return this.n.state === 'delivered' ? 'green' : 'amber'; },
      get nState() { return this.noteState(this.n); },
      get hasSession() { return !!this.st().session(); },
      get whoTrades() { var s = this.st().session(); return s && s.kind === 'external' ? 'External agent (MCP)' : 'In-app agent'; },
      get levelLine() {
        var s = this.st().session(), ttl = s && s.limits && s.limits.proposal_ttl_s;
        return this.levelWord() + (ttl ? ' · proposals wait ' + ttl + ' s' : '');
      },
      get marketLine() { return this.marketsWord() + ' · ' + this.st().pill().text; },
      get budgetLine() { return this.st().budgetLine(); },
      get perTradeText() { var s = this.st().session(); return s && s.limits ? usd(s.limits.per_trade) : '—'; },
      get endsLine() { return this.endsClock() + ' · ' + this.st().timeLeft(); },
      get rulesLine() { return this.st().rulesLine(); },
      get versionsLine() { return this.sessionVersions() || this.promptsLine(); },
      get prErrText() { return 'Could not read the prompts: ' + this.prErr; },
      get prOff() { return this.phone() || this.prBusy; },
      get lHead() { return this.layerHead(this.l); },
      get lIsUser() { return this.l === 'user'; },
      get draftModel() { return BTC.model(this.drafts, this.l); },
      get lAria() { return this.l === 'system' ? 'System prompt' : 'User prompt'; },
      get lSaveOff() { return this.phone() || this.prBusy || !this.dirty(this.l); },
      get lDirty() { return this.dirty(this.l); },
      get lVersions() { return 'Versions (' + this.history(this.l).length + ')'; },
      get lHistory() { return this.history(this.l); },
      saveThisPrompt: function () { this.savePrompt(this.l); },
      discardThis: function () { this.discard(this.l); },
      resetThisPrompt: function () { this.resetPrompt(this.l); },
      restoreThis: function () { this.restorePrompt(this.l, this.x.version); },
      get xHistLine() { return this.histLine(this.x); },
      get xCurrent() { return !!this.current(this.l, this.x); },
      get xNotCurrent() { return !this.current(this.l, this.x); },
      get tgOff() { return this.tgAvail === false; },
      get tgErrText() { return 'Could not read the Telegram status: ' + this.tgErr; },
      get tgReading() { return this.tgAvail === null && !this.tgErr; },
      get tgUnconfigured() { return !!this.tg && !this.tg.configured; },
      get tgConfigured() { return !!this.tg && !!this.tg.configured; },
      get tgRunning() { return this.tg.running ? 'running' : 'not running'; },
      get tgPairedText() { return this.tg.paired ? (this.tg.user_label || 'yes') : 'not paired'; },
      get tgCanPair() { return !this.phone() && !this.tg.paired; },
      get tgCanUnpair() { return !this.phone() && !!this.tg.paired; },
      get tgPairText() { return this.tgCode ? 'New code' : 'Pair'; },
      get tgCodeOn() { return !!this.tgCode && !this.tg.paired; },
      get tgStartText() { return '/start ' + (this.tgCode ? this.tgCode.code : ''); },
      get tgBotText() { return this.tg.bot_username ? '@' + this.tg.bot_username : 'your bot'; },
      get aKind() { return this.a[0]; },
      get aLabel() { return this.a[1]; },
      get aAria() { return this.a[1] + ' alerts'; },
      get aOn() { return this.tgAlert(this.a[0]); },
      get aOff() { return this.phone() || this.tgBusy || !(this.tg.alerts && this.a[0] in this.tg.alerts); },
      aChange: function (e) { this.tgSetAlert(this.a[0], e.target.checked); },
      get tgDailyOn() { return !!this.tg.alerts && 'daily' in this.tg.alerts; },
      get tgDailyOff() { return this.phone() || this.tgBusy || !this.tgAlert('daily'); },
      tgDailyChange: function (e) { this.tgSetDaily(e.target.value); },
      // All activity
      get feedBackOn() { return !this.wideLive() && this.page === 'agent'; },
      get feedTitle() { return this.page === 'audit' ? 'Every agent, every run' : 'All activity'; },
      get fKey() { return this.f[0]; },
      get fLabel() { return this.f[1]; },
      get fPressed() { return this.filter === this.f[0] ? 'true' : 'false'; },
      setThisFilter: function () { this.setFilter(this.f[0]); },
      get streamNoteOn() { return this.page === 'agent' && !!this.st().streamNote; },
      get thinkingOn() { return this.filter === 'thinking' && this.page === 'agent'; },
      get connectingOn() { var st = this.st(); return !st.everLive && !!st.enabled && st.sessionLive(); },
      get waitingOn() { return this.st().sessionLive() && !this.st().runCount; },
      get waitingText() {
        var s = this.st().session();
        return s && s.kind === 'external' ? 'Waiting for the external agent: no tool call yet.' : 'Waiting for the first event: no wake yet.';
      },
      get noWakesOn() { return this.st().ended() && !this.st().runCount; },
      get jrErrText() { return 'Could not read the activity: ' + this.jr.err; },
      get jrEmptyText() { return this.jrItems().length ? '' : this.emptyText(this.filter); },
      jrOlder: function () { this.jrLoad(true); },
      get jrOlderText() { return this.jr.busy ? 'Reading…' : 'Older ›'; },
      watchMoreHere: function () { this.watchMore(this.$el); },
      // the rule scorecard
      get cardReading() { return !this.st().card && !this.st().cardErr; },
      get cardErrText() { return 'Could not read the scorecard: ' + this.st().cardErr; },
      get xRetired() { return this.isRetired(this.x); },
      get xActive() { return !this.isRetired(this.x); },
      get xRetiredCls() { return this.isRetired(this.x) ? 'is-retired' : ''; },
      get xStatusCls() { return this.isRetired(this.x) ? 'muted' : 'green'; },
      get xStatusWord() { return this.statusWord(this.x); },
      get xReason() { return (this.isRetired(this.x) && this.x.status.reason) || ''; },
      get xBarTrades() { return (this.x.bar || {}).trades; },
      get xBarOf() { return (this.x.bar || {}).of; },
      get xBarAria() { return this.x.rule + ' 30-trade bar'; },
      get xBarStyle() { return 'width:' + this.barPct(this.x) + '%'; },
      get xBarLine() { return this.barLine(this.x); },
      get xRows() { return this.cardRows(this.x.rule); },
      get rKey() { return this.r.rule + ':' + this.r.market; },
      get rLiveWin() { return this.winLine(this.r.live); },
      get rLiveNet() { return this.netLine(this.r.live); },
      get rLiveExtra() { return this.extraLine(this.r.live); },
      get xBt() { return this.btLine(this.x.backtest); },
      get xBtNote() { var b = this.x.backtest; return b ? (b.note == null ? null : b.note) : ''; },
      get rsBusyOn() { return !!this.rsBusy; },
      retireThis: function () { this.setRule(this.x.rule, 'retired'); },
      reactivateThis: function () { this.setRule(this.x.rule, 'active'); },
      get rsTypedModel() { return BTC.model(this.rsTyped, this.x.rule); },
      get xPhraseHint() { return 'Type ' + this.phrase(this.x.rule); },
      get xPhraseAria() { return 'Type ' + this.phrase(this.x.rule) + ' to reactivate'; },
      get reactivateOff() { return !!this.rsBusy || String(this.rsTyped[this.x.rule] || '').trim() !== this.phrase(this.x.rule); }
    };
  };
  // x-data="agentView" (Alpine's CSP build looks components up by name)
  document.addEventListener('alpine:init', function () { Alpine.data('agentView', window.agentView); });
})();
