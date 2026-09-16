"""
Dependency bootstrap.

RelPyDB uses a few third-party packages only for optional features (cryptography
for encrypted columns, SQLAlchemy + drivers for SQL backends, pandas/NumPy for
those exports). Rather than failing when one is missing, RelPyDB installs it on
first use, automatically.

`ensure("pandas")` returns the imported module, installing it with pip if it is
not present. Set the environment variable ``RELPY_AUTO_INSTALL=0`` to disable
this and get a normal ImportError instead.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

_AUTO = os.environ.get("RELPY_AUTO_INSTALL", "1").lower() not in ("0", "false", "no")

# import name -> pip install name, when they differ
_PIP_NAME = {
    "cryptography.fernet": "cryptography",
    "cryptography": "cryptography",
    "sqlalchemy": "SQLAlchemy",
    "pandas": "pandas",
    "numpy": "numpy",
    "psycopg": "psycopg[binary]",
    "pymysql": "PyMySQL",
    "oracledb": "oracledb",
    "pyodbc": "pyodbc",
}

_INSTALLED_THIS_RUN: set[str] = set()


def ensure(module_name: str, pip_name: str | None = None):
    """Import ``module_name``, installing it with pip first if necessary."""
    try:
        return importlib.import_module(module_name)
    except ImportError:
        if not _AUTO:
            raise

    pip_name = pip_name or _PIP_NAME.get(module_name, module_name.split(".")[0])
    if pip_name not in _INSTALLED_THIS_RUN:
        sys.stderr.write(
            f"[relpy] '{module_name}' is required for this feature and is not "
            f"installed — installing '{pip_name}'...\n"
        )
        sys.stderr.flush()
        subprocess.run(
            [sys.executable, "-m", "pip", "install", pip_name],
            check=True,
        )
        _INSTALLED_THIS_RUN.add(pip_name)
        importlib.invalidate_caches()

    return importlib.import_module(module_name)
