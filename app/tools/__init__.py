"""One-shot operator tools. Not part of the running processes.

Invoked directly from the CLI, e.g. ``python -m app.tools.backfill_today``.
Keep each tool self-contained: acquire its own DB connection / Dhan client,
do the work, and exit. Tools MUST respect the single-writer invariant
(FRD B.2): when the worker is running, a tool that writes to the DB can
only be run after the worker is stopped.
"""
