"""
PEP517 backend patching system for runtime modification of DISTUTILS_USE_PEP517.

This module provides a virtual filesystem API for overriding the PEP517 build backend
for packages that require a different backend than the default.

The global default can be configured via .sys/pep517-default (defaults to 'setuptools').

Valid Backend Values:
- setuptools     : setuptools backend (default)
- standalone     : Auto-detect
- flit           : flit_core backend
- flit_core      : flit_core backend (alias)
- hatchling      : hatchling backend
- poetry         : poetry-core backend
- pdm-backend    : pdm backend
- maturin        : maturin (Rust) backend
- meson-python   : meson-python backend
- scikit-build-core : scikit-build-core backend
- sip            : sip backend
- uv-build       : uv build backend (note: hyphen, not underscore)
- no             : Disable PEP517 (legacy setup.py)

Patch File Format:
    # Comments start with #
    == flit

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from .constants import get_mount_point_key

logger = logging.getLogger(__name__)

# Current patch file format version
PATCH_FILE_VERSION = 3

# Valid PEP517 backend values
VALID_PEP517_BACKENDS = {
    'setuptools',       # setuptools backend (default fallback)
    'standalone',       # Auto-detect
    'flit',             # flit_core
    'flit_core',
    'hatchling',
    'poetry',           # poetry-core
    'pdm-backend',
    'maturin',
    'meson-python',
    'scikit-build-core',
    'sip',
    'uv-build',         # uv build backend (note: hyphen, not underscore)
    'no',               # Disable PEP517
}


def is_valid_pep517_backend(backend: str) -> bool:
    """
    Check if a PEP517 backend name is valid.

    Args:
        backend: The backend name to validate

    Returns:
        True if valid, False otherwise

    Examples:
        >>> is_valid_pep517_backend('flit')
        True
        >>> is_valid_pep517_backend('setuptools')
        True
        >>> is_valid_pep517_backend('invalid')
        False
        >>> is_valid_pep517_backend('.swp')
        False
    """
    return backend in VALID_PEP517_BACKENDS


@dataclass
class PEP517Patch:
    """
    Represents a PEP517 backend override.

    Attributes:
        backend: The PEP517 backend to use
        timestamp: Unix timestamp when patch was created

    Examples:
        >>> patch = PEP517Patch('flit', 1700000000.0)
        >>> patch.backend
        'flit'
    """
    backend: str
    timestamp: float

    def __post_init__(self):
        """Validate the patch."""
        if not is_valid_pep517_backend(self.backend):
            raise ValueError(f"Invalid PEP517 backend: {self.backend}")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PEP517Patch':
        """Create from dictionary."""
        return cls(**data)


@dataclass
class PackagePEP517Patch:
    """
    PEP517 backend override for a specific package version.

    Attributes:
        category: Package category (e.g., 'dev-python')
        package: Package name (e.g., 'pypdf')
        version: Version string or '_all' for all versions
        patch: The PEP517 patch (or None if not set)

    Examples:
        >>> pp = PackagePEP517Patch('dev-python', 'pypdf', '5.4.0', None)
        >>> pp.category
        'dev-python'
        >>> pp.is_all_versions
        False
        >>> pp_all = PackagePEP517Patch('dev-python', 'pypdf', '_all', None)
        >>> pp_all.is_all_versions
        True
    """
    category: str
    package: str
    version: str  # Version string or '_all' for all versions
    patch: Optional[PEP517Patch] = None

    @property
    def is_all_versions(self) -> bool:
        """Check if this applies to all versions."""
        return self.version == '_all'

    @property
    def key(self) -> str:
        """Generate unique key for this package/version combination."""
        return f"{self.category}/{self.package}/{self.version}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'category': self.category,
            'package': self.package,
            'version': self.version,
            'patch': self.patch.to_dict() if self.patch else None
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PackagePEP517Patch':
        """Create from dictionary."""
        patch = PEP517Patch.from_dict(data['patch']) if data.get('patch') else None
        return cls(
            category=data['category'],
            package=data['package'],
            version=data['version'],
            patch=patch
        )


class PEP517PatchStore:
    """
    Storage and application of PEP517 backend patches.

    This class manages patches that override the PEP517 build backend for packages,
    persisting them to JSON and applying them during ebuild generation.

    Attributes:
        storage_path: Path to the JSON file storing patches
        patches: Dictionary mapping package keys to PackagePEP517Patch

    Examples:
        >>> import tempfile
        >>> with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        ...     store = PEP517PatchStore(f.name)
        >>> store.set_backend('dev-python', 'pypdf', '_all', 'flit')
        >>> store.get_backend('dev-python', 'pypdf', '5.4.0')
        'flit'
        >>> import os; os.unlink(f.name)
    """

    # Default fallback when no default is configured
    FALLBACK_DEFAULT = 'setuptools'

    def __init__(self, storage_path: Optional[str] = None, mount_point: Optional[str] = None):
        """
        Initialize the patch store.

        Args:
            storage_path: Path to JSON file for persistence (None for memory-only)
            mount_point: Mount point path for namespaced configuration

        Note:
            WARNING: Race conditions with concurrent mounts

            When multiple FUSE instances share the same patches.json file,
            concurrent saves may cause one instance's changes to be lost.
            Each instance reads full file, modifies its section, writes back.

            Mitigation: Each mount point has isolated namespace.
            For guaranteed isolation: use separate --patch-file per mount.
        """
        self.storage_path = Path(storage_path) if storage_path else None
        self.mount_point = get_mount_point_key(mount_point) if mount_point else None
        self.patches: Dict[str, PackagePEP517Patch] = {}
        self._default_backend: Optional[str] = None  # Configurable default
        self._dirty = False

        if self.storage_path and self.storage_path.exists():
            self._load()

    def _load(self) -> None:
        """Load patches from JSON file."""
        if not self.storage_path or not self.storage_path.exists():
            return

        try:
            with self.storage_path.open('r', encoding='utf-8') as f:
                data = json.load(f)

            self.patches = {}
            self._default_backend = None
            version = data.get('version', 1)

            if version >= 3 and 'mount_points' in data:
                # v3 format: mount_points -> {mount_point -> {pep517_patches: [...], pep517_default: "..."}}
                if self.mount_point and self.mount_point in data['mount_points']:
                    mp_data = data['mount_points'][self.mount_point]
                    for item in mp_data.get('pep517_patches', []):
                        pp = PackagePEP517Patch.from_dict(item)
                        self.patches[pp.key] = pp
                    # Load default backend
                    default = mp_data.get('pep517_default')
                    if default and is_valid_pep517_backend(default):
                        self._default_backend = default
                # If mount_point not found, we'll have empty patches (new namespace)
            else:
                # v1/v2/v4 legacy format: pep517_patches at top level
                for item in data.get('pep517_patches', []):
                    pp = PackagePEP517Patch.from_dict(item)
                    self.patches[pp.key] = pp
                # Legacy default
                default = data.get('pep517_default')
                if default and is_valid_pep517_backend(default):
                    self._default_backend = default

            logger.info(f"Loaded {len(self.patches)} PEP517 patches from {self.storage_path}"
                       + (f" (mount: {self.mount_point})" if self.mount_point else "")
                       + (f" (default: {self._default_backend})" if self._default_backend else ""))

        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.error(f"Failed to load PEP517 patches from {self.storage_path}: {e}")
            self.patches = {}

    def save(self) -> bool:
        """
        Save patches to JSON file atomically.

        This method preserves existing data in the file (other mount points,
        and other patch types) and only updates the pep517_patches section
        for this mount point.

        Returns:
            True if save was successful, False otherwise

        Examples:
            >>> import tempfile
            >>> with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            ...     store = PEP517PatchStore(f.name)
            >>> store.set_backend('dev-python', 'test', '1.0', 'flit')
            >>> store.save()
            True
            >>> import os; os.unlink(f.name)
        """
        if not self.storage_path:
            return True  # Memory-only mode

        try:
            # Ensure directory exists
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)

            # Load existing data to preserve other sections
            existing_data = {}
            if self.storage_path.exists():
                try:
                    with self.storage_path.open('r', encoding='utf-8') as f:
                        existing_data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass

            old_version = existing_data.get('version', 1)

            # Migrate to v3 format if needed
            if old_version < 3:
                # Move existing patches to mount_points structure
                existing_data['version'] = PATCH_FILE_VERSION
                if 'mount_points' not in existing_data:
                    existing_data['mount_points'] = {}
                # Legacy data gets assigned to current mount point (or default key)
                mp_key = self.mount_point or '_default'
                if mp_key not in existing_data['mount_points']:
                    existing_data['mount_points'][mp_key] = {}
                # Move legacy pep517_patches to this mount point
                if 'pep517_patches' in existing_data:
                    existing_data['mount_points'][mp_key]['pep517_patches'] = existing_data.pop('pep517_patches')
            else:
                existing_data['version'] = PATCH_FILE_VERSION
                if 'mount_points' not in existing_data:
                    existing_data['mount_points'] = {}

            # Update patches for this mount point
            mp_key = self.mount_point or '_default'
            if mp_key not in existing_data['mount_points']:
                existing_data['mount_points'][mp_key] = {}
            existing_data['mount_points'][mp_key]['pep517_patches'] = [
                pp.to_dict() for pp in self.patches.values() if pp.patch is not None
            ]
            # Save default backend
            if self._default_backend:
                existing_data['mount_points'][mp_key]['pep517_default'] = self._default_backend
            elif 'pep517_default' in existing_data['mount_points'][mp_key]:
                del existing_data['mount_points'][mp_key]['pep517_default']

            # Write to temporary file first
            temp_path = self.storage_path.with_suffix('.tmp')
            with temp_path.open('w', encoding='utf-8') as f:
                json.dump(existing_data, f, indent=2)

            # Atomic rename
            temp_path.rename(self.storage_path)
            self._dirty = False

            logger.debug(f"Saved {len(self.patches)} PEP517 patches to {self.storage_path}"
                        + (f" (mount: {self.mount_point})" if self.mount_point else ""))
            return True

        except OSError as e:
            logger.error(f"Failed to save PEP517 patches to {self.storage_path}: {e}")
            return False

    def _get_or_create_patch(self, category: str, package: str, version: str) -> PackagePEP517Patch:
        """Get or create PackagePEP517Patch for a package/version."""
        key = f"{category}/{package}/{version}"
        if key not in self.patches:
            self.patches[key] = PackagePEP517Patch(category, package, version, None)
        return self.patches[key]

    def set_backend(self, category: str, package: str, version: str, backend: str) -> None:
        """
        Set the PEP517 backend for a package version.

        Args:
            category: Package category (e.g., 'dev-python')
            package: Package name
            version: Version string or '_all'
            backend: PEP517 backend value (e.g., 'flit', 'hatchling')

        Raises:
            ValueError: If backend is not valid

        Examples:
            >>> store = PEP517PatchStore()
            >>> store.set_backend('dev-python', 'pypdf', '_all', 'flit')
            >>> store.get_backend('dev-python', 'pypdf', '5.4.0')
            'flit'
        """
        if not is_valid_pep517_backend(backend):
            raise ValueError(f"Invalid PEP517 backend: {backend}. Valid values: {', '.join(sorted(VALID_PEP517_BACKENDS))}")

        pp = self._get_or_create_patch(category, package, version)
        pp.patch = PEP517Patch(backend, time.time())
        self._dirty = True
        logger.info(f"Set PEP517 backend to '{backend}' for {category}/{package}/{version}")

    def get_backend(self, category: str, package: str, version: str) -> Optional[str]:
        """
        Get the patched PEP517 backend for a package version.

        Checks version-specific patches first, then _all patches.

        Args:
            category: Package category
            package: Package name
            version: Version string

        Returns:
            Backend string if patched, None otherwise

        Examples:
            >>> store = PEP517PatchStore()
            >>> store.set_backend('dev-python', 'pypdf', '_all', 'flit')
            >>> store.get_backend('dev-python', 'pypdf', '5.4.0')
            'flit'
            >>> store.get_backend('dev-python', 'other', '1.0') is None
            True
        """
        # First check version-specific patch
        ver_key = f"{category}/{package}/{version}"
        if ver_key in self.patches and self.patches[ver_key].patch:
            return self.patches[ver_key].patch.backend

        # Then check _all patch
        all_key = f"{category}/{package}/_all"
        if all_key in self.patches and self.patches[all_key].patch:
            return self.patches[all_key].patch.backend

        return None

    def remove_backend(self, category: str, package: str, version: str) -> bool:
        """
        Remove the PEP517 backend patch for a package version.

        Args:
            category: Package category
            package: Package name
            version: Version string or '_all'

        Returns:
            True if removed, False if not found

        Examples:
            >>> store = PEP517PatchStore()
            >>> store.set_backend('dev-python', 'pypdf', '5.4.0', 'flit')
            >>> store.remove_backend('dev-python', 'pypdf', '5.4.0')
            True
            >>> store.get_backend('dev-python', 'pypdf', '5.4.0') is None
            True
        """
        key = f"{category}/{package}/{version}"
        if key in self.patches:
            del self.patches[key]
            self._dirty = True
            logger.info(f"Removed PEP517 backend patch for {category}/{package}/{version}")
            return True
        return False

    def has_patch(self, category: str, package: str, version: str) -> bool:
        """Check if a patch exists for a package version."""
        return self.get_backend(category, package, version) is not None

    def get_default_backend(self) -> str:
        """
        Get the configured default PEP517 backend.

        Returns the configured default, or 'setuptools' if none is configured.

        Returns:
            The default backend string

        Examples:
            >>> store = PEP517PatchStore()
            >>> store.get_default_backend()
            'setuptools'
            >>> store.set_default_backend('flit')
            >>> store.get_default_backend()
            'flit'
        """
        return self._default_backend or self.FALLBACK_DEFAULT

    def set_default_backend(self, backend: str) -> None:
        """
        Set the default PEP517 backend for all packages.

        Args:
            backend: The default backend value (e.g., 'setuptools', 'flit')

        Raises:
            ValueError: If backend is not valid

        Examples:
            >>> store = PEP517PatchStore()
            >>> store.set_default_backend('flit')
            >>> store.get_default_backend()
            'flit'
        """
        if not is_valid_pep517_backend(backend):
            raise ValueError(f"Invalid PEP517 backend: {backend}. Valid values: {', '.join(sorted(VALID_PEP517_BACKENDS))}")
        self._default_backend = backend
        self._dirty = True
        logger.info(f"Set default PEP517 backend to '{backend}'")

    def clear_default_backend(self) -> None:
        """
        Clear the configured default PEP517 backend.

        After clearing, get_default_backend() will return 'setuptools'.

        Examples:
            >>> store = PEP517PatchStore()
            >>> store.set_default_backend('flit')
            >>> store.clear_default_backend()
            >>> store.get_default_backend()
            'setuptools'
        """
        if self._default_backend is not None:
            self._default_backend = None
            self._dirty = True
            logger.info("Cleared default PEP517 backend")

    def get_package_versions_with_patches(self, category: str, package: str) -> List[str]:
        """
        Get all versions that have patches for a package.

        Args:
            category: Package category
            package: Package name

        Returns:
            List of version strings (including '_all' if present)
        """
        prefix = f"{category}/{package}/"
        versions = []
        for key in self.patches:
            if key.startswith(prefix) and self.patches[key].patch:
                version = key[len(prefix):]
                versions.append(version)
        return sorted(versions)

    def generate_patch_file(self, category: str, package: str, version: str) -> str:
        """
        Generate portable patch file content for a package version.

        Args:
            category: Package category
            package: Package name
            version: Version string

        Returns:
            Patch file content as string

        Examples:
            >>> store = PEP517PatchStore()
            >>> store.set_backend('dev-python', 'pypdf', '_all', 'flit')
            >>> content = store.generate_patch_file('dev-python', 'pypdf', '_all')
            >>> '== flit' in content
            True
        """
        backend = self.get_backend(category, package, version)
        lines = [
            f"# PEP517 backend patch for {category}/{package}/{version}",
            f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            ""
        ]

        if backend:
            lines.append(f"== {backend}")

        return '\n'.join(lines) + '\n'

    def parse_patch_file(self, content: str, category: str, package: str, version: str) -> int:
        """
        Parse and import patch from patch file content.

        Args:
            content: Patch file content
            category: Target package category
            package: Target package name
            version: Target version

        Returns:
            Number of patches imported (0 or 1)

        Examples:
            >>> store = PEP517PatchStore()
            >>> content = '''
            ... # Patch
            ... == flit
            ... '''
            >>> count = store.parse_patch_file(content, 'dev-python', 'pypdf', '_all')
            >>> count
            1
            >>> store.get_backend('dev-python', 'pypdf', '5.4.0')
            'flit'
        """
        count = 0

        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            if line.startswith('== '):
                backend = line[3:].strip()
                if is_valid_pep517_backend(backend):
                    self.set_backend(category, package, version, backend)
                    count = 1
                    break  # Only one backend per patch file
                else:
                    logger.warning(f"Invalid PEP517 backend in patch file: {backend}")

        return count

    def list_patched_packages(self) -> List[Tuple[str, str, str]]:
        """
        List all packages that have patches.

        Returns:
            List of (category, package, version) tuples
        """
        result = []
        for key in sorted(self.patches.keys()):
            if self.patches[key].patch:
                parts = key.split('/')
                if len(parts) == 3:
                    result.append((parts[0], parts[1], parts[2]))
        return result

    @property
    def is_dirty(self) -> bool:
        """Check if there are unsaved changes."""
        return self._dirty
