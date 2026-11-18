"""Tests package for env-brief (cross-platform: Windows / macOS / Linux).

Auto-import bootstrap: the plugin dir is named ``env-brief`` (a hyphen), so it
is NOT importable by name. Every test file loads it via importlib from this
package's parent (the dir that physically contains ``__init__.py``) — the
hyphen-agnostic analogue of the code_map tests' inline sys.path bootstrap.
"""
