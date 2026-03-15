"""
Base implementation compatibility patching system.

This module provides abstract base classes for language implementation
compatibility patching (PYTHON_COMPAT / USE_RUBY), shared by both
PyPI and RubyGems ecosystems.

Patch Operations:
- ADD (++): Add implementation to compatibility list
- REMOVE (--): Remove implementation from compatibility list
- SET (==): Replace entire compatibility list

Patch File Format:
    ++ python3_13          # Add implementation (or ruby34)
    -- python3_14          # Remove implementation
    == python3_11 python3_12 python3_13   # Set explicit list

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from .constants import get_mount_point_key

logger = logging.getLogger(__name__)

# Current patch file format version
PATCH_FILE_VERSION = 3


@dataclass
class CompatPatch:
    """
    Represents a single implementation compatibility modification.

    Attributes:
        operation: One of 'add', 'remove', 'set'
        impl: Single implementation (for add/remove)
        impls: List of implementations (for set)
        timestamp: Unix timestamp when patch was created

    Examples:
        >>> patch = CompatPatch('add', 'python3_13', None, 1700000000.0)
        >>> patch.operation
        'add'
        >>> patch = CompatPatch('remove', 'ruby34', None, 1700000000.0)
        >>> patch.impl
        'ruby34'
        >>> patch = CompatPatch('set', None, ['python3_11', 'python3_12'], 1700000000.0)
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
    def from_dict(cls, data: Dict[str, Any]) -> 'CompatPatch':
        """Create from dictionary."""
        return cls(**data)

    def to_patch_line(self) -> str:
        """
        Convert to patch file format line.

        Returns:
            Patch file line (++ impl, -- impl, or == impl1 impl2 ...)

        Examples:
            >>> patch = CompatPatch('add', 'python3_13', None, 0)
            >>> patch.to_patch_line()
            '++ python3_13'
            >>> patch = CompatPatch('remove', 'ruby34', None, 0)
            >>> patch.to_patch_line()
            '-- ruby34'
            >>> patch = CompatPatch('set', None, ['python3_11', 'python3_12'], 0)
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
    def from_patch_line(cls, line: str, timestamp: Optional[float] = None) -> Optional['CompatPatch']:
        """
        Parse a patch file line.

        Args:
            line: Patch file line
            timestamp: Timestamp to use (default: current time)

        Returns:
            CompatPatch or None if line is invalid

        Examples:
            >>> patch = CompatPatch.from_patch_line('++ python3_13')
            >>> patch.operation
            'add'
            >>> patch = CompatPatch.from_patch_line('-- ruby34')
            >>> patch.operation
            'remove'
            >>> patch = CompatPatch.from_patch_line('== python3_11 python3_12')
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
    Collection of compatibility patches for a specific package version.

    Attributes:
        category: Package category (e.g., 'dev-python', 'dev-ruby')
        package: Package name
        version: Version string or '_all' for all versions
        patches: List of compatibility patches

    Examples:
        >>> pp = PackageCompatPatches('dev-python', 'pillow', '9.4.0', [])
        >>> pp.category
        'dev-python'
        >>> pp.is_all_versions
        False
        >>> pp_all = PackageCompatPatches('dev-ruby', 'rails', '_all', [])
        >>> pp_all.is_all_versions
        True
    """
    category: str
    package: str
    version: str  # Version string or '_all' for all versions
    patches: List[CompatPatch] = field(default_factory=list)

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
        patches = [CompatPatch.from_dict(p) for p in data.get('patches', [])]
        return cls(
            category=data['category'],
            package=data['package'],
            version=data['version'],
            patches=patches
        )


class CompatPatchStore(ABC):
    """
    Base class for language implementation compatibility patching.

    This abstract class provides shared functionality for storing and
    applying patches that modify implementation compatibility lists
    (PYTHON_COMPAT or USE_RUBY).

    Subclasses must implement:
    - json_key: Key used in JSON storage
    - get_valid_impls: Get list of valid implementations dynamically
    - is_valid_impl: Validate an implementation name

    Attributes:
        storage_path: Path to the JSON file storing patches
        patches: Dictionary mapping package keys to PackageCompatPatches
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

    @property
    @abstractmethod
    def json_key(self) -> str:
        """Key used in JSON storage (e.g., 'python_compat_patches', 'ruby_compat_patches')."""
        pass

    @abstractmethod
    def get_valid_impls(self) -> List[str]:
        """Get list of valid implementations dynamically."""
        pass

    @abstractmethod
    def is_valid_impl(self, impl: str) -> bool:
        """Validate an implementation name."""
        pass

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
                # v3 format: mount_points -> {mount_point -> {json_key: [...]}}
                if self.mount_point and self.mount_point in data['mount_points']:
                    mp_data = data['mount_points'][self.mount_point]
                    for item in mp_data.get(self.json_key, []):
                        pp = PackageCompatPatches.from_dict(item)
                        self.patches[pp.key] = pp
                # If mount_point not found, we'll have empty patches (new namespace)
            else:
                # v1/v2 legacy format: patches at top level
                for item in data.get(self.json_key, []):
                    pp = PackageCompatPatches.from_dict(item)
                    self.patches[pp.key] = pp

            logger.info(f"Loaded {len(self.patches)} {self.json_key} from {self.storage_path}"
                       + (f" (mount: {self.mount_point})" if self.mount_point else ""))

        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.error(f"Failed to load {self.json_key} from {self.storage_path}: {e}")
            self.patches = {}

    def save(self) -> bool:
        """
        Save patches to JSON file atomically.

        This method preserves existing data in the file (other mount points,
        and other patch types) and only updates the patches section
        for this mount point.

        When migrating from v1/v2 to v3 format, existing patches are moved to
        the current mount point's namespace.

        Returns:
            True if save was successful, False otherwise
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
                # Move legacy patches to this mount point
                if self.json_key in existing_data:
                    existing_data['mount_points'][mp_key][self.json_key] = existing_data.pop(self.json_key)
            else:
                existing_data['version'] = PATCH_FILE_VERSION
                if 'mount_points' not in existing_data:
                    existing_data['mount_points'] = {}

            # Update patches for this mount point
            mp_key = self.mount_point or '_default'
            if mp_key not in existing_data['mount_points']:
                existing_data['mount_points'][mp_key] = {}
            existing_data['mount_points'][mp_key][self.json_key] = [pp.to_dict() for pp in self.patches.values()]

            # Write to temporary file first
            temp_path = self.storage_path.with_suffix('.tmp')
            with temp_path.open('w', encoding='utf-8') as f:
                json.dump(existing_data, f, indent=2)

            # Atomic rename
            temp_path.rename(self.storage_path)
            self._dirty = False

            logger.debug(f"Saved {len(self.patches)} {self.json_key} to {self.storage_path}"
                        + (f" (mount: {self.mount_point})" if self.mount_point else ""))
            return True

        except OSError as e:
            logger.error(f"Failed to save {self.json_key} to {self.storage_path}: {e}")
            return False

    def _get_or_create_patches(self, category: str, package: str, version: str) -> PackageCompatPatches:
        """Get or create PackageCompatPatches for a package/version."""
        key = f"{category}/{package}/{version}"
        if key not in self.patches:
            self.patches[key] = PackageCompatPatches(category, package, version, [])
        return self.patches[key]

    def add_impl(self, category: str, package: str, version: str, impl: str) -> None:
        """
        Add an implementation to compatibility list.

        Args:
            category: Package category (e.g., 'dev-python', 'dev-ruby')
            package: Package name
            version: Version string or '_all'
            impl: Implementation to add (e.g., 'python3_13', 'ruby34')
        """
        pp = self._get_or_create_patches(category, package, version)
        patch = CompatPatch('add', impl, None, time.time())
        pp.patches.append(patch)
        self._dirty = True
        logger.info(f"Added {impl} to {self.json_key} for {category}/{package}/{version}")

    def remove_impl(self, category: str, package: str, version: str, impl: str) -> None:
        """
        Remove an implementation from compatibility list.

        Args:
            category: Package category
            package: Package name
            version: Version string or '_all'
            impl: Implementation to remove
        """
        pp = self._get_or_create_patches(category, package, version)
        patch = CompatPatch('remove', impl, None, time.time())
        pp.patches.append(patch)
        self._dirty = True
        logger.info(f"Removed {impl} from {self.json_key} for {category}/{package}/{version}")

    def set_impls(self, category: str, package: str, version: str, impls: List[str]) -> None:
        """
        Set explicit implementation list (replaces auto-detected).

        Args:
            category: Package category
            package: Package name
            version: Version string or '_all'
            impls: List of implementations
        """
        pp = self._get_or_create_patches(category, package, version)
        patch = CompatPatch('set', None, impls, time.time())
        pp.patches.append(patch)
        self._dirty = True
        logger.info(f"Set {self.json_key} to {impls} for {category}/{package}/{version}")

    def get_patches(self, category: str, package: str, version: str) -> List[CompatPatch]:
        """
        Get all patches for a specific package version.

        Returns patches for both the specific version AND _all patches,
        with _all patches applied first, then version-specific patches.

        Args:
            category: Package category
            package: Package name
            version: Version string

        Returns:
            List of CompatPatch objects in application order
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
                      compat_list: List[str]) -> List[str]:
        """
        Apply patches to a compatibility list.

        Args:
            category: Package category
            package: Package name
            version: Version string
            compat_list: Original list of implementations

        Returns:
            Modified list of implementations
        """
        patches = self.get_patches(category, package, version)
        if not patches:
            return compat_list

        # Work with a copy
        result = list(compat_list)

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
        """
        patches = self.get_patches(category, package, version)
        lines = [
            f"# {self.json_key} for {category}/{package}/{version}",
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
        """
        pp = self._get_or_create_patches(category, package, version)
        count = 0
        timestamp = time.time()

        for line in content.splitlines():
            patch = CompatPatch.from_patch_line(line, timestamp)
            if patch:
                pp.patches.append(patch)
                count += 1
                timestamp += 0.001  # Ensure unique timestamps

        if count > 0:
            self._dirty = True
            logger.info(f"Imported {count} {self.json_key} for {category}/{package}/{version}")

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
            logger.info(f"Cleared {count} {self.json_key} for {key}")
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
