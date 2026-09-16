"""
Native C engine loader.

RelPyDB's query engine runs in C. This module loads the compiled ``_relpy_core``
extension and exposes it as ``core``. The C engine is REQUIRED — there is no
pure-Python fallback and no way to turn it off. If the extension has not been
built, importing RelPyDB fails with a clear instruction to build it.
"""

from __future__ import annotations

try:
    from . import _relpy_core as core  # the compiled C engine (required)
except Exception as exc:  # pragma: no cover
    raise ImportError(
        "RelPyDB's C engine (relpy._relpy_core) is not built. RelPyDB requires "
        "its native engine — build it with `pip install .` (or, from a source "
        "checkout, `python setup.py build_ext --inplace`). A C compiler and the "
        "Python development headers are required."
    ) from exc
