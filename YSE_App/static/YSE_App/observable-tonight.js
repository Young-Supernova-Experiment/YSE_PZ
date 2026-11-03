/* "Observable tonight from <site>" for a shortlist (#854, UI preview only).
 *
 * Reads the transient ids of the rows on the page, asks
 * observable-tonight/?site=&ids=&airmass= once for all of them, and adds one
 * line under each row's name: the UTC window above the airmass limit during
 * astronomical night, the minimum airmass and the moon distance. Rows are
 * never hidden or reordered. The site menu starts from the user's default site
 * (saved on the server with "Make this my default", #959); without one, from the
 * last choice in this browser (localStorage); otherwise the first telescope.
 */
(function () {
  'use strict';

  var SITE_KEY = 'yse-obs-tonight-site';
  var each = function (list, fn) { Array.prototype.forEach.call(list, fn); };

  function loadSite() {
    try { return window.localStorage.getItem(SITE_KEY); } catch (e) { return null; }
  }
  function saveSite(value) {
    try { window.localStorage.setItem(SITE_KEY, value); } catch (e) { /* private mode */ }
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function utcSpan(when) {
    var span = el('span', 'yse-utcdate', when.utc);
    span.title = 'MJD ' + when.mjd + ' (' + when.iso.replace('T', ' ') + ' UTC)';
    return span;
  }

  function nameCell(row) {
    var cell = row.querySelector('[data-col="name_string"]');
    if (cell) { return cell; }
    var link = row.querySelector('a[href*="transient_detail"]');
    if (link && link.closest('td')) { return link.closest('td'); }
    return row.cells && row.cells.length ? row.cells[row.cells.length > 1 ? 1 : 0] : row;
  }

  function init(box) {
    var url = box.getAttribute('data-url');
    var idSelector = box.getAttribute('data-id-selector');
    var scopeSelector = box.getAttribute('data-scope');
    var watchSelector = box.getAttribute('data-watch');
    var max = parseInt(box.getAttribute('data-max'), 10) || 500;
    var site = box.querySelector('.yse-obs-site');
    var airmass = box.querySelector('.yse-obs-airmass');
    var show = box.querySelector('.yse-obs-show');
    var clear = box.querySelector('.yse-obs-clear');
    var status = box.querySelector('.yse-obs-status');
    var active = false;
    var lastKey = '';
    var pending = 0;

    var makeDefault = box.querySelector('.yse-obs-make-default');
    var defaultNote = box.querySelector('.yse-obs-default-note');
    var defaultSite = box.getAttribute('data-default-site') || '';

    var stored = loadSite();
    if (defaultSite && site.querySelector('option[value="' + defaultSite + '"]')) {
      site.value = defaultSite;
    } else if (stored && /^\d+$/.test(stored) && site.querySelector('option[value="' + stored + '"]')) {
      site.value = stored;
    }

    function showDefaultState() {
      var isDefault = !!defaultSite && site.value === defaultSite;
      if (makeDefault) { makeDefault.hidden = isDefault; }
      if (defaultNote) { defaultNote.hidden = !isDefault; }
    }
    showDefaultState();

    function saveDefault() {
      var chosen = site.value;
      makeDefault.disabled = true;
      fetch(box.getAttribute('data-default-url'), {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'X-CSRFToken': box.getAttribute('data-csrf') || '',
          Accept: 'application/json'
        },
        body: 'site=' + encodeURIComponent(chosen)
      })
        .then(function (r) {
          return r.json().then(function (body) { return { ok: r.ok, body: body }; });
        })
        .then(function (res) {
          makeDefault.disabled = false;
          if (!res.ok) {
            status.textContent = res.body.error || 'Could not save your default site.';
            status.classList.add('text-danger');
            return;
          }
          defaultSite = String(res.body.default_site || '');
          box.setAttribute('data-default-site', defaultSite);
          showDefaultState();
        })
        .catch(function () {
          makeDefault.disabled = false;
          status.textContent = 'Could not save your default site. Try again.';
          status.classList.add('text-danger');
        });
    }

    function rows() {
      var scopes = scopeSelector ? document.querySelectorAll(scopeSelector) : [document];
      var out = [];
      each(scopes, function (scope) {
        each(scope.querySelectorAll(idSelector), function (node) {
          var id = node.getAttribute('data-transient-id') || node.value;
          if (id && /^\d+$/.test(id)) {
            out.push({ id: id, row: node.closest('tr') || node });
          }
        });
      });
      return out;
    }

    function removeLines() {
      each(document.querySelectorAll('.yse-obs-line'), function (line) { line.parentNode.removeChild(line); });
    }

    function lineFor(entry, body) {
      var line = el('div', 'yse-obs-line small');
      var limit = body.airmass_limit;
      if (!entry) {
        line.classList.add('text-muted');
        line.textContent = 'Observable tonight: not available for this object.';
        return line;
      }
      if (entry.up) {
        line.classList.add('yse-obs-up');
        line.appendChild(el('strong', '', 'Up '));
        line.appendChild(utcSpan(entry.start));
        line.appendChild(document.createTextNode(' – '));
        line.appendChild(utcSpan(entry.end));
        line.appendChild(document.createTextNode(' UTC (' + entry.hours + ' h)'));
      } else {
        line.classList.add('yse-obs-down');
        line.appendChild(el('strong', '', entry.min_airmass === null
          ? 'Below the horizon all night'
          : 'Not at airmass \u2264 ' + limit + ' during astronomical night'));
      }
      var parts = [];
      if (entry.min_airmass !== null) { parts.push('min airmass ' + entry.min_airmass.toFixed(2)); }
      parts.push('moon ' + entry.moon_sep + '°');
      line.appendChild(document.createTextNode(' · ' + parts.join(' · ')));
      return line;
    }

    function render(items, body) {
      removeLines();
      var up = 0;
      items.forEach(function (item) {
        var entry = body.results[item.id];
        if (entry && entry.up) { up += 1; }
        nameCell(item.row).appendChild(lineFor(entry, body));
      });
      var ids = {};
      items.forEach(function (item) { ids[item.id] = true; });
      var text = up + ' of ' + Object.keys(ids).length + ' up from ' + body.telescope.name +
        ', night ' + body.night + ' (astronomical night ' + body.dark_start.utc + ' – ' +
        body.dark_end.utc + ' UTC).';
      if (body.note) { text += ' Note: ' + body.note + '.'; }
      status.textContent = text;
      status.classList.remove('text-danger');
    }

    function fail(message) {
      removeLines();
      status.textContent = message;
      status.classList.add('text-danger');
    }

    function run(force) {
      var items = rows();
      var unique = [];
      var seen = {};
      items.forEach(function (item) {
        if (!seen[item.id]) { seen[item.id] = true; unique.push(item.id); }
      });
      var key = site.value + '|' + airmass.value + '|' + unique.join(',');
      if (!force && key === lastKey) { return; }
      lastKey = key;
      if (!unique.length) {
        removeLines();
        status.textContent = 'No objects on this page.';
        return;
      }
      if (unique.length > max) {
        fail('Observable tonight works on at most ' + max + ' objects; this page has ' + unique.length +
             '. Narrow the list first.');
        return;
      }
      var ticket = ++pending;
      status.classList.remove('text-danger');
      status.textContent = 'Computing…';
      var params = 'site=' + encodeURIComponent(site.value) + '&airmass=' + encodeURIComponent(airmass.value) +
        '&ids=' + unique.join(',');
      fetch(url + '?' + params, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
        .then(function (r) {
          return r.json().then(function (body) { return { ok: r.ok, body: body }; });
        })
        .then(function (res) {
          if (ticket !== pending || !active) { return; }
          if (!res.ok) { fail(res.body.error || 'Could not compute visibility.'); return; }
          render(rows(), res.body);
        })
        .catch(function () {
          if (ticket === pending) { fail('Could not compute visibility. Try again.'); lastKey = ''; }
        });
    }

    show.addEventListener('click', function () {
      active = true;
      clear.hidden = false;
      run(true);
    });
    clear.addEventListener('click', function () {
      active = false;
      lastKey = '';
      pending += 1;
      removeLines();
      status.textContent = '';
      clear.hidden = true;
    });
    if (makeDefault) { makeDefault.addEventListener('click', saveDefault); }
    site.addEventListener('change', function () {
      saveSite(site.value);
      showDefaultState();
      if (active) { run(true); }
    });
    airmass.addEventListener('change', function () { if (active) { run(true); } });

    // Rows loaded later (dashboard tabs) or a tab switch: annotate what is shown now.
    var watched = watchSelector ? document.querySelector(watchSelector) : null;
    if (watched && window.MutationObserver) {
      var timer = null;
      new MutationObserver(function () {
        if (!active) { return; }
        window.clearTimeout(timer);
        timer = window.setTimeout(function () { run(false); }, 150);
      }).observe(watched, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden'] });
    }
  }

  function start() { each(document.querySelectorAll('div.yse-obs-tonight[data-url]'), init); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
}());
