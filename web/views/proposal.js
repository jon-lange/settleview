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
   Approve is press-and-hold (B6#11), so a stray tap cannot send a real-money order: hold the button (mouse, touch or
   pen; or hold Space / Enter on the focused button) until its fill completes (HOLD_MS). Letting go, sliding off, a
   scroll taking over, the button losing focus or the proposal expiring first cancels and sends nothing; a tap or a
   click only says to hold. Assistive technology that activates the button without a press (a click with no pointer
   or key behind it) gets an explicit two-step confirm instead: the first activation turns the button into
   "Confirm approve" for CONFIRM_MS, the second sends. Plain DOM and listeners (no inline Alpine expressions), so it
   works under Alpine's CSP build.
   A proposal: {id, rule, market_label, side, stake, max_price, btc_stop, line, reason, could_make, could_lose,
   max_cost, created_ts, expires_ts, state, outcome}. Every figure shown is a field from the server; a missing one
   shows "—". The countdown runs on the server's clock (server_ms - Date.now() at the last read).
   Usage: window.proposalCards.mount(element, {session: () => session id}) -> {reload, destroy}. A short vibration greets a new proposal where the device
   supports it (the slide already uses one); there is no other notification. */
(function () {
  'use strict';
  var POLL_MS = 5000, TICK_MS = 250, MSG_MS = 8000;
  var HOLD_MS = 800, CONFIRM_MS = 5000;  // Approve: how long to hold; how long an assistive confirm stays armed
  var HOLD_LABEL = 'Hold to approve', CONFIRM_LABEL = 'Confirm approve';

  function ok(x) { return typeof x === 'number' && isFinite(x); }
  var usd = window.BTC.fmt.usd;  // app.js: the shared money formatter (domain/fmt.py's style)
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

    var noteN = 0;  // ids for each card's note (the Approve button's description)
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
      c.ok = h('button', 'pc-btn pc-ok pc-hold'); c.ok.type = 'button';
      c.fill = h('span', 'pc-fill'); c.fill.setAttribute('aria-hidden', 'true');
      c.label = h('span', 'pc-lbl', HOLD_LABEL);
      c.ok.appendChild(c.fill); c.ok.appendChild(c.label);
      c.skip.addEventListener('click', function () { answer(c, 'skip'); });
      holdable(c);
      two.appendChild(c.skip); two.appendChild(c.ok);
      c.el.appendChild(two);
      var note = h('p', 'pc-note', p.btc_stop == null
        ? 'Hold Approve to send the entry (this rule has no stop: it rides to settlement). Unanswered proposals expire; nothing is sent.'
        : 'Hold Approve to send the entry and its stop. Unanswered proposals expire; nothing is sent.');
      note.id = 'pc-note-' + (++noteN);
      c.ok.setAttribute('aria-describedby', note.id);
      c.el.appendChild(note);
      return c;
    }

    // ----- press-and-hold Approve (see the header) -----
    function holdable(c) {
      var b = c.ok;
      b.addEventListener('pointerdown', function (e) {
        if (e.button !== 0 || b.disabled) return;
        e.preventDefault();  // no text selection or drag: the hold is the gesture
        b.focus({ preventScroll: true });
        try { b.setPointerCapture(e.pointerId); } catch (err) { /* capture is a nicety */ }
        c.pressed = true;  // the click this press ends in belongs to the hold, not to a tap
        startHold(c, 'pointer', e.pointerId);
      });
      b.addEventListener('pointerup', function (e) { if (c.hold && c.hold.id === e.pointerId) cancelHold(c); });
      b.addEventListener('pointercancel', function (e) { if (c.hold && c.hold.id === e.pointerId) cancelHold(c); });
      b.addEventListener('pointermove', function (e) {
        if (!c.hold || c.hold.id !== e.pointerId) return;
        var r = b.getBoundingClientRect();
        if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) cancelHold(c);
      });
      b.addEventListener('keydown', function (e) {
        if (e.key !== ' ' && e.key !== 'Enter') return;
        e.preventDefault();  // no native click: the key must be held
        if (!e.repeat && !b.disabled && !c.hold) startHold(c, 'key', e.key);
      });
      b.addEventListener('keyup', function (e) {
        if (e.key !== ' ' && e.key !== 'Enter') return;
        e.preventDefault();
        if (c.hold && c.hold.id === e.key) cancelHold(c);
      });
      b.addEventListener('blur', function () { if (c.hold) cancelHold(c); });
      b.addEventListener('contextmenu', function (e) { e.preventDefault(); });  // a long press is ours, not a menu
      b.addEventListener('click', function (e) {
        e.preventDefault();
        if (e.detail !== 0) {
          // a mouse or touch click: the press it ends was the hold (already sent or cancelled); one with no press
          // seen here only says to hold
          if (c.pressed) c.pressed = false;
          else if (!b.disabled) say('Hold Approve until it fills; a tap sends nothing.', null);
          return;
        }
        // detail 0: activated with no pointer press or key hold behind it (a screen reader's double tap, voice
        // control): the explicit two-step confirm.
        if (b.disabled || c.hold) return;
        if (c.armed) { disarm(c); answer(c, 'approve'); return; }
        c.armed = setTimeout(function () { disarm(c); }, CONFIRM_MS);
        c.label.textContent = CONFIRM_LABEL;
        say('Activate ' + CONFIRM_LABEL + ' again to send; nothing is sent otherwise.', null);
      });
    }

    function disarm(c) {
      if (c.armed) clearTimeout(c.armed);
      c.armed = null;
      c.label.textContent = HOLD_LABEL;
    }

    function startHold(c, how, id) {
      disarm(c);
      var t0 = performance.now();
      c.hold = { how: how, id: id, done: setTimeout(function () { completeHold(c); }, HOLD_MS), raf: 0 };
      c.ok.classList.add('pc-holding');
      (function frame() {
        if (!c.hold) return;
        c.fill.style.transform = 'scaleX(' + Math.min(1, (performance.now() - t0) / HOLD_MS) + ')';
        c.hold.raf = requestAnimationFrame(frame);
      })();
    }

    function stopHold(c) {
      if (!c.hold) return;
      clearTimeout(c.hold.done); cancelAnimationFrame(c.hold.raf);
      c.hold = null;
      c.ok.classList.remove('pc-holding');
      c.fill.style.transform = '';
    }

    function cancelHold(c) {  // let go before the fill completed: nothing is sent
      stopHold(c);
      say('Released early: nothing was sent. Hold until it fills to approve.', null);
    }

    function completeHold(c) {
      if (!c.hold) return;
      stopHold(c);
      try { if (navigator.vibrate) navigator.vibrate(15); } catch (err) { /* no haptics */ }
      answer(c, 'approve');
    }

    function answer(c, what) {
      var id = c.p.id;
      if (busy[id]) return;
      busy[id] = true;
      stopHold(c); disarm(c);
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
      if (c) { stopHold(c); disarm(c); if (c.el.parentNode) c.el.parentNode.removeChild(c.el); delete cards[id]; }
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
        if (left <= 0 && !busy[id]) { stopHold(c); disarm(c); c.ok.disabled = true; c.left.textContent = 'expired'; }
      });
    }

    function onCustom(ev) {
      var d = ev && ev.detail;
      if (!d || (d.name !== 'proposal_new' && d.name !== 'proposal_resolved')) return;
      var v = d.value || {};
      if (d.name === 'proposal_resolved' && v.id && !busy[v.id]) drop(v.id);
      load();
    }
    function onVisible() { if (document.visibilityState === 'visible') { tick(); load(); } }
    function onSelected() { Object.keys(cards).forEach(function (id) { if (!busy[id]) drop(id); }); load(); }

    window.addEventListener('agent-custom', onCustom);
    window.addEventListener('agent-selected', onSelected);
    document.addEventListener('visibilitychange', onVisible);
    pollT = setInterval(function () { if (document.visibilityState === 'visible') load(); }, POLL_MS);
    tickT = setInterval(function () { if (document.visibilityState === 'visible') tick(); }, TICK_MS);  // hidden: no ticking
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
