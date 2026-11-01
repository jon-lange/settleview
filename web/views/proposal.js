/* Assist proposals: the "Ask Settleview proposes" card (the copilot wireframe's Assist column).
   An assist agent session never places a rule's match: it proposes the whole ticket and only the user's Approve sends
   it (entry + its BTC stop in one bracket, through the slide's prepare -> order gate -> approve path, with a person
   at the screen). Skip, or no answer before the countdown ends, sends nothing.
   Data (agent_routes.py):
   - GET /api/agent/proposals?session_id= -> {ok, session_id, pending: [proposal], recent, server_ms}: the agent on
     screen's run (`opts.session()`, views/agent.js; none: nothing is shown), read on mount, on every proposal event,
     when another run is shown, and every POLL_MS while the page is visible (a fallback for a closed feed).
   - CUSTOM "proposal_new" / "proposal_resolved" on that run's feed (EventSource /api/agent/sessions/{id}/stream,
     opened by views/agent.js, which re-dispatches each CUSTOM event as window event 'agent-custom' {detail: {name,
     value}}).
   - POST /api/agent/proposals/approve {id} and /skip {id}: the answer, or a refusal {ok: false, code, reason}.
   A proposal: {id, rule, market_label, side, stake, max_price, btc_stop, line, reason, could_make, could_lose,
   max_cost, created_ts, expires_ts, state, outcome}. Every figure shown is a field from the server; a missing one
   shows "—". The countdown runs on the server's clock (server_ms - Date.now() at the last read).
   Usage: window.proposalCards.mount(element, {session: () => session id}) -> {reload, destroy}. A short vibration greets a new proposal where the device
   supports it (the slide already uses one); there is no other notification. */
(function () {
  'use strict';
  var POLL_MS = 5000, TICK_MS = 250, MSG_MS = 8000;

  function ok(x) { return typeof x === 'number' && isFinite(x); }
  function usd(x) { return ok(x) ? (x < 0 ? '−$' : '$') + Math.abs(x).toFixed(2) : '—'; }
  function h(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
  }
  function mmss(ms) {
    var s = Math.max(0, Math.ceil(ms / 1000));
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
  function reasonOf(e) {
    if (e && e.body && typeof e.body === 'object' && e.body.reason) return e.body.reason;
    return (e && e.message) || String(e);
  }

  // Ask Settleview's identifier: the logo mark (index.html's #sv-mark symbol), in the AI colour.
  function mark() {
    var NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg'), use = document.createElementNS(NS, 'use');
    svg.setAttribute('class', 'pc-glyph'); svg.setAttribute('width', '16'); svg.setAttribute('height', '16');
    svg.setAttribute('aria-hidden', 'true'); use.setAttribute('href', '#sv-mark'); svg.appendChild(use);
    return svg;
  }

  function mount(root, opts) {
    if (!root) throw new Error('proposalCards.mount needs an element');
    var sessionOf = opts && opts.session;
    if (typeof sessionOf !== 'function') throw new Error('proposalCards.mount needs opts.session (the run shown)');
    var api = window.BTC && window.BTC.api;
    if (!api) throw new Error('proposalCards needs window.BTC.api (app.js)');
    var wrap = h('section', 'pc-wrap');
    wrap.setAttribute('aria-label', 'Assist proposals');
    var list = h('div', 'pc-list');
    var status = h('p', 'pc-status');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    wrap.appendChild(list); wrap.appendChild(status);
    root.appendChild(wrap);

    var cards = {}, skew = 0, busy = {}, alive = true, seen = {}, msgT = null, pollT = null, tickT = null;
    var asked = 0, applied = 0;  // read numbers: a read answered after a newer one was shown is dropped

    function say(text, cls) {
      status.textContent = text || '';
      status.className = 'pc-status' + (cls ? ' ' + cls : '');
      clearTimeout(msgT);
      if (text) msgT = setTimeout(function () { status.textContent = ''; }, MSG_MS);
    }

    function card(p) {
      var c = { p: p, el: h('article', 'pc-card') };
      c.el.setAttribute('aria-label', 'Ask Settleview proposes ' + (p.line || ''));
      var t = h('div', 'pc-t');
      t.appendChild(mark());
      t.appendChild(h('span', null, 'Ask Settleview proposes'));
      t.appendChild(h('span', 'pc-sp'));
      c.left = h('span', 'pc-left num', '—');
      t.appendChild(c.left);
      c.el.appendChild(t);
      var ttl = h('div', 'pc-ttl'); c.bar = h('span'); ttl.appendChild(c.bar);
      ttl.setAttribute('aria-hidden', 'true');
      c.el.appendChild(ttl);
      c.el.appendChild(h('span', 'pc-o num', p.line || '—'));
      c.el.appendChild(h('span', 'pc-why', p.reason || ''));
      var nums = h('div', 'pc-nums num');
      nums.appendChild(h('span', 'pc-make', 'Could make ' + (ok(p.could_make) && p.could_make >= 0 ? '+' : '') + usd(p.could_make)));
      nums.appendChild(h('span', 'pc-lose', 'Could lose ' + usd(p.could_lose)));
      nums.appendChild(h('span', 'pc-worst', 'Worst case ' + usd(p.max_cost)));
      c.el.appendChild(nums);
      var two = h('div', 'pc-two');
      c.skip = h('button', 'pc-btn', 'Skip'); c.skip.type = 'button';
      c.ok = h('button', 'pc-btn pc-ok', 'Approve'); c.ok.type = 'button';
      c.skip.addEventListener('click', function () { answer(c, 'skip'); });
      c.ok.addEventListener('click', function () { answer(c, 'approve'); });
      two.appendChild(c.skip); two.appendChild(c.ok);
      c.el.appendChild(two);
      c.el.appendChild(h('p', 'pc-note', p.btc_stop == null
        ? 'One Approve sends the entry (this rule has no stop: it rides to settlement). Unanswered proposals expire; nothing is sent.'
        : 'One Approve sends the entry and its stop. Unanswered proposals expire; nothing is sent.'));
      return c;
    }

    function answer(c, what) {
      var id = c.p.id;
      if (busy[id]) return;
      busy[id] = true;
      c.ok.disabled = c.skip.disabled = true;
      c.el.setAttribute('aria-busy', 'true');
      api.post('/api/agent/proposals/' + what, { id: id }).then(function (r) {
        if (r && r.ok) say(what === 'approve' ? 'Approved: the entry and its stop were sent.' : 'Skipped; nothing was sent.', 'pc-good');
        else say((r && r.reason) || 'The server refused without a reason.', 'pc-bad');
      }, function (e) { say((what === 'approve' ? 'Approve' : 'Skip') + ' failed: ' + reasonOf(e), 'pc-bad'); })
        .then(function () { delete busy[id]; drop(id); load(); });
    }

    function drop(id) {
      var c = cards[id];
      if (c) { if (c.el.parentNode) c.el.parentNode.removeChild(c.el); delete cards[id]; }
    }

    function show(pending) {
      var keep = {}, st = window.Alpine && Alpine.store('agent');
      // the Agent's state chip says "Waiting for you" while one waits (its time: the oldest proposal's)
      if (st) st.pending = pending.map(function (p) { return { id: p.id, created_ts: p.created_ts }; });
      pending.forEach(function (p) {
        keep[p.id] = true;
        if (!cards[p.id]) {
          cards[p.id] = card(p);
          list.insertBefore(cards[p.id].el, list.firstChild);
          if (!seen[p.id]) {
            seen[p.id] = true;
            try { if (navigator.vibrate) navigator.vibrate(30); } catch (err) { /* no haptics */ }
          }
        }
      });
      Object.keys(cards).forEach(function (id) { if (!keep[id] && !busy[id]) drop(id); });
      tick();
    }

    function load() {
      if (!alive) return Promise.resolve();
      var sid = sessionOf(), n = ++asked;
      if (!sid) { applied = n; show([]); return Promise.resolve(); }
      // reads overlap (mount, every proposal event, the poll); an older one that answers last would drop a newer
      // proposal's card until the next poll, so only a read newer than the one shown is applied
      return api.get('/api/agent/proposals', { session_id: sid }).then(function (r) {
        if (!alive || sid !== sessionOf() || n < applied) return;
        applied = n;
        if (r && r.code === 'assist_unavailable') { clearInterval(pollT); return; }  // this app has no Assist: quiet
        if (!r || !r.ok) { say((r && r.reason) || 'Proposals could not be read.', 'pc-bad'); return; }
        if (ok(r.server_ms)) skew = r.server_ms - Date.now();
        show(r.pending || []);
      }, function (e) { if (alive && e && e.status !== 404) say('Proposals could not be read: ' + reasonOf(e), 'pc-bad'); });
    }

    function tick() {
      var now = Date.now() + skew;
      Object.keys(cards).forEach(function (id) {
        var c = cards[id], p = c.p;
        if (!ok(p.expires_ts) || !ok(p.created_ts)) { c.left.textContent = '—'; return; }
        var left = p.expires_ts - now, span = Math.max(1, p.expires_ts - p.created_ts);
        c.left.textContent = mmss(left);
        c.bar.style.width = Math.max(0, Math.min(100, left / span * 100)) + '%';
        if (left <= 0 && !busy[id]) { c.ok.disabled = true; c.left.textContent = 'expired'; }
      });
    }

    function onCustom(ev) {
      var d = ev && ev.detail;
      if (!d || (d.name !== 'proposal_new' && d.name !== 'proposal_resolved')) return;
      var v = d.value || {};
      if (d.name === 'proposal_resolved' && v.id && !busy[v.id]) drop(v.id);
      load();
    }
    function onVisible() { if (document.visibilityState === 'visible') load(); }
    function onSelected() { Object.keys(cards).forEach(function (id) { if (!busy[id]) drop(id); }); load(); }

    window.addEventListener('agent-custom', onCustom);
    window.addEventListener('agent-selected', onSelected);
    document.addEventListener('visibilitychange', onVisible);
    pollT = setInterval(function () { if (document.visibilityState === 'visible') load(); }, POLL_MS);
    tickT = setInterval(tick, TICK_MS);
    load();

    return {
      reload: load,
      destroy: function () {
        alive = false;
        clearInterval(pollT); clearInterval(tickT); clearTimeout(msgT);
        window.removeEventListener('agent-custom', onCustom);
        window.removeEventListener('agent-selected', onSelected);
        document.removeEventListener('visibilitychange', onVisible);
        if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
      }
    };
  }

  window.proposalCards = { mount: mount };
})();
