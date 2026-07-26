"""Orchestration the CLI (or any future caller) drives.

The layer boundary this package defends: **orchestration returns data, callers
render it.** Nothing here prints, reads argv, or exits — so the same call that
backs `coach sync` today can back a scheduler or an API handler later without
being rewritten (§11: clean seams, not premature machinery).
"""
