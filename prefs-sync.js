/* Keeps each signed-in person's saved settings (chart view, indicators, drawings, Screener columns and filters, theme...) on the server,
   so they follow the person to any browser or phone. Loaded first on every page, before the page reads its settings.

   On load: if the server holds this person's settings they replace what this browser has (except the screen layout, which stays per device);
   if it holds none yet, this browser's settings are uploaded as the starting point.
   After that, every change to a "setupdesk.*" setting is sent to the server a moment later. Nothing happens while the login is off. */
(function () {
  'use strict';
  var P = 'setupdesk.', V1 = 'setupdesk.v1', DEVICE = ['ui'];            // parts of the main settings that belong to one screen, not to the person
  var ls; try { ls = window.localStorage; ls.length; } catch (e) { return; }
  var origSet = Storage.prototype.setItem, origRemove = Storage.prototype.removeItem;
  function mine() {
    var o = {};
    for (var i = 0; i < ls.length; i++) { var k = ls.key(i); if (k.indexOf(P) === 0) o[k] = ls.getItem(k); }
    return o;
  }
  function pull() {
    var x = new XMLHttpRequest();
    x.open('GET', '/api/prefs', false);                                   // synchronous on purpose: the page must not read its settings before this finishes
    x.send();
    if (x.status !== 200) return null;
    return JSON.parse(x.responseText);
  }
  var d;
  try { d = pull(); } catch (e) { return; }
  if (!d || !d.sync) return;
  var items = d.items || {}, has = Object.keys(items).length > 0;
  if (has) {
    var local = {}; try { local = JSON.parse(ls.getItem(V1) || '{}') || {}; } catch (e) {}
    mine_keys_remove();
    Object.keys(items).forEach(function (k) {
      var v = items[k];
      if (k === V1) {
        try { var o = JSON.parse(v) || {}; DEVICE.forEach(function (f) { if (local[f] !== undefined) o[f] = local[f]; else delete o[f]; }); v = JSON.stringify(o); } catch (e) {}
      }
      origSet.call(ls, k, v);
    });
  }
  function mine_keys_remove() {
    var del = [];
    for (var i = 0; i < ls.length; i++) { var k = ls.key(i); if (k.indexOf(P) === 0) del.push(k); }
    del.forEach(function (k) { origRemove.call(ls, k); });
  }
  var timer = null;
  function push(unloading) {
    timer = null;
    try {
      fetch('/api/prefs', { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ items: mine() }), keepalive: !!unloading, credentials: 'same-origin' }).catch(function () {});
    } catch (e) {}
  }
  function later() { clearTimeout(timer); timer = setTimeout(push, 1500); }
  Storage.prototype.setItem = function (k, v) { origSet.call(this, k, v); if (this === ls && String(k).indexOf(P) === 0) later(); };
  Storage.prototype.removeItem = function (k) { origRemove.call(this, k); if (this === ls && String(k).indexOf(P) === 0) later(); };
  window.addEventListener('pagehide', function () { if (timer) { clearTimeout(timer); push(true); } });
  if (!has && Object.keys(mine()).length) push();                          // first time on this account: this browser's settings become the account's
})();
