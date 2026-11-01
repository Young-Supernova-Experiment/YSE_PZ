/* Spectrum upload form on the transient page (form_snippets/spectrum_upload_form.html).
 *
 * #900: an empty datetime-local field can still show a date in some browsers, so the line under
 *       it says whether a value is set and how it is read (UT). "Now (UT)" fills in the current
 *       UT time.
 * #901: required fields are checked before the file is sent; the server checks stay as they are.
 *
 * Handlers are delegated from document because parts of the transient page are loaded later.
 */
(function ($) {
    'use strict';

    function pad(n) {
        return (n < 10 ? '0' : '') + n;
    }

    function nowUT() {
        var d = new Date();
        return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) +
            'T' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes());
    }

    function showDate() {
        var $input = $('#upload_spectrum #obs_date');
        var $status = $('#obs_date_status');
        if (!$input.length || !$status.length) {
            return;
        }
        var value = $input.val();
        $status.empty();
        if (value) {
            $status.append($('<span class="text-success"></span>')
                .text('Will be saved as ' + value.replace('T', ' ') + ' UT'));
        } else {
            $status.append($('<span class="text-danger"></span>').text('Not set'));
        }
    }

    function labelFor(field) {
        var $label = field.id ? $('label[for="' + field.id + '"]').first() : $();
        if (!$label.length) {
            return field.name;
        }
        return $.trim($label.clone().children().remove().end().text());
    }

    $(document).on('input change', '#upload_spectrum #obs_date', showDate);

    $(document).on('click', '#upload_spectrum #obs_date_now', function (event) {
        event.preventDefault();
        $('#upload_spectrum #obs_date').val(nowUT());
        showDate();
    });

    $(document).on('submit', '#upload_spectrum', function (event) {
        var missing = [];
        $(this).find('[required]').each(function () {
            if (!$.trim($(this).val() || '')) {
                missing.push(labelFor(this));
            }
        });
        var $check = $('#upload_spectrum_check');
        showDate();
        if (missing.length) {
            event.preventDefault();
            $check.text('Not uploaded yet. Please fill in: ' + missing.join(', ') + '.').show();
            return false;
        }
        $check.hide().text('');
        return true;
    });

    $(showDate);
})(jQuery);
