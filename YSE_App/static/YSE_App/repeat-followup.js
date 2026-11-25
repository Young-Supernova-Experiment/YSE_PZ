/* "Repeat follow-up…" (#853, UI preview only).
 *
 * Bulk control: on submit, the ticked rows (data-id-selector, inside data-scope) become
 * transient_id fields of the form, which posts to the review page. Nothing is created
 * until the review page is confirmed.
 *
 * Review page: the collaboration groups follow the chosen resource, like the single
 * request form (#794): groups the resource does not allow are disabled, unticked and hidden.
 */
(function () {
  'use strict';
  var each = function (list, fn) { Array.prototype.forEach.call(list, fn); };

  function initControl(form) {
    if (form.getAttribute('data-ready')) { return; }
    form.setAttribute('data-ready', '1');
    var msg = form.querySelector('.yse-repeat-fu-msg');
    form.addEventListener('submit', function (ev) {
      var selector = form.getAttribute('data-id-selector');
      var scopeSel = form.getAttribute('data-scope');
      var scopes = scopeSel ? document.querySelectorAll(scopeSel) : [document];
      var ids = [];
      each(scopes, function (scope) {
        each(scope.querySelectorAll(selector), function (box) {
          var id = box.value || box.getAttribute('data-transient-id');
          if (box.checked && id && ids.indexOf(id) < 0) { ids.push(id); }
        });
      });
      each(form.querySelectorAll('input[name="transient_id"]'), function (old) { old.parentNode.removeChild(old); });
      if (!ids.length) {
        ev.preventDefault();
        if (msg) { msg.textContent = 'Tick at least one row first.'; }
        return;
      }
      ids.forEach(function (id) {
        var input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'transient_id';
        input.value = id;
        form.appendChild(input);
      });
    });
  }

  function initReview(box) {
    var select = box.querySelector('select[name="resource"]');
    var mapNode = document.getElementById('yse-repeat-fu-resource-map');
    if (!select || !mapNode) { return; }
    var map = {};
    try { map = JSON.parse(mapNode.textContent) || {}; } catch (e) { map = {}; }
    function sync() {
      var info = map[select.value];
      each(box.querySelectorAll('.yse-repeat-fu-group'), function (row) {
        var input = row.querySelector('input');
        var gid = parseInt(input.value, 10);
        var enabled = true;
        if (info) {
          if (row.getAttribute('data-public') === '1') {
            enabled = !!info.is_public;
          } else {
            enabled = info.is_public || info.group_ids.indexOf(gid) >= 0;
          }
        }
        input.disabled = !enabled;
        if (!enabled) { input.checked = false; }
        row.hidden = !enabled;
      });
      var windowBox = box.querySelector('.yse-repeat-fu-window');
      if (windowBox) { windowBox.hidden = select.value.indexOf('classical_resource:') === 0; }
    }
    select.addEventListener('change', sync);
    sync();
  }

  function init() {
    each(document.querySelectorAll('form.yse-repeat-fu'), initControl);
    each(document.querySelectorAll('.yse-repeat-fu-review'), initReview);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
