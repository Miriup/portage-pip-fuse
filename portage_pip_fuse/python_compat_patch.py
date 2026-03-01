"""
PYTHON_COMPAT patching system for runtime modification of Python implementation support.

This module provides a virtual filesystem API for overriding auto-detected PYTHON_COMPAT
values that may be incorrect or overly restrictive.

Patch Operations:
- ADD (++): Add Python implementation to PYTHON_COMPAT
- REMOVE (--): Remove Python implementation from PYTHON_COMPAT
- SET (==): Replace entire PYTHON_COMPAT list

Patch File Format:
    ++ python3_13          # Add implementation
    -- python3_14          # Remove implementation
    == python3_11 python3_12 python3_13   # Set explicit list (replace all)

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from .constants import get_mount_point_key

logger = logging.getLogger(__name__)

# Current patch file format version
PATCH_FILE_VERSION = 3


@dataclass
class PythonCompatPatch:
    """
    Represents a single PYTHON_COMPAT modification.

    Attributes:
        operation: One of 'add', 'remove', 'set'
        impl: Single implementation (for add/remove)
        impls: List of implementations (for set)
        timestamp: Unix timestamp when patch was created

    Examples:
        >>> patch = PythonCompatPatch('add', 'python3_13', None, 1700000000.0)
        >>> patch.operation
        'add'
        >>> patch = PythonCompatPatch('remove', 'python3_14', None, 1700000000.0)
        >>> patch.impl
        'python3_14'
        >>> patch = PythonCompatPatch('set', None, ['python3_11', 'python3_12'], 1700000000.0)
        >>> patch.impls
        ['python3_11', 'python3_12']
    """
    operation: str  # 'add', 'remove', 'set'
    impl: Optional[str]  # Single implementation (for add/remove)
    impls: Optional[List[str]]  # List of implementations (for set)
    timestamp: float  # Unix timestamp

    def __post_init__(self):
        """Validate the patch operation."""
        if self.operation not in ('add', 'remove', 'set'):
            raise ValueError(f"Invalid operation: {self.operation}")
        if self.operation in ('add', 'remove') and self.impl is None:
            raise ValueError(f"{self.operation.capitalize()} operation requires impl")
        if self.operation == 'set' and not self.impls:
            raise ValueError("Set operation requires impls list")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PythonCompatPatch':
        """Create from dictionary."""
        return cls(**data)

    def to_patch_line(self) -> str:
        """
        Convert to patch file format line.

        Returns:
            Patch file line (++ impl, -- impl, or == impl1 impl2 ...)

        Examples:
            >>> patch = PythonCompatPatch('add', 'python3_13', None, 0)
            >>> patch.to_patch_line()
            '++ python3_13'
            >>> patch = PythonCompatPatch('remove', 'python3_14', None, 0)
            >>> patch.to_patch_line()
            '-- python3_14'
            >>> patch = PythonCompatPatch('set', None, ['python3_11', 'python3_12'], 0)
            >>> patch.to_patch_line()
            '== python3_11 python3_12'
        """
        if self.operation == 'add':
            return f"++ {self.impl}"
        elif self.operation == 'remove':
            return f"-- {self.impl}"
        elif self.operation == 'set':
            return f"== {' '.join(self.impls)}"
        return ""

    @classmethod
    def from_patch_line(cls, line: str, timestamp: Optional[float] = None) -> Optional['PythonCompatPatch']:
        """
        Parse a patch file line.

        Args:
            line: Patch file line
            timestamp: Timestamp to use (default: current time)

        Returns:
            PythonCompatPatch or None if line is invalid

        Examples:
            >>> patch = PythonCompatPatch.from_patch_line('++ python3_13')
            >>> patch.operation
            'add'
            >>> patch = PythonCompatPatch.from_patch_line('-- python3_14')
            >>> patch.operation
            'remove'
            >>> patch = PythonCompatPatch.from_patch_line('== python3_11 python3_12')
            >>> patch.operation
            'set'
            >>> patch.impls
            ['python3_11', 'python3_12']
        """
        if timestamp is None:
            timestamp = time.time()

        line = line.strip()
        if not line or line.startswith('#'):
            return None

        if line.startswith('++ '):
            # Add: ++ impl
            impl = line[3:].strip()
            if impl:
                return cls('add', impl, None, timestamp)
        elif line.startswith('-- '):
            # Remove: -- impl
            impl = line[3:].strip()
            if impl:
                return cls('remove', impl, None, timestamp)
        elif line.startswith('== '):
            # Set: == impl1 impl2 ...
            impls = line[3:].split()
            if impls:
                return cls('set', None, impls, timestamp)

        return None


@dataclass
class PackageCompatPatches:
    """
    Collection of PYTHON_COMPAT patches for a specific package version.

    Attributes:
        category: Package category (e.g., 'dev-python')
        package: Package name (e.g., 'pillow')
        version: Version string or '_all' for all versions
        patches: List of PYTHON_COMPAT patches

    Examples:
        >>> pp = PackageCompatPatches('dev-python', 'pillow', '9.4.0', [])
        >>> pp.category
        'dev-python'
        >>> pp.is_all_versions
        False
        >>> pp_all = PackageCompatPatches('dev-python', 'pillow', '_all', [])
        >>> pp_all.is_all_versions
        True
    """
    category: str
    package: str
    version: str  # Version string or '_all' for all versions
    patches: List[PythonCompatPatch] = field(default_factory=list)

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
            'patches': [p.to_dict() for p in self.patches]
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PackageCompatPatches':
        """Create from dictionary."""
        patches = [PythonCompatPatch.from_dict(p) for p in data.get('patches', [])]
        return cls(
            category=data['category'],
            package=data['package'],
            version=data['version'],
            patches=patches
        )


class PythonCompatPatchStore:
    """
    Storage and application of PYTHON_COMPAT patches.

    This class manages patches that override Python implementation compatibility,
    persisting them to JSON and applying them during ebuild generation.

    Attributes:
        storage_path: Path to the JSON file storing patches
        patches: Dictionary mapping package keys to PackageCompatPatches

    Examples:
        >>> import tempfile
        >>> with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        ...     store = PythonCompatPatchStore(f.name)
        >>> store.add_impl('dev-python', 'pillow', '9.4.0', 'python3_13')
        >>> len(store.get_patches('dev-python', 'pillow', '9.4.0'))
        1
        >>> import os; os.unlink(f.name)
    """

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
        self.patches: Dict[str, PackageCompatPatches] = {}
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
            version = data.get('version', 1)

            if version >= 3 and 'mount_points' in data:
                # v3 format: mount_points -> {mount_point -> {python_compat_patches: [...]}}
                if self.mount_point and self.mount_point in data['mount_points']:
                    mp_data = data['mount_points'][self.mount_point]
                    for item in mp_data.get('python_compat_patches', []):
                        pp = PackageCompatPatches.from_dict(item)
                        self.patches[pp.key] = pp
                # If mount_point not found, we'll have empty patches (new namespace)
            else:
                # v1/v2 legacy format: python_compat_patches at top level
                for item in data.get('python_compat_patches', []):
                    pp = PackageCompatPatches.from_dict(item)
                    self.patches[pp.key] = pp

            logger.info(f"Loaded {len(self.patches)} PYTHON_COMPAT patches from {self.storage_path}"
                       + (f" (mount: {self.mount_point})" if self.mount_point else ""))

        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.error(f"Failed to load PYTHON_COMPAT patches from {self.storage_path}: {e}")
            self.patches = {}

    def save(self) -> bool:
        """
        Save patches to JSON file atomically.

        This method preserves existing data in the file (other mount points,
        and other patch types) and only updates the python_compat_patches section
        for this mount point.

        When migrating from v1/v2 to v3 format, existing patches are moved to
        the current mount point's namespace.

        Returns:
            True if save was successful, False otherwise

        Examples:
            >>> import tempfile
            >>> with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            ...     store = PythonCompatPatchStore(f.name)
            >>> store.add_impl('dev-python', 'test', '1.0', 'python3_13')
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
                # Move legacy python_compat_patches to this mount point
                if 'python_compat_patches' in existing_data:
                    existing_data['mount_points'][mp_key]['python_compat_patches'] = existing_data.pop('python_compat_patches')
            else:
                existing_data['version'] = PATCH_FILE_VERSION
                if 'mount_points' not in existing_data:
                    existing_data['mount_points'] = {}

            # Update patches for this mount point
            mp_key = self.mount_point or '_default'
            if mp_key not in existing_data['mount_points']:
                existing_data['mount_points'][mp_key] = {}
            existing_data['mount_points'][mp_key]['python_compat_patches'] = [pp.to_dict() for pp in self.patches.values()]

            # Write to temporary file first
            temp_path = self.storage_path.with_suffix('.tmp')
            with temp_path.open('w', encoding='utf-8') as f:
                json.dump(existing_data, f, indent=2)

            # Atomic rename
            temp_path.rename(self.storage_path)
            self._dirty = False

            logger.debug(f"Saved {len(self.patches)} PYTHON_COMPAT patches to {self.storage_path}"
                        + (f" (mount: {self.mount_point})" if self.mount_point else ""))
            return True

        except OSError as e:
            logger.error(f"Failed to save PYTHON_COMPAT patches to {self.storage_path}: {e}")
            return False

    def _get_or_create_patches(self, category: str, package: str, version: str) -> PackageCompatPatches:
        """Get or create PackageCompatPatches for a package/version."""
        key = f"{category}/{package}/{version}"
        if key not in self.patches:
            self.patches[key] = PackageCompatPatches(category, package, version, [])
        return self.patches[key]

    def add_impl(self, category: str, package: str, version: str, impl: str) -> None:
        """
        Add a Python implementation to PYTHON_COMPAT.

        Args:
            category: Package category (e.g., 'dev-python')
            package: Package name
            version: Version string or '_all'
            impl: Python implementation to add (e.g., 'python3_13')

        Examples:
            >>> store = PythonCompatPatchStore()
            >>> store.add_impl('dev-python', 'pillow', '9.4.0', 'python3_13')
            >>> patches = store.get_patches('dev-python', 'pillow', '9.4.0')
            >>> len(patches)
            1
            >>> patches[0].operation
            'add'
        """
        pp = self._get_or_create_patches(category, package, version)
        patch = PythonCompatPatch('add', impl, None, time.time())
        pp.patches.append(patch)
        self._dirty = True
        logger.info(f"Added {impl} to PYTHON_COMPAT for {category}/{package}/{version}")

    def remove_impl(self, category: str, package: str, version: str, impl: str) -> None:
        """
        Remove a Python implementation from PYTHON_COMPAT.

        Args:
            category: Package category (e.g., 'dev-python')
            package: Package name
            version: Version string or '_all'
            impl: Python implementation to remove (e.g., 'python3_14')

        Examples:
            >>> store = PythonCompatPatchStore()
            >>> store.remove_impl('dev-python', 'pillow', '9.4.0', 'python3_14')
            >>> patches = store.get_patches('dev-python', 'pillow', '9.4.0')
            >>> patches[0].operation
            'remove'
        """
        pp = self._get_or_create_patches(category, package, version)
        patch = PythonCompatPatch('remove', impl, None, time.time())
        pp.patches.append(patch)
        self._dirty = True
        logger.info(f"Removed {impl} from PYTHON_COMPAT for {category}/{package}/{version}")

    def set_impls(self, category: str, package: str, version: str, impls: List[str]) -> None:
        """
        Set explicit PYTHON_COMPAT list (replaces auto-detected).

        Args:
            category: Package category (e.g., 'dev-python')
            package: Package name
            version: Version string or '_all'
            impls: List of Python implementations

        Examples:
            >>> store = PythonCompatPatchStore()
            >>> store.set_impls('dev-python', 'pillow', '9.4.0', ['python3_11', 'python3_12'])
            >>> patches = store.get_patches('dev-python', 'pillow', '9.4.0')
            >>> patches[0].operation
            'set'
            >>> patches[0].impls
            ['python3_11', 'python3_12']
        """
        pp = self._get_or_create_patches(category, package, version)
        patch = PythonCompatPatch('set', None, impls, time.time())
        pp.patches.append(patch)
        self._dirty = True
        logger.info(f"Set PYTHON_COMPAT to {impls} for {category}/{package}/{version}")

    def get_patches(self, category: str, package: str, version: str) -> List[PythonCompatPatch]:
        """
        Get all patches for a specific package version.

        Returns patches for both the specific version AND _all patches,
        with _all patches applied first, then version-specific patches.

        Args:
            category: Package category
            package: Package name
            version: Version string

        Returns:
            List of PythonCompatPatch objects in application order

        Examples:
            >>> store = PythonCompatPatchStore()
            >>> store.add_impl('dev-python', 'pillow', '_all', 'python3_13')
            >>> store.remove_impl('dev-python', 'pillow', '9.4.0', 'python3_14')
            >>> patches = store.get_patches('dev-python', 'pillow', '9.4.0')
            >>> len(patches)
            2
            >>> patches[0].impl
            'python3_13'
            >>> patches[1].impl
            'python3_14'
        """
        result = []

        # First apply _all patches (sorted by timestamp)
        all_key = f"{category}/{package}/_all"
        if all_key in self.patches:
            all_patches = sorted(self.patches[all_key].patches, key=lambda p: p.timestamp)
            result.extend(all_patches)

        # Then apply version-specific patches (sorted by timestamp)
        if version != '_all':
            ver_key = f"{category}/{package}/{version}"
            if ver_key in self.patches:
                ver_patches = sorted(self.patches[ver_key].patches, key=lambda p: p.timestamp)
                result.extend(ver_patches)

        return result

    def has_patches(self, category: str, package: str, version: str) -> bool:
        """Check if any patches exist for a package version."""
        return len(self.get_patches(category, package, version)) > 0

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
            if key.startswith(prefix):
                version = key[len(prefix):]
                versions.append(version)
        return sorted(versions)

    def apply_patches(self, category: str, package: str, version: str,
                      python_compat: List[str]) -> List[str]:
        """
        Apply patches to a PYTHON_COMPAT list.

        Args:
            category: Package category
            package: Package name
            version: Version string
            python_compat: Original list of Python implementations

        Returns:
            Modified list of Python implementations

        Examples:
            >>> store = PythonCompatPatchStore()
            >>> store.remove_impl('dev-python', 'test', '1.0', 'python3_14')
            >>> store.add_impl('dev-python', 'test', '1.0', 'python3_13')
            >>> compat = ['python3_11', 'python3_12', 'python3_14']
            >>> result = store.apply_patches('dev-python', 'test', '1.0', compat)
            >>> 'python3_14' in result
            False
            >>> 'python3_13' in result
            True
        """
        patches = self.get_patches(category, package, version)
        if not patches:
            return python_compat

        # Work with a copy
        result = list(python_compat)

        for patch in patches:
            if patch.operation == 'add':
                # Add implementation if not already present
                if patch.impl not in result:
                    result.append(patch.impl)

            elif patch.operation == 'remove':
                # Remove implementation
                result = [impl for impl in result if impl != patch.impl]

            elif patch.operation == 'set':
                # Replace entire list
                result = list(patch.impls)

        return result

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
            >>> store = PythonCompatPatchStore()
            >>> store.add_impl('dev-python', 'pillow', '9.4.0', 'python3_13')
            >>> content = store.generate_patch_file('dev-python', 'pillow', '9.4.0')
            >>> '++ python3_13' in content
            True
        """
        patches = self.get_patches(category, package, version)
        lines = [
            f"# PYTHON_COMPAT patches for {category}/{package}/{version}",
            f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            ""
        ]

        for patch in patches:
            lines.append(patch.to_patch_line())

        return '\n'.join(lines) + '\n'

    def parse_patch_file(self, content: str, category: str, package: str, version: str) -> int:
        """
        Parse and import patches from patch file content.

        Args:
            content: Patch file content
            category: Target package category
            package: Target package name
            version: Target version

        Returns:
            Number of patches imported

        Examples:
            >>> store = PythonCompatPatchStore()
            >>> content = '''
            ... # Patches
            ... ++ python3_13
            ... -- python3_14
            ... '''
            >>> count = store.parse_patch_file(content, 'dev-python', 'test', '1.0')
            >>> count
            2
        """
        pp = self._get_or_create_patches(category, package, version)
        count = 0
        timestamp = time.time()

        for line in content.splitlines():
            patch = PythonCompatPatch.from_patch_line(line, timestamp)
            if patch:
                pp.patches.append(patch)
                count += 1
                timestamp += 0.001  # Ensure unique timestamps

        if count > 0:
            self._dirty = True
            logger.info(f"Imported {count} PYTHON_COMPAT patches for {category}/{package}/{version}")

        return count

    def clear_patches(self, category: str, package: str, version: str) -> int:
        """
        Clear all patches for a specific package version.

        Returns:
            Number of patches cleared
        """
        key = f"{category}/{package}/{version}"
        if key in self.patches:
            count = len(self.patches[key].patches)
            del self.patches[key]
            self._dirty = True
            logger.info(f"Cleared {count} PYTHON_COMPAT patches for {key}")
            return count
        return 0

    def list_patched_packages(self) -> List[Tuple[str, str, str]]:
        """
        List all packages that have patches.

        Returns:
            List of (category, package, version) tuples
        """
        result = []
        for key in sorted(self.patches.keys()):
            parts = key.split('/')
            if len(parts) == 3:
                result.append((parts[0], parts[1], parts[2]))
        return result

    @property
    def is_dirty(self) -> bool:
        """Check if there are unsaved changes."""
        return self._dirty

    def get_current_impls(self, category: str, package: str, version: str,
                          original_impls: List[str]) -> List[str]:
        """
        Get the current (patched) implementation list for display.

        Args:
            category: Package category
            package: Package name
            version: Version string
            original_impls: Original auto-detected implementations

        Returns:
            List of current implementations after patches applied
        """
        return self.apply_patches(category, package, version, original_impls)
