.. _dashboard:

***************
Dashboard Pages
***************

Main Dashboard
==============

The YSE-PZ home page is the dashboard, which sorts transients
into categories based on their "status".  Status labels are customizable
through the `<http://127.0.0.1:8000/admin>`_ pages but the dashboard is hard-coded to show
transients with specific statuses New, Watch, Interesting, FollowupRequested, Following,
FollowupFinished, and NeedsTemplate.  Each category has a link to the transient "summary"
page for each object type and the transient names themselves link to the "detail" page
for a given SN (see :ref:`detail`).  Tables are sortable.

.. image:: _static/yse_pz_maindashboard.png

Personal Dashboard
==================

The `<http://127.0.0.1:8000/personaldashboard>`_ page is in the same format as the main dashboard but
it is populated by queries selected by a given user.  The goal is to allow each user to
flag transients that meet their particular science interests.  See :ref:`queries` documentation
for more information about building queries and adding them to your dashboard.

.. image:: _static/yse_pz_personaldashboard.png

Each saved query appears once per user: adding a query that is already on your
dashboard re-uses the existing entry, and the trash button removes every copy
of that query from your dashboard.  Databases populated before this rule was
enforced can hold duplicate rows; an operator can clean them up with::

  docker exec ysepz_web_container python3 manage.py dedupe_dashboard_queries --dry-run
  docker exec ysepz_web_container python3 manage.py dedupe_dashboard_queries
  docker exec ysepz_web_container python3 manage.py dedupe_dashboard_queries --user <username>

The command keeps the oldest copy of each (user, query) pair and deletes the
rest; ``--dry-run`` only reports what would be removed.
