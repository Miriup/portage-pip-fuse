"""
Constants used across the portage-pip-fuse filesystem.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import os
from pathlib import Path
from typing import Optional

# Repository name that identifies this FUSE filesystem to portage
REPO_NAME = "portage-pip-fuse"

# Default repository location (mountpoint)
REPO_LOCATION = "/var/db/repos/pypi"

# Cache directory candidates (in order of preference)
CACHE_DIR_USER = Path.home() / '.cache' / 'portage-pip-fuse'
CACHE_DIR_SYSTEM = Path('/var/cache/portage-pip-fuse')

CACHE_DIR_CANDIDATES = [
    CACHE_DIR_USER,
    CACHE_DIR_SYSTEM,
]

# Default cache directory (first candidate)
DEFAULT_CACHE_DIR = CACHE_DIR_USER

# Cache time-to-live in seconds (1 hour)
DEFAULT_CACHE_TTL = 3600

# Maximum depth for dependency resolution
DEFAULT_MAX_DEPENDENCY_DEPTH = 10


def find_cache_dir(explicit_dir: Optional[str] = None) -> Path:
    """
    Find the best cache directory.

    Checks locations in order of preference:
    1. Explicitly specified directory (--cache-dir)
    2. ~/.cache/portage-pip-fuse
    3. /var/cache/portage-pip-fuse

    Returns the first directory that exists and is read/writable,
    or creates and returns the first candidate that can be created.

    Args:
        explicit_dir: Explicitly specified cache directory (highest priority)

    Returns:
        Path to the cache directory
    """
    # If explicit directory specified, use it
    if explicit_dir:
        path = Path(explicit_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
            # Test if writable
            test_file = path / '.write_test'
            test_file.write_text('test')
            test_file.unlink()
            return path
        except (PermissionError, OSError):
            pass  # Fall through to candidates

    # Check candidates in order
    for candidate in CACHE_DIR_CANDIDATES:
        # Check if it exists and is writable
        if candidate.exists():
            if os.access(candidate, os.R_OK | os.W_OK):
                return candidate
        else:
            # Try to create it
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                # Test if writable
                test_file = candidate / '.write_test'
                test_file.write_text('test')
                test_file.unlink()
                return candidate
            except (PermissionError, OSError):
                continue

    # No writable cache directory found
    raise RuntimeError(
        f"No writable cache directory found. Tried: {', '.join(str(c) for c in CACHE_DIR_CANDIDATES)}"
    )