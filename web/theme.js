/* Theme, before first paint (no flash): 'light' / 'dark' set data-theme on <html>; System (no key) leaves it off so
   the CSS follows prefers-color-scheme. A stored value that is not one of the three is NOT rewritten: System is used
   and Settings shows that the saved value was unrecognised. Changes dispatch 'btc-theme' (charts re-read tokens).
   Loaded from <head> without defer, so it runs before first paint. A file, not an inline script: the CSP
   (adapters/http/access.py) runs only this origin's script files. */
(function () {
  var KEY = 'btc.theme', MODES = ['system', 'light', 'dark'], s = null, mq = null;
  try { s = window.localStorage.getItem(KEY); } catch (e) { s = null; }
  try { mq = window.matchMedia('(prefers-color-scheme: dark)'); } catch (e) { mq = null; }
  var unknown = s != null && MODES.indexOf(s) < 0 ? s : null;
  function apply(m) {
    var r = document.documentElement, meta = document.querySelector('meta[name="theme-color"]');
    if (m === 'light' || m === 'dark') r.setAttribute('data-theme', m); else r.removeAttribute('data-theme');
    var dark = m === 'dark' || (m === 'system' && !!(mq && mq.matches));
    if (meta) meta.setAttribute('content', dark ? '#000000' : '#F2F2F7');
  }
  var T = window.BTCTheme = {
    KEY: KEY, MODES: MODES, mode: s === 'light' || s === 'dark' ? s : 'system', unknown: unknown,
    // Returns true when the choice was saved; false when storage is blocked (it still applies to this visit).
    set: function (m) {
      if (MODES.indexOf(m) < 0) throw new Error('Unknown theme: ' + m);
      var saved = true;
      try { if (m === 'system') window.localStorage.removeItem(KEY); else window.localStorage.setItem(KEY, m); } catch (e) { saved = false; }
      T.mode = m; if (saved) T.unknown = null;
      apply(m);
      window.dispatchEvent(new CustomEvent('btc-theme', { detail: m }));
      return saved;
    }
  };
  apply(T.mode);
  if (mq) {
    var onSys = function () { apply(T.mode); if (T.mode === 'system') window.dispatchEvent(new CustomEvent('btc-theme', { detail: 'system' })); };
    if (mq.addEventListener) mq.addEventListener('change', onSys); else if (mq.addListener) mq.addListener(onSys);
  }
})();
