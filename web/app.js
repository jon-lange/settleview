/* Core: API client, formatters, the app store, hash router, the tab bar's navigation, toasts and the chrome sizes.
   Sections (the tab bar, design pass 2026-10-31): Play (views/play.js; its Kalshi picker opens Perps, views/perps.js),
   Agent (views/agent.js) and Record (views/play.js recordView). The chart lives in playchart.js. Loaded before Alpine
   (deferred). */
(function () {
  'use strict';

  // Route table: '' and 'play' open Play, 'perps' Kalshi's BTC Perps (a market of Play), 'agent' and 'record' their
  // tabs; any other address opens Play with a one-time note that it is unknown (never a silent redirect).
  var ROUTES = { '': 'play', play: 'play', perps: 'perps', agent: 'agent', record: 'record' };
  // The tab bar's sections: Perps belongs to Play.
  var SECTION = { play: 'play', perps: 'play', agent: 'agent', record: 'record' };
  var PERPS_OPEN_KEY = 'perps.open';  // views/perps.js: Play's last market was BTC Perps (Play's tab reopens it)
  var TOAST_MS = 4000;

  // ---------- formatters ----------
  function pad(n) { return String(n).padStart(2, '0'); }
  var fmt = {
    time: function (ms) {
      if (ms == null) return '—';
      var d = new Date(ms);
      return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    },
    hm: function (ms) {
      if (ms == null) return '—';
      var d = new Date(ms);
      return pad(d.getHours()) + ':' + pad(d.getMinutes());
    },
    dateTime: function (ms) {
      if (ms == null) return '—';
      var d = new Date(ms);
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    },
    countdown: function (ms) {
      if (ms == null) return '—';
      if (ms < 0) ms = 0;
      var s = Math.ceil(ms / 1000);
      var m = Math.floor(s / 60);
      return m + ':' + pad(s % 60);
    }
  };

  // ---------- API client ----------
  function ApiError(status, body, message) {
    this.name = 'ApiError'; this.status = status; this.body = body; this.message = message;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function errorMessage(status, body) {
    if (body && typeof body === 'object') {
      if (typeof body.detail === 'string') return body.detail;
      if (typeof body.error === 'string') return body.error;
      if (body.errors) return 'Some values were not accepted. See the highlighted fields.';
    }
    if (status === 404) return 'This feature is not available on the server yet.';
    return 'The server returned an error (' + status + ').';
  }

  function request(method, path, query, body, timeoutMs) {
    var qs = new URLSearchParams();
    Object.keys(query || {}).forEach(function (k) {
      var v = query[k];
      if (v !== null && v !== undefined && v !== '') qs.append(k, v);
    });
    var url = path + (qs.toString() ? '?' + qs : '');
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, timeoutMs || 10000);
    var opts = { method: method, cache: 'no-store', signal: ctrl.signal, headers: {} };
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    return fetch(url, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
        if (!res.ok) throw new ApiError(res.status, data, errorMessage(res.status, data));
        return data;
      });
    }, function (e) {
      throw new ApiError(0, null, e && e.name === 'AbortError' ? 'The server took too long to answer.' : 'Could not reach the server.');
    }).finally(function () { clearTimeout(timer); });
  }

  var api = {
    get: function (path, query, timeoutMs) { return request('GET', path, query, undefined, timeoutMs); },
    post: function (path, body) { return request('POST', path, {}, body === undefined ? {} : body); }
  };

  // ---------- Alpine wiring ----------
  function parseHash() {
    var raw = location.hash.replace(/^#\/?/, '');
    var parts = raw.split('?');
    var id = parts[0];
    var note = '';
    if (!Object.prototype.hasOwnProperty.call(ROUTES, id)) note = 'There is no "#' + id + '" screen; Play is shown.';
    return { view: ROUTES[id] || 'play', params: new URLSearchParams(parts[1] || ''), note: note };
  }
  // An old or unknown address is shown once: the note appears, and the address bar becomes #play so a reload or a
  // saved bookmark of this page does not show it again.
  function settleHash(r) {
    if (r.note && window.history && history.replaceState) {
      try { history.replaceState(null, '', location.pathname + location.search + '#play'); } catch (e) { /* keep the hash */ }
    }
  }

  document.addEventListener('alpine:init', function () {
    var first = parseHash();
    Alpine.store('app', {
      view: first.view,
      params: first.params,
      routeNote: first.note,   // the one-time "no such screen" note (Play shows it until dismissed)
      announcement: '',
      perpsOff: null,          // Kalshi's BTC Perps switched off on the server (null = not known yet): Play's picker says "off"
      toastText: '', toastCls: '', toastT: null,
      // The tab bar's section for the current view (Perps is Play's).
      section: function () { return SECTION[this.view] || 'play'; },
      // A transient message above the tab bar (a launch, a stop): read out too. cls 'red' for a failure.
      toast: function (text, cls) {
        var self = this;
        clearTimeout(self.toastT);
        self.toastText = text || ''; self.toastCls = cls || '';
        if (text) self.announce(text);
        self.toastT = setTimeout(function () { self.toastText = ''; }, cls === 'red' ? 2 * TOAST_MS : TOAST_MS);
      },
      announce: function (text) {
        // Clear then set so repeated identical text is still read.
        var self = this;
        self.announcement = '';
        setTimeout(function () { self.announcement = text; }, 50);
      }
    });

    settleHash(first);
  });

  document.addEventListener('alpine:init', function () {
    window.addEventListener('hashchange', function () {
      var r = parseHash();
      var s = Alpine.store('app');
      s.view = r.view; s.params = r.params; s.routeNote = r.note;
      settleHash(r);
    });
  });

  // The tab bar: Play reopens the last Play market (BTC Perps when that was it); the address follows (#play, #perps,
  // #agent, #record) so a reload or a bookmark comes back to the same tab.
  window.navTo = function (section) {
    var view;
    if (section === 'play') view = load(PERPS_OPEN_KEY) === '1' ? 'perps' : 'play';
    else if (section === 'agent' || section === 'record') view = section;
    else throw new Error('navTo: unknown section ' + section);
    try { history.replaceState(null, '', location.pathname + location.search + '#' + view); } catch (e) { /* keep the hash */ }
    var s = Alpine.store('app');
    s.view = view; s.routeNote = '';
    window.scrollTo(0, 0);
  };
  function load(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }

  // The chrome's heights as CSS variables, so content scrolls clear of it: --chrome-h (the tab bar with its accessory)
  // and --dock-h (the order dock above it, when it is fixed: the phone layout).
  function measure() {
    var root = document.documentElement, bar = document.getElementById('tabbar'), dock = 0;
    if (bar) root.style.setProperty('--chrome-h', bar.offsetHeight + 'px');
    document.querySelectorAll('.pl-bottom').forEach(function (d) {
      if (d.getClientRects().length && getComputedStyle(d).position === 'fixed') dock = Math.max(dock, d.offsetHeight);
    });
    root.style.setProperty('--dock-h', dock + 'px');
    var top = 0;
    document.querySelectorAll('.pl-top').forEach(function (t) {
      if (t.offsetParent !== null && getComputedStyle(t).position === 'sticky') top = Math.max(top, t.offsetHeight);
    });
    root.style.setProperty('--top-h', top + 'px');
  }
  document.addEventListener('alpine:initialized', function () {
    measure();
    if (window.ResizeObserver) {
      var ro = new ResizeObserver(measure);
      var bar = document.getElementById('tabbar');
      if (bar) ro.observe(bar);
      document.querySelectorAll('.pl-bottom, .pl-top').forEach(function (d) { ro.observe(d); });
    }
    window.addEventListener('resize', measure);
    Alpine.effect(function () { Alpine.store('app').view; setTimeout(measure, 0); });
    // Play's Kalshi picker names BTC Perps "off" while the server has perps switched off: one read of the perps view
    // (the Perps screen keeps it current from then on).
    api.get('/api/perps/view').then(function (r) {
      var s = Alpine.store('app');
      if (r && typeof r.enabled === 'boolean' && s.perpsOff == null) s.perpsOff = !r.enabled;
    }, function () { /* unknown: the picker just says BTC Perps */ });
  });

  // A 150 ms flash on a number that changed (a fill, a settle): elements marked data-flash. Off under Reduce Motion.
  (function () {
    var last = new WeakMap();
    function reduced() { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } }
    var mo = new MutationObserver(function (muts) {
      if (reduced()) return;
      muts.forEach(function (m) {
        var el = m.target.nodeType === 3 ? m.target.parentElement : m.target;
        if (!el || !el.hasAttribute || !el.hasAttribute('data-flash')) return;
        var t = el.textContent, was = last.get(el);
        last.set(el, t);
        if (was == null || was === t || was === '—' || t === '—') return;
        el.classList.remove('nv-flash'); void el.offsetWidth; el.classList.add('nv-flash');
      });
    });
    document.addEventListener('alpine:initialized', function () {
      mo.observe(document.body, { subtree: true, characterData: true, childList: true });
    });
  })();

  // Calls `load` the first time a view becomes visible and whenever it is re-entered.
  window.onView = function (component, id, load) {
    var s = Alpine.store('app');
    var was = false;
    Alpine.effect(function () {
      var on = s.view === id;
      var p = s.params && s.params.toString();
      if (on) { var firstTime = !was; setTimeout(function () { load.call(component, firstTime, p); }, 0); }
      was = on;
    });
  };

  window.BTC = { api: api, fmt: fmt };
})();
