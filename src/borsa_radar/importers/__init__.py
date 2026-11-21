"""Trade history importers: Revolut statements and a broker-neutral CSV (see generic.py for the format)."""

from __future__ import annotations


def import_text(text: str, sub, *, dry_run: bool = False, fmt: str = "auto") -> dict:
    """fmt: "revolut", "csv" or "auto" (the broker-neutral format when its header matches, Revolut otherwise)."""
    from . import generic, revolut
    if fmt == "auto":
        fmt = "csv" if generic.looks_like(text) else "revolut"
    mod = generic if fmt == "csv" else revolut
    return mod.import_text(text, sub, dry_run=dry_run) | {"format": fmt}
