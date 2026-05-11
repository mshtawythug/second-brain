"""Quartz overlay sources — TypeScript/SCSS files that customise Quartz rendering.

This package exists solely so that ``importlib.resources.files("brain.quartz_overrides")``
resolves to this directory regardless of whether the brain package is installed in
editable mode (``pip install -e``) or as a regular wheel.  The Python files inside are
not imported directly; the overlay step in ``brain.vault.quartz_overlay`` walks the
directory tree and copies everything here into a live Quartz workspace.
"""
