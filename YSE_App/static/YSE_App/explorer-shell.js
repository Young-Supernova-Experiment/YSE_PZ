/**
 * SQL Explorer inside the YSE-PZ shell (#450).
 *
 * Loaded by templates/explorer/base.html after django-sql-explorer 2.3's
 * explorer.js and before each page's own script block. Explorer's templates
 * use Bootstrap 3 markup; the shell runs Bootstrap 4.6. This file:
 *  1. adds the Bootstrap 4 classes the dropdown, tab and collapse plugins need;
 *  2. scopes ExplorerEditor to the editor form. Explorer selects every <form>
 *     on the page ($("form")), which in the shell would include the header
 *     search and logout forms.
 */
(function ($) {
  "use strict";
  if (!$) {
    return;
  }
  var root = document.getElementById("yse-explorer");
  if (!root) {
    return;
  }
  var $root = $(root);

  // Dropdown menus: <ul class="dropdown-menu"><li><a|input.btn-link> -> .dropdown-item.
  $root.find(".dropdown-menu > li > a, .dropdown-menu > li > input").addClass("dropdown-item");
  $root.find(".dropdown-menu > li.divider").addClass("dropdown-divider");

  // Tabs: Bootstrap 4 keeps .active on the link, not the <li>.
  $root.find(".nav-tabs > li").each(function () {
    var $li = $(this);
    $li.addClass("nav-item");
    var $a = $li.children("a").addClass("nav-link");
    if ($li.hasClass("active")) {
      $li.removeClass("active");
      $a.addClass("active");
    }
  });

  // Collapsible query categories: Bootstrap 3 ".in" is Bootstrap 4 ".show".
  $root.find(".collapse.in").addClass("show");

  if (typeof window.ExplorerEditor === "function") {
    var Original = window.ExplorerEditor;
    var Scoped = function (queryId) {
      Original.call(this, queryId);
      var $editor = $("#editor");
      if ($editor.length) {
        // Read lazily by every click handler bound in Original's bind().
        this.$form = $editor;
      }
    };
    Scoped.prototype = Original.prototype;
    window.ExplorerEditor = Scoped;
  }
})(window.jQuery);
