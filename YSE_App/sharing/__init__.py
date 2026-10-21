"""Sharing services: TNS reporting, submission queue and TNS retrieval (#324).

Modules:

- :mod:`YSE_App.sharing.tns` builds AT (discovery) and classification reports
  in the TNS bulk-report schema, talks to the TNS API, and runs the
  ``sharing.submit`` / ``sharing.poll`` job handlers.
- :mod:`YSE_App.sharing.retrieval` runs ``sharing.tns_retrieval``: it matches
  YSE-PZ transients without a TNS name to TNS objects by cone search.
- :mod:`YSE_App.sharing.autopublish` evaluates ``AutoPublisher`` rules on
  transient saves and in the ``sharing.autopublish_sweep`` job.
- :mod:`YSE_App.sharing.hermes` is the hook for Hermes publishing (#281).

Importing :mod:`YSE_App.sharing.handlers` registers every job handler.
"""
