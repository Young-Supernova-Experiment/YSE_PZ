/* Step through a list from the transient page (#850), UI preview only.
 *
 * On a list page, a container with data-yse-list-source="dash|search|tag" gets its
 * transient links extended with the list context:
 *   ?ls=<source>&lq=<the list page's query string>[&lt=<dashboard tab>]
 * The query string comes from data-yse-list-query when set (the tag page builds its
 * list without changing the URL), else from the page's own location.search. The tab
 * comes from the closest data-yse-list-tab. Rows that load later are handled too.
 * Before a link is followed, the list page's scroll position is kept in
 * sessionStorage for "Back to list", and restored once when the list page loads.
 *
 * On the transient page, J opens the next object and K the previous one, unless the
 * key is typed into a field, a modifier is held, or a dialog is open.
 *
 * Nothing about the path through a list is saved anywhere.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.yseListContext = api;
    api.init(root.document);
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var DETAIL_RE = /\/transient_detail\/[^\/?#]+\/?$/;
  var SCROLL_PREFIX = 'yse-list-scroll:';

  /* 'next', 'prev' or null for a keydown event. */
  function stepKey(event) {
    if (!event || event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey) {
      return null;
    }
    var key = event.key;
    if (key !== 'j' && key !== 'J' && key !== 'k' && key !== 'K') { return null; }
    if (isEditable(event.target)) { return null; }
    return (key === 'j' || key === 'J') ? 'next' : 'prev';
  }

  function isEditable(el) {
    for (var node = el; node && node.nodeType === 1; node = node.parentNode) {
      var tag = (node.tagName || '').toUpperCase();
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') { return true; }
      if (node.isContentEditable) { return true; }
      var ce = node.getAttribute && node.getAttribute('contenteditable');
      if (ce !== null && ce !== undefined && ce !== 'false') { return true; }
      var role = node.getAttribute && node.getAttribute('role');
      if (role === 'textbox' || role === 'combobox' || role === 'searchbox') { return true; }
    }
    return false;
  }

  /* The detail-page link with the list context added, or null if href is not one. */
  function contextHref(href, source, query, tab) {
    if (!href) { return null; }
    var hashAt = href.indexOf('#');
    var hash = hashAt >= 0 ? href.substring(hashAt) : '';
    var base = hashAt >= 0 ? href.substring(0, hashAt) : href;
    if (base.indexOf('?') >= 0 || !DETAIL_RE.test(base)) { return null; }
    var params = 'ls=' + encodeURIComponent(source) + '&lq=' + encodeURIComponent((query || '').replace(/^\?/, ''));
    if (tab) { params += '&lt=' + encodeURIComponent(tab); }
    return base + '?' + params + hash;
  }

  function storage() {
    try { return window.sessionStorage; } catch (e) { return null; }
  }

  function scrollKey(loc) {
    return SCROLL_PREFIX + loc.pathname + loc.search;
  }

  function decorate(container, doc) {
    var source = container.getAttribute('data-yse-list-source');
    var links = container.querySelectorAll('a[href]');
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      var query = container.getAttribute('data-yse-list-query');
      if (query === null) { query = doc.defaultView.location.search; }
      var tabEl = a.closest ? a.closest('[data-yse-list-tab]') : null;
      var tab = tabEl ? tabEl.getAttribute('data-yse-list-tab') : '';
      var original = a.getAttribute('data-yse-plain-href') || a.getAttribute('href');
      var next = contextHref(original, source, query, tab);
      if (next) {
        a.setAttribute('data-yse-plain-href', original);
        if (a.getAttribute('href') !== next) { a.setAttribute('href', next); }
      }
    }
  }

  function initListPage(doc) {
    var containers = doc.querySelectorAll('[data-yse-list-source]');
    if (!containers.length) { return; }
    var win = doc.defaultView;
    var store = storage();
    var pending = null;
    function decorateAll() {
      pending = null;
      for (var i = 0; i < containers.length; i++) { decorate(containers[i], doc); }
    }
    decorateAll();
    if (win.MutationObserver) {
      var observer = new win.MutationObserver(function () {
        if (!pending) { pending = win.setTimeout(decorateAll, 0); }
      });
      for (var i = 0; i < containers.length; i++) {
        observer.observe(containers[i], {childList: true, subtree: true, attributes: true,
                                         attributeFilter: ['data-yse-list-query']});
      }
    }
    doc.addEventListener('click', function (ev) {
      var a = ev.target && ev.target.closest ? ev.target.closest('[data-yse-list-source] a[data-yse-plain-href]') : null;
      if (a && store) {
        try { store.setItem(scrollKey(win.location), String(win.pageYOffset || 0)); } catch (e) { /* no storage */ }
      }
    }, true);
    if (store) {
      var saved = null;
      try { saved = store.getItem(scrollKey(win.location)); store.removeItem(scrollKey(win.location)); } catch (e) { saved = null; }
      var y = parseInt(saved, 10);
      if (y > 0) {
        var restore = function () { win.scrollTo(0, y); };
        restore();
        win.addEventListener('load', restore);
        win.setTimeout(restore, 800);  // rows that arrive by AJAX make the page tall enough later
      }
    }
  }

  function initDetailPage(doc) {
    var stepper = doc.getElementById('yse-v2-list-stepper');
    if (!stepper) { return; }
    var urls = {next: stepper.getAttribute('data-next-url'), prev: stepper.getAttribute('data-prev-url')};
    if (!urls.next && !urls.prev) { return; }
    doc.addEventListener('keydown', function (ev) {
      var which = stepKey(ev);
      if (!which || !urls[which]) { return; }
      if (doc.querySelector('.modal.show, .modal.in')) { return; }
      ev.preventDefault();
      doc.defaultView.location.href = urls[which];
    });
  }

  function init(doc) {
    if (!doc) { return; }
    var run = function () { initListPage(doc); initDetailPage(doc); };
    if (doc.readyState === 'loading') {
      doc.addEventListener('DOMContentLoaded', run);
    } else {
      run();
    }
  }

  return {stepKey: stepKey, isEditable: isEditable, contextHref: contextHref, init: init};
}));
