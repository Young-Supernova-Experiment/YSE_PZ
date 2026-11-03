/* Fit part of the data (#847), UI preview only (loaded by transient_detail_v2.html).
   The Analysis tab fragment carries the form (#fit_subset_form) and the fit history's
   "Repeat this fit" buttons; both POST {service, params} to the tab's run URL, the same
   endpoint (permissions, daily cap, job queue) as a normal run. The light-curve toolbar's
   "Fit part of the data" button opens the Analysis tab at the form. Handlers are
   delegated on document because the fragment loads later. */
(function($) {
    if (!$) { return; }

    function runUrl() { return $('#yse_analysis_tab_body').data('run-url'); }
    /* The form carries the token; a Repeat button is only shown when the form is (a fit service the user may run). */
    function csrf() { return $('#fit_subset_form').attr('data-csrf') || ''; }
    function reload() { if (window.yseLoadAnalysisTab) { window.yseLoadAnalysisTab(true); } }

    function post(service, params, $btn, $err) {
        $btn.prop('disabled', true);
        $err.text('');
        $.ajax({
            url: runUrl(),
            method: 'POST',
            contentType: 'application/json',
            headers: {'X-CSRFToken': csrf()},
            data: JSON.stringify({service: service, params: params})
        }).done(reload).fail(function(xhr) {
            var json = xhr.responseJSON || {};
            var msg = (json.errors && json.errors.selection) || json.error || ('request failed (' + xhr.status + ')');
            $err.text(msg);
            $btn.prop('disabled', false);
        });
    }

    function collectSelection() {
        var selection = {};
        var until = $.trim($('#fit_subset_until').val() || '');
        if (until) { selection.until = until; }
        $('#fit_subset_form .fit-subset-group').each(function() {
            var values = $(this).find('input[type=checkbox]:checked').map(function() { return this.value; }).get();
            if (values.length) { selection[$(this).data('key')] = values; }
        });
        return selection;
    }

    $(document).on('click', '#fit_subset_run_btn', function() {
        var $err = $('#fit_subset_error');
        var selection = collectSelection();
        if ($.isEmptyObject(selection)) {
            $err.text('Choose a date, or at least one band, telescope or quality level.');
            return;
        }
        post($('#fit_subset_service').val(), {selection: selection}, $(this), $err);
    });

    $(document).on('click', '.fit-subset-repeat', function() {
        var $btn = $(this);
        var params;
        try { params = JSON.parse($btn.attr('data-params') || '{}'); } catch (e) { params = null; }
        var $err = $btn.siblings('.fit-subset-repeat-error');
        if (!$err.length) { $err = $('<span class="text-danger small fit-subset-repeat-error"></span>').insertAfter($btn); }
        if (!params || !params.selection) { $err.text('This run has no saved selection.'); return; }
        post($btn.attr('data-service'), params, $btn, $err);
    });

    /* Light-curve toolbar: open the Analysis tab and bring the form into view once it loads. */
    $(document).on('click', '.yse-fit-subset-open', function(event) {
        event.preventDefault();
        $('a[href="#analysis_tab"]').tab('show');
        var tries = 0;
        (function focusForm() {
            var el = document.getElementById('fit_subset_card');
            if (el) {
                if (el.scrollIntoView) { el.scrollIntoView({block: 'start'}); }
                $('#fit_subset_until').trigger('focus');
            } else if (tries++ < 40) {
                setTimeout(focusForm, 250);
            }
        })();
    });
})(window.jQuery);
