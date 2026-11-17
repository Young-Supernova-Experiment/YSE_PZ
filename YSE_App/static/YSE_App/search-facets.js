/* Facet sidebar on Transient Search (#842), UI preview only.
 *
 * A facet value or an active-filter chip is a plain link to the same search with one
 * parameter added or removed, so it works without this script. With it, the click
 * fetches the same URL with the header "X-YSE-Partial: search" and swaps in the parts
 * that change (facets and counts, filter form, chips, results table, the hidden
 * "params" of the bulk, Save-as-SQL and Save-as-Selection forms), then updates the
 * address bar. Anything the swap cannot update (form errors, the Save-as-SQL or the
 * "Observable tonight" blocked state changing) falls back to loading the page.
 */
(function ($) {
  'use strict';
  if (!$ || !document.getElementById('search-preview-layout')) { return; }

  var pending = null;
  var shown = window.location.pathname + window.location.search;

  function navigate(url) { window.location.href = url; }

  function apply(d, url) {
    var obsNow = $('#search-results').attr('data-obs-blocked') || '';
    var saveBlockedNow = $('#search-save-blocked').length > 0;
    if (d.has_errors || (d.obs_blocked || '') !== obsNow || !!d.save_blocked !== saveBlockedNow) {
      navigate(url);
      return false;
    }
    $('#search-facets-body').html(d.facets_html);
    $('#search-filter-groups').html(d.groups_html);
    $('#search-chips-slot').html(d.chips_html);
    $('#search-results-table').html(d.table_html);
    $('#search-result-count').text(d.count_text);
    $('label[for="search-bulk-select-all"]').text(d.select_all_label);
    $('#search-bulk-select-all-page').prop('checked', false);
    $('#search-bulk-form input[name="params"]').val(d.bulk_params);
    $('#selection-save-form input[name="params"]').val(d.bulk_params);
    $('#selection-save-name').val(d.selection_name);
    $('#search-save-form input[name="params"]').val(d.save_params);
    $('#search-save-title').val(d.save_title);
    $('#search-save-sql').text(d.save_sql || '(not available)');
    // Download -> Spectra / Photometry (#848): the forms carry the search as "params"
    $('#download-spectra-form, #download-photometry-form').each(function () {
      var $input = $(this).find('input[name="params"]');
      if (!d.download_params) { $input.remove(); return; }
      if (!$input.length) { $input = $('<input type="hidden" name="params">').prependTo(this); }
      $input.val(d.download_params);
    });
    $('#search-api-link').attr('href', d.api_url);
    $('#search-export-csv').attr('href', d.export_url);
    $('#search-bulk-result, #search-saved, #search-save-error, #selection-save-error, #search-errors').remove();
    return true;
  }

  function load(url, push) {
    if (pending) { pending.abort(); }
    var $layout = $('#search-preview-layout').attr('aria-busy', 'true');
    pending = $.ajax({ url: url, dataType: 'json', headers: { 'X-YSE-Partial': 'search' } })
      .done(function (d) {
        if (!apply(d, url)) { return; }
        if (push && window.history && window.history.pushState) {
          window.history.pushState({ yseSearch: true }, '', url);
        }
        shown = window.location.pathname + window.location.search;
      })
      .fail(function (xhr, status) { if (status !== 'abort') { navigate(url); } })
      .always(function () { pending = null; $layout.removeAttr('aria-busy'); });
  }

  $(document).on('click', '#search-facets a[data-yse-facet], #search-chips-slot a', function (event) {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button > 0) { return; }
    event.preventDefault();
    load(this.href, true);
  });

  window.addEventListener('popstate', function () {
    var now = window.location.pathname + window.location.search;
    if (now !== shown) { load(window.location.href, false); }
  });
}(window.jQuery));
