"""
Build the optional native core extension.

From the package root:  python relpy/_core_src/setup.py build_ext --inplace
This compiles relpy/_relpy_core*.so next to the package so `relpy._core`
picks it up. If it is not built, RelPyDB still works (pure-Python fallback).
"""

from setuptools import setup, Extension

ext = Extension(
    "relpy._relpy_core",
    sources=["relpy/_core_src/_relpy_core.c"],
    extra_compile_args=["-O3"],
)

setup(
    name="relpy-native-core",
    version="2.0.0",
    ext_modules=[ext],
)
