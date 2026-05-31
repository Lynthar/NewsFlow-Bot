"""Pluggable non-RSS source fetchers.

Each module here defines a :class:`~newsflow.core.source_fetcher.SourceFetcher`
for one ``source_type`` and registers it on import. They are imported lazily by
``source_fetcher.get_source_fetcher`` the first time a feed of that type is
dispatched, so an uninstalled optional dependency only surfaces as a clear
per-fetch error rather than breaking startup.
"""
