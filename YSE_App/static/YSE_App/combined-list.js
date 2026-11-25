/* Combined candidate list (#849), UI preview only.
 *
 * The chosen sources live in the URL. This browser remembers the last explicit choice in
 * localStorage (key per user, from data-yse-storage-key) and reopens it when the page is
 * opened without one. "Use the defaults" (?src=default) forgets it. Nothing is sent to the
 * server and no per-object state is kept.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.yseCombinedList = api;
    api.init(root.document);
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  function storage(win) {
    try { return win.localStorage; } catch (e) { return null; }
  }

  /* What to do on load: {action: 'restore', query} | {action: 'save', query} | {action: 'forget'} | null */
  function plan(search, explicit, choice, stored) {
    var params = (search || '').replace(/^\?/, '');
    if (/(^|&)src=default(&|$)/.test(params)) { return {action: 'forget'}; }
    if (!params && stored) { return {action: 'restore', query: stored}; }
    if (explicit && choice) { return {action: 'save', query: choice}; }
    return null;
  }

  function init(doc) {
    if (!doc) { return; }
    var run = function () {
      var box = doc.getElementById('combined-sources');
      if (!box) { return; }
      var win = doc.defaultView;
      var key = box.getAttribute('data-yse-storage-key');
      var store = storage(win);
      var stored = null;
      try { stored = store ? store.getItem(key) : null; } catch (e) { stored = null; }
      var next = plan(win.location.search, box.getAttribute('data-yse-explicit') === '1',
                      box.getAttribute('data-yse-choice'), stored);
      try {
        if (next && next.action === 'restore') {
          win.location.replace(win.location.pathname + '?' + next.query);
          return;
        }
        if (next && next.action === 'save' && store) { store.setItem(key, next.query); }
        if (next && next.action === 'forget' && store) { store.removeItem(key); }
      } catch (e) { /* no storage: the URL still carries the choice */ }

      var toggle = doc.getElementById('combined-sources-toggle');
      var form = doc.getElementById('combined-sources-form');
      if (toggle && form) {
        toggle.addEventListener('click', function () {
          var open = form.hidden;
          form.hidden = !open;
          toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        });
      }
    };
    if (doc.readyState === 'loading') {
      doc.addEventListener('DOMContentLoaded', run);
    } else {
      run();
    }
  }

  return {plan: plan, init: init};
}));
