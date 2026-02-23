"""
portage-pip-fuse: A FUSE-based filesystem adapter between pip and portage.

This package provides a virtual filesystem that translates between Python's pip
package format and Gentoo's portage ebuild format, enabling seamless integration
between the two package management systems.

Copyright (C) 2026 Dirk Tilger
Licensed under GPL-2.0
"""

__version__ = "0.1.0"
__author__ = "Dirk Tilger"
__email__ = "dirk@systemication.com"

from portage_pip_fuse.filesystem import PortagePipFS
from portage_pip_fuse.name_translator import (
    SimpleNameTranslator,
    CachedNameTranslator,
    pypi_to_gentoo,
    gentoo_to_pypi,
)
from portage_pip_fuse.prefetcher import (
    RepositoryScanner,
    PyPIPrefetcher,
    create_prefetched_translator,
)

__all__ = [
    "PortagePipFS",
    "SimpleNameTranslator",
    "CachedNameTranslator",
    "pypi_to_gentoo",
    "gentoo_to_pypi",
    "RepositoryScanner",
    "PyPIPrefetcher",
    "create_prefetched_translator",
    "__version__",
]