/* Tabbed main dashboard for UI preview users (#859).
 *
 * Tabs: the open tab is the URL hash (sort and page links end in #<tab>), else the
 * tab remembered in localStorage, else the server's default. A tab's table is
 * fetched the first time it opens. Columns: optional columns are rendered and
 * hidden here; the choice is kept in localStorage. Bulk status: the ticked rows
 * of the open tab get one PATCH each to the same API endpoint the per-row status
 * menu uses, after a confirmation.
 */
(function () {
  'use strict';
  var root = document.getElementById('yse-dash2');
  if (!root || !window.fetch) { return; }

  var TAB_KEY = 'yse-dash-tab';
  var COL_KEY = 'yse-dash-cols';
  var FLASH_KEY = 'yse-dash-flash';
  var each = function (list, fn) { Array.prototype.forEach.call(list, fn); };

  function load(store, key) {
    try { return window[store].getItem(key); } catch (e) { return null; }
  }
  function save(store, key, value) {
    try {
      if (value === null) { window[store].removeItem(key); } else { window[store].setItem(key, value); }
    } catch (e) { /* storage unavailable: nothing is remembered */ }
  }

  var tabs = root.querySelectorAll('[data-dash-tab]');
  var panels = root.querySelectorAll('[data-dash-panel]');
  var valid = {};
  each(tabs, function (t) { valid[t.getAttribute('data-dash-tab')] = true; });
  var active = null;

  /* ---------- columns ---------- */
  var toggles = root.querySelectorAll('.yse-dash-col-toggle');
  var optional = {};
  each(toggles, function (box) { optional[box.value] = true; });
  var defaults = (root.getAttribute('data-default-columns') || '').split(/\s+/).filter(Boolean);

  function visibleColumns() {
    var stored = load('localStorage', COL_KEY);
    if (stored) {
      try {
        var list = JSON.parse(stored);
        if (Array.isArray(list)) { return list; }
      } catch (e) { /* fall through to the defaults */ }
    }
    return defaults;
  }

  function applyColumns() {
    var shown = {};
    visibleColumns().forEach(function (name) { shown[name] = true; });
    each(toggles, function (box) { box.checked = !!shown[box.value]; });
    each(root.querySelectorAll('[data-col]'), function (cell) {
      var name = cell.getAttribute('data-col');
      if (optional[name]) { cell.hidden = !shown[name]; }
    });
  }

  each(toggles, function (box) {
    box.addEventListener('change', function () {
      var list = [];
      each(toggles, function (b) { if (b.checked) { list.push(b.value); } });
      save('localStorage', COL_KEY, JSON.stringify(list));
      applyColumns();
    });
  });
  var reset = root.querySelector('.yse-dash-col-reset');
  if (reset) {
    reset.addEventListener('click', function () {
      save('localStorage', COL_KEY, null);
      applyColumns();
    });
  }

  /* ---------- selection and bulk status ---------- */
  var bulkCount = root.querySelector('.yse-dash-bulk-count');
  var bulkStatus = root.querySelector('#yse-dash-bulk-status');
  var bulkApply = root.querySelector('.yse-dash-bulk-apply');
  var bulkMsg = root.querySelector('.yse-dash-bulk-msg');

  function activePanel() {
    return root.querySelector('[data-dash-panel="' + active + '"]');
  }

  function selectedBoxes() {
    var panel = activePanel();
    return panel ? panel.querySelectorAll('.yse-dash-select:checked') : [];
  }

  function updateSelection() {
    var n = selectedBoxes().length;
    bulkCount.textContent = n + ' selected';
    bulkApply.disabled = n === 0;
  }

  root.addEventListener('change', function (ev) {
    var el = ev.target;
    if (el.classList.contains('yse-dash-select-all')) {
      var table = el.closest('table');
      each(table.querySelectorAll('.yse-dash-select'), function (box) { box.checked = el.checked; });
      updateSelection();
    } else if (el.classList.contains('yse-dash-select')) {
      updateSelection();
    }
  });

  function patchStatus(id, statusUrl) {
    var url = root.getAttribute('data-api-url').replace(/\/0\/$/, '/' + encodeURIComponent(id) + '/');
    return fetch(url, {
      method: 'PATCH',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-CSRFToken': root.getAttribute('data-csrf'),
        'X-Requested-With': 'XMLHttpRequest'
      },
      body: 'status=' + encodeURIComponent(statusUrl)
    }).then(function (r) { return r.ok; }, function () { return false; });
  }

  bulkApply.addEventListener('click', function () {
    var boxes = selectedBoxes();
    var statusUrl = bulkStatus.value;
    if (!boxes.length) { return; }
    if (!statusUrl) {
      bulkMsg.textContent = 'Choose a status first.';
      bulkStatus.focus();
      return;
    }
    var label = bulkStatus.options[bulkStatus.selectedIndex].text;
    var ids = [];
    each(boxes, function (box) { ids.push(box.value); });
    var noun = ids.length === 1 ? 'transient' : 'transients';
    if (!window.confirm('Set the status of ' + ids.length + ' ' + noun + ' to "' + label + '"?')) { return; }

    bulkApply.disabled = true;
    bulkMsg.textContent = 'Updating…';
    var ok = 0;
    var chain = Promise.resolve();
    ids.forEach(function (id) {
      chain = chain.then(function () {
        return patchStatus(id, statusUrl).then(function (done) { if (done) { ok += 1; } });
      });
    });
    chain.then(function () {
      var msg = 'Status set to ' + label + ' on ' + ok + ' of ' + ids.length + ' ' + noun + '.';
      if (ok < ids.length) { msg += ' ' + (ids.length - ok) + ' could not be changed.'; }
      save('sessionStorage', FLASH_KEY, msg);
      window.location.reload();
    });
  });

  /* ---------- tabs ---------- */
  function sectionUrl(base) {
    var search = window.location.search;
    if (search && search.length > 1) {
      base += (base.indexOf('?') >= 0 ? '&' : '?') + search.substring(1);
    }
    return base;
  }

  function loadPanel(panel) {
    if (panel.getAttribute('data-loaded')) { return; }
    panel.setAttribute('data-loaded', '1');
    var slot = panel.querySelector('.yse-dash-slot');
    fetch(sectionUrl(slot.getAttribute('data-url')), {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    }).then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.text();
    }).then(function (html) {
      slot.innerHTML = html;
      applyColumns();
      updateSelection();
    }).catch(function () {
      panel.removeAttribute('data-loaded');
      slot.innerHTML = '<p class="text-danger">Failed to load this tab. Open it again to retry.</p>';
    });
  }

  function activate(key, remember) {
    if (!valid[key]) { return; }
    active = key;
    each(tabs, function (t) {
      var on = t.getAttribute('data-dash-tab') === key;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    each(panels, function (p) {
      var on = p.getAttribute('data-dash-panel') === key;
      p.hidden = !on;
      if (on) { loadPanel(p); }
    });
    // Ticks on another tab are not applied to this one.
    each(root.querySelectorAll('.yse-dash-select, .yse-dash-select-all'), function (box) { box.checked = false; });
    updateSelection();
    if (remember) { save('localStorage', TAB_KEY, key); }
  }

  each(tabs, function (t) {
    t.addEventListener('click', function (ev) {
      ev.preventDefault();
      var key = t.getAttribute('data-dash-tab');
      activate(key, true);
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, '', '#' + key);
      }
    });
  });

  window.addEventListener('hashchange', function () {
    var key = window.location.hash.replace(/^#/, '').toLowerCase();
    if (valid[key]) { activate(key, true); }
  });

  var flash = load('sessionStorage', FLASH_KEY);
  if (flash) {
    bulkMsg.textContent = flash;
    save('sessionStorage', FLASH_KEY, null);
  }

  var fromHash = window.location.hash.replace(/^#/, '').toLowerCase();
  var stored = load('localStorage', TAB_KEY);
  if (valid[fromHash]) {
    activate(fromHash, true);
  } else if (valid[stored]) {
    activate(stored, false);
  } else {
    activate(root.getAttribute('data-default-tab'), false);
  }
  applyColumns();
}());
