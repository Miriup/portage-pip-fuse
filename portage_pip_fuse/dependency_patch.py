"""
Dependency patching system for runtime modification of PyPI package dependencies.

This module provides a virtual filesystem API for overriding overly-restrictive
version constraints that conflict with system packages installed via portage.

Patch Operations:
- MODIFY (->): Change dependency version constraint
- REMOVE (--): Remove dependency entirely
- ADD (++): Add new dependency

Patch File Format:
    -> =dev-python/xyz-1.0[${PYTHON_USEDEP}] >=dev-python/xyz-1.0[${PYTHON_USEDEP}]
    -- =dev-python/unwanted-1.0[${PYTHON_USEDEP}]
    ++ >=dev-python/needed-2.0[${PYTHON_USEDEP}]

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)


@dataclass
class DependencyPatch:
    """
    Represents a single dependency modification.

    Attributes:
        operation: One of 'add', 'remove', 'modify'
        old_dep: Original dependency string (for modify/remove)
        new_dep: New dependency string (for add/modify)
        timestamp: Unix timestamp when patch was created

    Examples:
        >>> patch = DependencyPatch('modify', '=dev-python/urllib3-1.21', '>=dev-python/urllib3-1.21', 1700000000.0)
        >>> patch.operation
        'modify'
        >>> patch = DependencyPatch('remove', '=dev-python/unwanted-1.0', None, 1700000000.0)
        >>> patch.new_dep is None
        True
    """
    operation: str  # 'add', 'remove', 'modify'
    old_dep: Optional[str]  # Original dependency (for modify/remove)
    new_dep: Optional[str]  # New dependency (for add/modify)
    timestamp: float  # Unix timestamp

    def __post_init__(self):
        """Validate the patch operation."""
        if self.operation not in ('add', 'remove', 'modify'):
            raise ValueError(f"Invalid operation: {self.operation}")
        if self.operation == 'add' and self.new_dep is None:
            raise ValueError("Add operation requires new_dep")
        if self.operation == 'remove' and self.old_dep is None:
            raise ValueError("Remove operation requires old_dep")
        if self.operation == 'modify' and (self.old_dep is None or self.new_dep is None):
            raise ValueError("Modify operation requires both old_dep and new_dep")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DependencyPatch':
        """Create from dictionary."""
        return cls(**data)

    def to_patch_line(self) -> str:
        """
        Convert to patch file format line.

        Returns:
            Patch file line (-> old new, -- old, or ++ new)

        Examples:
            >>> patch = DependencyPatch('modify', '=dep-1.0', '>=dep-1.0', 0)
            >>> patch.to_patch_line()
            '-> =dep-1.0 >=dep-1.0'
            >>> patch = DependencyPatch('remove', '=dep-1.0', None, 0)
            >>> patch.to_patch_line()
            '-- =dep-1.0'
            >>> patch = DependencyPatch('add', None, '>=dep-2.0', 0)
            >>> patch.to_patch_line()
            '++ >=dep-2.0'
        """
        if self.operation == 'modify':
            return f"-> {self.old_dep} {self.new_dep}"
        elif self.operation == 'remove':
            return f"-- {self.old_dep}"
        elif self.operation == 'add':
            return f"++ {self.new_dep}"
        return ""

    @classmethod
    def from_patch_line(cls, line: str, timestamp: Optional[float] = None) -> Optional['DependencyPatch']:
        """
        Parse a patch file line.

        Args:
            line: Patch file line
            timestamp: Timestamp to use (default: current time)

        Returns:
            DependencyPatch or None if line is invalid

        Examples:
            >>> patch = DependencyPatch.from_patch_line('-> =dep-1.0 >=dep-1.0')
            >>> patch.operation
            'modify'
            >>> patch = DependencyPatch.from_patch_line('-- =dep-1.0')
            >>> patch.operation
            'remove'
            >>> patch = DependencyPatch.from_patch_line('++ >=dep-2.0')
            >>> patch.operation
            'add'
        """
        if timestamp is None:
            timestamp = time.time()

        line = line.strip()
        if not line or line.startswith('#'):
            return None

        if line.startswith('-> '):
            # Modify: -> old_dep new_dep
            parts = line[3:].split(None, 1)
            if len(parts) == 2:
                return cls('modify', parts[0], parts[1], timestamp)
        elif line.startswith('-- '):
            # Remove: -- old_dep
            old_dep = line[3:].strip()
            if old_dep:
                return cls('remove', old_dep, None, timestamp)
        elif line.startswith('++ '):
            # Add: ++ new_dep
            new_dep = line[3:].strip()
            if new_dep:
                return cls('add', None, new_dep, timestamp)

        return None


@dataclass
class PackagePatches:
    """
    Collection of patches for a specific package version.

    Attributes:
        category: Package category (e.g., 'dev-python')
        package: Package name (e.g., 'requests')
        version: Version string or '_all' for all versions
        patches: List of dependency patches

    Examples:
        >>> pp = PackagePatches('dev-python', 'requests', '2.31.0', [])
        >>> pp.category
        'dev-python'
        >>> pp.is_all_versions
        False
        >>> pp_all = PackagePatches('dev-python', 'requests', '_all', [])
        >>> pp_all.is_all_versions
        True
    """
    category: str
    package: str
    version: str  # Version string or '_all' for all versions
    patches: List[DependencyPatch] = field(default_factory=list)

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
    def from_dict(cls, data: Dict[str, Any]) -> 'PackagePatches':
        """Create from dictionary."""
        patches = [DependencyPatch.from_dict(p) for p in data.get('patches', [])]
        return cls(
            category=data['category'],
            package=data['package'],
            version=data['version'],
            patches=patches
        )


class DependencyPatchStore:
    """
    Storage and application of dependency patches.

    This class manages patches that override PyPI package dependencies,
    persisting them to JSON and applying them during ebuild generation.

    Attributes:
        storage_path: Path to the JSON file storing patches
        patches: Dictionary mapping package keys to PackagePatches

    Examples:
        >>> import tempfile
        >>> with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        ...     store = DependencyPatchStore(f.name)
        >>> store.add_dependency('dev-python', 'requests', '2.31.0', '>=dev-python/new-dep-1.0[${PYTHON_USEDEP}]')
        >>> len(store.get_patches('dev-python', 'requests', '2.31.0'))
        1
        >>> import os; os.unlink(f.name)
    """

    def __init__(self, storage_path: Optional[str] = None):
        """
        Initialize the patch store.

        Args:
            storage_path: Path to JSON file for persistence (None for memory-only)
        """
        self.storage_path = Path(storage_path) if storage_path else None
        self.patches: Dict[str, PackagePatches] = {}
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
            for item in data.get('patches', []):
                pp = PackagePatches.from_dict(item)
                self.patches[pp.key] = pp

            logger.info(f"Loaded {len(self.patches)} package patches from {self.storage_path}")

        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.error(f"Failed to load patches from {self.storage_path}: {e}")
            self.patches = {}

    def save(self) -> bool:
        """
        Save patches to JSON file atomically.

        Returns:
            True if save was successful, False otherwise

        Examples:
            >>> import tempfile
            >>> with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            ...     store = DependencyPatchStore(f.name)
            >>> store.add_dependency('dev-python', 'test', '1.0', '>=dep-1.0')
            >>> store.save()
            True
            >>> import os; os.unlink(f.name)
        """
        if not self.storage_path:
            return True  # Memory-only mode

        try:
            # Ensure directory exists
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)

            # Write to temporary file first
            temp_path = self.storage_path.with_suffix('.tmp')
            data = {
                'version': 1,
                'patches': [pp.to_dict() for pp in self.patches.values()]
            }

            with temp_path.open('w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)

            # Atomic rename
            temp_path.rename(self.storage_path)
            self._dirty = False

            logger.debug(f"Saved {len(self.patches)} package patches to {self.storage_path}")
            return True

        except OSError as e:
            logger.error(f"Failed to save patches to {self.storage_path}: {e}")
            return False

    def _get_or_create_patches(self, category: str, package: str, version: str) -> PackagePatches:
        """Get or create PackagePatches for a package/version."""
        key = f"{category}/{package}/{version}"
        if key not in self.patches:
            self.patches[key] = PackagePatches(category, package, version, [])
        return self.patches[key]

    def add_dependency(self, category: str, package: str, version: str, new_dep: str) -> None:
        """
        Add a new dependency to a package.

        Args:
            category: Package category (e.g., 'dev-python')
            package: Package name
            version: Version string or '_all'
            new_dep: New dependency atom to add

        Examples:
            >>> store = DependencyPatchStore()
            >>> store.add_dependency('dev-python', 'requests', '2.31.0', '>=dev-python/urllib3-2.0[${PYTHON_USEDEP}]')
            >>> patches = store.get_patches('dev-python', 'requests', '2.31.0')
            >>> len(patches)
            1
            >>> patches[0].operation
            'add'
        """
        pp = self._get_or_create_patches(category, package, version)
        patch = DependencyPatch('add', None, new_dep, time.time())
        pp.patches.append(patch)
        self._dirty = True
        logger.info(f"Added dependency {new_dep} to {category}/{package}/{version}")

    def remove_dependency(self, category: str, package: str, version: str, old_dep: str) -> None:
        """
        Remove a dependency from a package.

        Args:
            category: Package category (e.g., 'dev-python')
            package: Package name
            version: Version string or '_all'
            old_dep: Dependency atom to remove

        Examples:
            >>> store = DependencyPatchStore()
            >>> store.remove_dependency('dev-python', 'requests', '2.31.0', '=dev-python/urllib3-1.21[${PYTHON_USEDEP}]')
            >>> patches = store.get_patches('dev-python', 'requests', '2.31.0')
            >>> patches[0].operation
            'remove'
        """
        pp = self._get_or_create_patches(category, package, version)
        patch = DependencyPatch('remove', old_dep, None, time.time())
        pp.patches.append(patch)
        self._dirty = True
        logger.info(f"Removed dependency {old_dep} from {category}/{package}/{version}")

    def modify_dependency(self, category: str, package: str, version: str,
                         old_dep: str, new_dep: str) -> None:
        """
        Modify a dependency version constraint.

        Args:
            category: Package category (e.g., 'dev-python')
            package: Package name
            version: Version string or '_all'
            old_dep: Original dependency atom
            new_dep: New dependency atom

        Examples:
            >>> store = DependencyPatchStore()
            >>> store.modify_dependency('dev-python', 'requests', '2.31.0',
            ...     '=dev-python/urllib3-1.21[${PYTHON_USEDEP}]',
            ...     '>=dev-python/urllib3-1.21[${PYTHON_USEDEP}]')
            >>> patches = store.get_patches('dev-python', 'requests', '2.31.0')
            >>> patches[0].operation
            'modify'
        """
        pp = self._get_or_create_patches(category, package, version)
        patch = DependencyPatch('modify', old_dep, new_dep, time.time())
        pp.patches.append(patch)
        self._dirty = True
        logger.info(f"Modified dependency {old_dep} -> {new_dep} in {category}/{package}/{version}")

    def get_patches(self, category: str, package: str, version: str) -> List[DependencyPatch]:
        """
        Get all patches for a specific package version.

        Returns patches for both the specific version AND _all patches,
        with _all patches applied first, then version-specific patches.

        Args:
            category: Package category
            package: Package name
            version: Version string

        Returns:
            List of DependencyPatch objects in application order

        Examples:
            >>> store = DependencyPatchStore()
            >>> store.add_dependency('dev-python', 'requests', '_all', '>=dep-all')
            >>> store.add_dependency('dev-python', 'requests', '2.31.0', '>=dep-ver')
            >>> patches = store.get_patches('dev-python', 'requests', '2.31.0')
            >>> len(patches)
            2
            >>> patches[0].new_dep
            '>=dep-all'
            >>> patches[1].new_dep
            '>=dep-ver'
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
                     deps: List[str]) -> List[str]:
        """
        Apply patches to a dependency list.

        Args:
            category: Package category
            package: Package name
            version: Version string
            deps: Original list of dependency atoms

        Returns:
            Modified list of dependency atoms

        Examples:
            >>> store = DependencyPatchStore()
            >>> store.remove_dependency('dev-python', 'test', '1.0', '=dev-python/old-1.0[${PYTHON_USEDEP}]')
            >>> store.add_dependency('dev-python', 'test', '1.0', '>=dev-python/new-2.0[${PYTHON_USEDEP}]')
            >>> deps = ['=dev-python/old-1.0[${PYTHON_USEDEP}]', '>=dev-python/other-1.0[${PYTHON_USEDEP}]']
            >>> result = store.apply_patches('dev-python', 'test', '1.0', deps)
            >>> '=dev-python/old-1.0[${PYTHON_USEDEP}]' in result
            False
            >>> '>=dev-python/new-2.0[${PYTHON_USEDEP}]' in result
            True
        """
        patches = self.get_patches(category, package, version)
        if not patches:
            return deps

        # Work with a copy
        result = list(deps)

        for patch in patches:
            if patch.operation == 'add':
                # Add new dependency if not already present
                if patch.new_dep not in result:
                    result.append(patch.new_dep)

            elif patch.operation == 'remove':
                # Remove dependency (exact match or by base package)
                result = [d for d in result if not self._deps_match(d, patch.old_dep)]

            elif patch.operation == 'modify':
                # Replace old with new
                result = [
                    patch.new_dep if self._deps_match(d, patch.old_dep) else d
                    for d in result
                ]

        return result

    def _deps_match(self, dep1: str, dep2: str) -> bool:
        """
        Check if two dependency atoms match.

        Handles both exact matches and base package name matches.
        """
        if dep1 == dep2:
            return True

        # Extract base package name for comparison
        # e.g., ">=dev-python/urllib3-1.21[${PYTHON_USEDEP}]" -> "dev-python/urllib3"
        base1 = self._extract_package_name(dep1)
        base2 = self._extract_package_name(dep2)

        return base1 and base2 and base1 == base2

    def _extract_package_name(self, dep: str) -> Optional[str]:
        """
        Extract package name from a dependency atom.

        Examples:
            >>> store = DependencyPatchStore()
            >>> store._extract_package_name('>=dev-python/urllib3-1.21[${PYTHON_USEDEP}]')
            'dev-python/urllib3'
            >>> store._extract_package_name('dev-python/requests')
            'dev-python/requests'
        """
        # Remove operator prefix
        dep = dep.lstrip('>=<!=~')

        # Remove USE flags
        if '[' in dep:
            dep = dep[:dep.index('[')]

        # Split category/package-version
        if '/' in dep:
            parts = dep.split('/')
            if len(parts) == 2:
                category = parts[0]
                pv = parts[1]
                # Remove version (last hyphen followed by digit)
                match = re.match(r'^(.+)-\d', pv)
                if match:
                    return f"{category}/{match.group(1)}"
                return f"{category}/{pv}"

        return None

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
            >>> store = DependencyPatchStore()
            >>> store.modify_dependency('dev-python', 'requests', '2.31.0', '=old', '>=new')
            >>> content = store.generate_patch_file('dev-python', 'requests', '2.31.0')
            >>> '-> =old >=new' in content
            True
        """
        patches = self.get_patches(category, package, version)
        lines = [
            f"# Dependency patches for {category}/{package}/{version}",
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
            >>> store = DependencyPatchStore()
            >>> content = '''
            ... # Patches
            ... -> =old >=new
            ... -- =remove-this
            ... ++ >=add-this
            ... '''
            >>> count = store.parse_patch_file(content, 'dev-python', 'test', '1.0')
            >>> count
            3
        """
        pp = self._get_or_create_patches(category, package, version)
        count = 0
        timestamp = time.time()

        for line in content.splitlines():
            patch = DependencyPatch.from_patch_line(line, timestamp)
            if patch:
                pp.patches.append(patch)
                count += 1
                timestamp += 0.001  # Ensure unique timestamps

        if count > 0:
            self._dirty = True
            logger.info(f"Imported {count} patches for {category}/{package}/{version}")

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
            logger.info(f"Cleared {count} patches for {key}")
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

    def get_original_deps(self, category: str, package: str, version: str,
                         deps: List[str]) -> List[str]:
        """
        Get the original (unpatched) dependency list for display.

        This is used for the .sys/dependencies/ virtual filesystem where
        we show original deps with patches applied for file listing purposes.
        """
        # For now, we just return the current deps
        # The filesystem will fetch original deps from PyPI
        return deps
