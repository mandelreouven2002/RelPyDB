"""
Build configuration for RelPyDB.

Project metadata is in pyproject.toml. This file builds the two REQUIRED native
extensions — RelPyDB runs on C and does not ship a pure-Python fallback:

  * relpy._relpy_core   — the C engine for the query WHERE path
  * relpy._relpyengine  — the C columnar store + query engine (CoreDB)

If a C compiler or the Python development headers are unavailable, the build
fails (by design). A C toolchain is a hard requirement.
"""

from setuptools import setup, Extension

extensions = [
    Extension(
        "relpy._relpy_core",
        sources=["relpy/_core_src/_relpy_core.c"],
        extra_compile_args=["-O3"],
    ),
    Extension(
        "relpy._relpyengine",
        sources=["relpy/_core_src/_relpyengine.c"],
        extra_compile_args=["-O3", "-std=c11"],
    ),
]

setup(ext_modules=extensions)
