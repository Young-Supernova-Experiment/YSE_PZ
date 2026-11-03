/* Visual rule builder (#843), UI preview only.
 *
 * The rows are server-rendered (selections/rule_builder.html). This script adds and removes
 * rows and groups by cloning the page's <template> prototypes, swaps a row's comparison and
 * value when its field changes, and posts the tree as JSON: to selections/rule-builder/check/
 * for "Count matches" and "View as SQL", and with the form for saving. Every control is a
 * native button, select or input, so the builder works from the keyboard.
 */
(function ($) {
  'use strict';
  if (!$) { return; }

  function builderOf(el) { return $(el).closest('.yse-rule-builder'); }

  function cloneTemplate($builder, selector) {
    var tpl = $builder.find(selector).get(0);
    if (!tpl) { return $(); }
    var html = tpl.content ? $('<div>').append(document.importNode(tpl.content, true)).html() : tpl.innerHTML;
    return $($.parseHTML($.trim(html)));
  }

  function ruleCount($builder) { return $builder.find('.yse-rule-tree .yse-rule-row').length; }

  function setStatus($builder, text, isError) {
    $builder.find('.yse-rule-status').text(text || '').toggleClass('text-danger', !!isError);
  }

  function addRow($group) {
    var $builder = builderOf($group);
    var max = parseInt($builder.data('max-rules'), 10) || 40;
    if (ruleCount($builder) >= max) {
      setStatus($builder, 'Use at most ' + max + ' rules.', true);
      return null;
    }
    var $row = cloneTemplate($builder, 'template.yse-rule-row-tpl');
    $group.children('.yse-rule-children').append($row);
    return $row;
  }

  function addGroup($parent) {
    var $builder = builderOf($parent);
    var depth = (parseInt($parent.attr('data-depth'), 10) || 1) + 1;
    var maxDepth = parseInt($builder.data('max-depth'), 10) || 4;
    if (depth > maxDepth) { return null; }
    var $group = cloneTemplate($builder, 'template.yse-rule-group-tpl');
    $group.attr('data-depth', depth);
    $group.find('.yse-rule-add-group').prop('hidden', depth >= maxDepth);
    $parent.children('.yse-rule-children').append($group);
    addRow($group);
    return $group;
  }

  function serializeGroup($group) {
    var node = { op: $group.children('.yse-rule-group-head').find('.yse-rule-op').val() || 'and', rules: [] };
    $group.children('.yse-rule-children').children().each(function () {
      var $child = $(this);
      if ($child.hasClass('yse-rule-group')) {
        node.rules.push(serializeGroup($child));
      } else if ($child.hasClass('yse-rule-row')) {
        var $value = $child.find('.yse-rule-value');
        node.rules.push({
          field: $child.find('.yse-rule-field').val() || '',
          operator: $child.find('.yse-rule-operator').val() || '',
          value: $value.length ? $value.val() : ''
        });
      }
    });
    return node;
  }

  function treeJson($builder) {
    return JSON.stringify(serializeGroup($builder.find('.yse-rule-tree > .yse-rule-group').first()));
  }

  function check($builder, extra, done) {
    var $form = $builder.find('form.yse-rule-form');
    var data = { rules: treeJson($builder), csrfmiddlewaretoken: $form.find('input[name=csrfmiddlewaretoken]').val() };
    $.extend(data, extra);
    setStatus($builder, 'Checking…', false);
    $.ajax({ url: $builder.data('check-url'), method: 'POST', data: data, dataType: 'json' })
      .done(function (resp) {
        if (!resp.ok) { setStatus($builder, resp.error, true); return; }
        done(resp);
      })
      .fail(function () { setStatus($builder, 'The rules could not be checked. Try again.', true); });
  }

  // --- field change: the comparison and value of that field -------------------------------
  $(document).on('change', '.yse-rule-builder .yse-rule-field', function () {
    var $row = $(this).closest('.yse-rule-row');
    var $controls = $row.find('.yse-rule-controls').empty();
    var key = $(this).val();
    if (key) {
      $controls.append(cloneTemplate(builderOf(this), 'template.yse-rule-controls-tpl[data-yse-rule-field="' + key + '"]'));
    }
  });

  // --- add / remove ------------------------------------------------------------------------
  $(document).on('click', '.yse-rule-builder .yse-rule-add', function () {
    var $row = addRow($(this).closest('.yse-rule-group'));
    if ($row) { $row.find('.yse-rule-field').trigger('focus'); }
  });

  $(document).on('click', '.yse-rule-builder .yse-rule-add-group', function () {
    var $group = addGroup($(this).closest('.yse-rule-group'));
    if ($group) { $group.find('.yse-rule-field').first().trigger('focus'); }
  });

  $(document).on('click', '.yse-rule-builder .yse-rule-remove', function () {
    var $group = $(this).closest('.yse-rule-row').parent().closest('.yse-rule-group');
    $(this).closest('.yse-rule-row').remove();
    $group.children('.yse-rule-group-actions').find('.yse-rule-add').trigger('focus');
  });

  $(document).on('click', '.yse-rule-builder .yse-rule-remove-group', function () {
    var $group = $(this).closest('.yse-rule-group');
    var $parent = $group.parent().closest('.yse-rule-group');
    $group.remove();
    $parent.children('.yse-rule-group-actions').find('.yse-rule-add').trigger('focus');
  });

  // --- count / SQL -------------------------------------------------------------------------
  $(document).on('click', '.yse-rule-builder .yse-rule-count', function () {
    var $builder = builderOf(this);
    check($builder, { count: '1' }, function (resp) {
      setStatus($builder, resp.count + ' match' + (resp.count === 1 ? '' : 'es') + ': ' + resp.summary, false);
    });
  });

  $(document).on('click', '.yse-rule-builder .yse-rule-sql-toggle', function () {
    var $builder = builderOf(this);
    var $button = $(this);
    var $box = $builder.find('.yse-rule-sql-box');
    if (!$box.prop('hidden')) {
      $box.prop('hidden', true);
      $button.attr('aria-expanded', 'false').text('View as SQL');
      return;
    }
    check($builder, { sql: '1' }, function (resp) {
      $builder.find('.yse-rule-sql').text(resp.sql);
      $box.prop('hidden', false);
      $button.attr('aria-expanded', 'true').text('Hide SQL');
      setStatus($builder, resp.summary, false);
    });
  });

  // Enter in a rule's value counts the matches instead of submitting the save form.
  $(document).on('keydown', '.yse-rule-builder .yse-rule-tree input', function (event) {
    if (event.key === 'Enter') {
      event.preventDefault();
      builderOf(this).find('.yse-rule-count').trigger('click');
    }
  });

  // --- save ----------------------------------------------------------------------------------
  $(document).on('submit', '.yse-rule-builder form.yse-rule-form', function () {
    $(this).find('input.yse-rule-json').val(treeJson(builderOf(this)));
  });

  $(document).on('change', '.yse-rule-builder #rule-builder-visibility', function () {
    var team = $(this).val() === 'team';
    $('#rule-builder-team-group').prop('hidden', !team);
    $('#rule-builder-team').prop('disabled', !team);
  });
}(window.jQuery));
