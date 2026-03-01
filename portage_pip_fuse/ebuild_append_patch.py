"""
Ebuild phase append patching system for runtime modification of ebuild functions.

This module provides a virtual filesystem API for adding custom ebuild phase
functions (like src_configure, python_compile_pre) to packages that require
special build configuration.

Use Cases:
- Set environment variables before compilation (e.g., GEVENTSETUP_EMBED_CARES=0)
- Override default phase behavior for specific packages
- Add pre/post hooks for build phases

Patch File Format:
    [src_configure]
    export GEVENTSETUP_EMBED_CARES=0
    export GEVENTSETUP_EMBED_LIBEV=0
    distutils-r1_src_configure

    [python_compile_pre]
    # Custom cleanup or setup

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

# Valid phase name pattern (ebuild function names)
PHASE_NAME_PATTERN = re.compile(r'^[a-z_][a-z0-9_]*$')


def is_valid_phase_name(phase: str) -> bool:
    """
    Check if a phase name is valid for ebuild functions.

    Valid phase names must start with a lowercase letter or underscore,
    and contain only lowercase letters, digits, and underscores.

    Args:
        phase: The phase name to validate

    Returns:
        True if valid, False otherwise

    Examples:
        >>> is_valid_phase_name('src_configure')
        True
        >>> is_valid_phase_name('python_compile_pre')
        True
        >>> is_valid_phase_name('.src_configure.swp')
        False
        >>> is_valid_phase_name('4913')
        False
    """
    return bool(PHASE_NAME_PATTERN.match(phase))


@dataclass
class EbuildAppendPatch:
    """
    Represents a single ebuild phase append.

    Attributes:
        phase: Phase name (e.g., 'src_configure', 'python_compile_pre')
        content: Function body content (lines of shell code)
        timestamp: Unix timestamp when patch was created

    Examples:
        >>> patch = EbuildAppendPatch('src_configure', 'export FOO=bar\\ndistutils-r1_src_configure', 1700000000.0)
        >>> patch.phase
        'src_configure'
        >>> 'export FOO=bar' in patch.content
        True
    """
    phase: str
    content: str
    timestamp: float

    def __post_init__(self):
        """Validate the patch."""
        if not self.phase:
            raise ValueError("Phase name is required")
        if not is_valid_phase_name(self.phase):
            raise ValueError(f"Invalid phase name: {self.phase}")

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'EbuildAppendPatch':
        """Create from dictionary."""
        return cls(**data)


@dataclass
class PackageEbuildAppends:
    """
    Collection of ebuild phase appends for a specific package version.

    Attributes:
        category: Package category (e.g., 'dev-python')
        package: Package name (e.g., 'gevent')
        version: Version string or '_all' for all versions
        patches: List of phase appends

    Examples:
        >>> pp = PackageEbuildAppends('dev-python', 'gevent', '25.9.1', [])
        >>> pp.category
        'dev-python'
        >>> pp.is_all_versions
        False
        >>> pp_all = PackageEbuildAppends('dev-python', 'gevent', '_all', [])
        >>> pp_all.is_all_versions
        True
    """
    category: str
    package: str
    version: str  # Version string or '_all' for all versions
    patches: List[EbuildAppendPatch] = field(default_factory=list)

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
    def from_dict(cls, data: Dict[str, Any]) -> 'PackageEbuildAppends':
        """Create from dictionary."""
        patches = [EbuildAppendPatch.from_dict(p) for p in data.get('patches', [])]
        return cls(
            category=data['category'],
            package=data['package'],
            version=data['version'],
            patches=patches
        )


class EbuildAppendPatchStore:
    """
    Storage and application of ebuild phase appends.

    This class manages custom phase functions for packages, persisting them
    to JSON and applying them during ebuild generation.

    Attributes:
        storage_path: Path to the JSON file storing patches
        patches: Dictionary mapping package keys to PackageEbuildAppends

    Examples:
        >>> import tempfile
        >>> with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        ...     store = EbuildAppendPatchStore(f.name)
        >>> store.set_phase('dev-python', 'gevent', '_all', 'src_configure', 'export FOO=1')
        >>> phases = store.get_phases('dev-python', 'gevent', '_all')
        >>> 'src_configure' in phases
        True
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
        self.patches: Dict[str, PackageEbuildAppends] = {}
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
                # v3 format: mount_points -> {mount_point -> {ebuild_appends: [...]}}
                if self.mount_point and self.mount_point in data['mount_points']:
                    mp_data = data['mount_points'][self.mount_point]
                    for item in mp_data.get('ebuild_appends', []):
                        pp = PackageEbuildAppends.from_dict(item)
                        self.patches[pp.key] = pp
                # If mount_point not found, we'll have empty patches (new namespace)
            else:
                # v1/v2/v3 legacy format: ebuild_appends at top level
                for item in data.get('ebuild_appends', []):
                    pp = PackageEbuildAppends.from_dict(item)
                    self.patches[pp.key] = pp

            logger.info(f"Loaded {len(self.patches)} ebuild append patches from {self.storage_path}"
                       + (f" (mount: {self.mount_point})" if self.mount_point else ""))

        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.error(f"Failed to load ebuild append patches from {self.storage_path}: {e}")
            self.patches = {}

    def save(self) -> bool:
        """
        Save patches to JSON file atomically.

        This method preserves existing data in the file (other mount points,
        and other patch types) and only updates the ebuild_appends section
        for this mount point.

        When migrating from older formats to v3 format, existing patches are moved to
        the current mount point's namespace.

        Returns:
            True if save was successful, False otherwise

        Examples:
            >>> import tempfile
            >>> with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            ...     store = EbuildAppendPatchStore(f.name)
            >>> store.set_phase('dev-python', 'test', '1.0', 'src_configure', 'echo test')
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
                # Move legacy ebuild_appends to this mount point
                if 'ebuild_appends' in existing_data:
                    existing_data['mount_points'][mp_key]['ebuild_appends'] = existing_data.pop('ebuild_appends')
            else:
                existing_data['version'] = PATCH_FILE_VERSION
                if 'mount_points' not in existing_data:
                    existing_data['mount_points'] = {}

            # Update patches for this mount point
            mp_key = self.mount_point or '_default'
            if mp_key not in existing_data['mount_points']:
                existing_data['mount_points'][mp_key] = {}
            existing_data['mount_points'][mp_key]['ebuild_appends'] = [pp.to_dict() for pp in self.patches.values()]

            # Write to temporary file first
            temp_path = self.storage_path.with_suffix('.tmp')
            with temp_path.open('w', encoding='utf-8') as f:
                json.dump(existing_data, f, indent=2)

            # Atomic rename
            temp_path.rename(self.storage_path)
            self._dirty = False

            logger.debug(f"Saved {len(self.patches)} ebuild append patches to {self.storage_path}"
                        + (f" (mount: {self.mount_point})" if self.mount_point else ""))
            return True

        except OSError as e:
            logger.error(f"Failed to save ebuild append patches to {self.storage_path}: {e}")
            return False

    def _get_or_create_patches(self, category: str, package: str, version: str) -> PackageEbuildAppends:
        """Get or create PackageEbuildAppends for a package/version."""
        key = f"{category}/{package}/{version}"
        if key not in self.patches:
            self.patches[key] = PackageEbuildAppends(category, package, version, [])
        return self.patches[key]

    def set_phase(self, category: str, package: str, version: str,
                  phase: str, content: str) -> None:
        """
        Set or replace phase content for a package.

        Args:
            category: Package category (e.g., 'dev-python')
            package: Package name
            version: Version string or '_all'
            phase: Phase name (e.g., 'src_configure')
            content: Function body content

        Examples:
            >>> store = EbuildAppendPatchStore()
            >>> store.set_phase('dev-python', 'gevent', '_all', 'src_configure', 'export FOO=1')
            >>> phases = store.get_phases('dev-python', 'gevent', '_all')
            >>> phases['src_configure']
            'export FOO=1'
        """
        pp = self._get_or_create_patches(category, package, version)

        # Remove existing patch for this phase if any
        pp.patches = [p for p in pp.patches if p.phase != phase]

        # Add new patch
        patch = EbuildAppendPatch(phase, content.strip(), time.time())
        pp.patches.append(patch)
        self._dirty = True
        logger.info(f"Set {phase} for {category}/{package}/{version}")

    def remove_phase(self, category: str, package: str, version: str, phase: str) -> bool:
        """
        Remove a phase from a package.

        Args:
            category: Package category
            package: Package name
            version: Version string or '_all'
            phase: Phase name to remove

        Returns:
            True if phase was removed, False if not found

        Examples:
            >>> store = EbuildAppendPatchStore()
            >>> store.set_phase('dev-python', 'test', '1.0', 'src_configure', 'echo test')
            >>> store.remove_phase('dev-python', 'test', '1.0', 'src_configure')
            True
            >>> store.remove_phase('dev-python', 'test', '1.0', 'nonexistent')
            False
        """
        key = f"{category}/{package}/{version}"
        if key not in self.patches:
            return False

        pp = self.patches[key]
        original_count = len(pp.patches)
        pp.patches = [p for p in pp.patches if p.phase != phase]

        if len(pp.patches) < original_count:
            self._dirty = True
            # Remove empty entries
            if not pp.patches:
                del self.patches[key]
            logger.info(f"Removed {phase} from {category}/{package}/{version}")
            return True
        return False

    def get_phases(self, category: str, package: str, version: str) -> Dict[str, str]:
        """
        Get all phases for a specific package version.

        Returns phases for both the specific version AND _all patches,
        with _all patches applied first, then version-specific patches override.

        Args:
            category: Package category
            package: Package name
            version: Version string

        Returns:
            Dictionary mapping phase names to content

        Examples:
            >>> store = EbuildAppendPatchStore()
            >>> store.set_phase('dev-python', 'test', '_all', 'src_configure', 'global')
            >>> store.set_phase('dev-python', 'test', '1.0', 'src_configure', 'specific')
            >>> phases = store.get_phases('dev-python', 'test', '1.0')
            >>> phases['src_configure']
            'specific'
        """
        result: Dict[str, str] = {}

        # First apply _all patches
        all_key = f"{category}/{package}/_all"
        if all_key in self.patches:
            for patch in sorted(self.patches[all_key].patches, key=lambda p: p.timestamp):
                result[patch.phase] = patch.content

        # Then apply version-specific patches (overrides _all)
        if version != '_all':
            ver_key = f"{category}/{package}/{version}"
            if ver_key in self.patches:
                for patch in sorted(self.patches[ver_key].patches, key=lambda p: p.timestamp):
                    result[patch.phase] = patch.content

        return result

    def get_phase(self, category: str, package: str, version: str, phase: str) -> Optional[str]:
        """
        Get content for a specific phase.

        Args:
            category: Package category
            package: Package name
            version: Version string
            phase: Phase name

        Returns:
            Phase content or None if not found
        """
        phases = self.get_phases(category, package, version)
        return phases.get(phase)

    def has_phases(self, category: str, package: str, version: str) -> bool:
        """Check if any phases exist for a package version."""
        return len(self.get_phases(category, package, version)) > 0

    def get_package_versions_with_phases(self, category: str, package: str) -> List[str]:
        """
        Get all versions that have phases for a package.

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

    def list_phases_for_version(self, category: str, package: str, version: str) -> List[str]:
        """
        List phase names defined for a specific version only (not including _all).

        Args:
            category: Package category
            package: Package name
            version: Version string

        Returns:
            List of phase names
        """
        key = f"{category}/{package}/{version}"
        if key not in self.patches:
            return []
        return sorted(set(p.phase for p in self.patches[key].patches))

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
            >>> store = EbuildAppendPatchStore()
            >>> store.set_phase('dev-python', 'gevent', '_all', 'src_configure', 'export FOO=1')
            >>> content = store.generate_patch_file('dev-python', 'gevent', '_all')
            >>> '[src_configure]' in content
            True
            >>> 'export FOO=1' in content
            True
        """
        phases = self.get_phases(category, package, version)
        lines = [
            f"# Ebuild appends for {category}/{package}/{version}",
            f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            ""
        ]

        for phase_name in sorted(phases.keys()):
            content = phases[phase_name]
            lines.append(f"[{phase_name}]")
            lines.extend(content.split('\n'))
            lines.append("")

        return '\n'.join(lines)

    def parse_patch_file(self, content: str, category: str, package: str, version: str) -> int:
        """
        Parse and import patches from patch file content.

        Args:
            content: Patch file content
            category: Target package category
            package: Target package name
            version: Target version

        Returns:
            Number of phases imported

        Examples:
            >>> store = EbuildAppendPatchStore()
            >>> content = '''
            ... # Patches
            ... [src_configure]
            ... export FOO=1
            ... distutils-r1_src_configure
            ...
            ... [python_compile_pre]
            ... echo cleanup
            ... '''
            >>> count = store.parse_patch_file(content, 'dev-python', 'test', '1.0')
            >>> count
            2
        """
        count = 0
        current_phase: Optional[str] = None
        current_content: List[str] = []

        for line in content.splitlines():
            # Skip comments at the start
            stripped = line.strip()
            if stripped.startswith('#') and current_phase is None:
                continue

            # Check for phase header
            match = re.match(r'^\[([a-z_][a-z0-9_]*)\]$', stripped)
            if match:
                # Save previous phase if any
                if current_phase is not None:
                    phase_content = '\n'.join(current_content).strip()
                    if phase_content:
                        self.set_phase(category, package, version, current_phase, phase_content)
                        count += 1

                current_phase = match.group(1)
                current_content = []
            elif current_phase is not None:
                current_content.append(line)

        # Save last phase
        if current_phase is not None:
            phase_content = '\n'.join(current_content).strip()
            if phase_content:
                self.set_phase(category, package, version, current_phase, phase_content)
                count += 1

        if count > 0:
            logger.info(f"Imported {count} ebuild phases for {category}/{package}/{version}")

        return count

    def clear_phases(self, category: str, package: str, version: str) -> int:
        """
        Clear all phases for a specific package version.

        Returns:
            Number of phases cleared
        """
        key = f"{category}/{package}/{version}"
        if key in self.patches:
            count = len(self.patches[key].patches)
            del self.patches[key]
            self._dirty = True
            logger.info(f"Cleared {count} ebuild phases for {key}")
            return count
        return 0

    def list_patched_packages(self) -> List[Tuple[str, str, str]]:
        """
        List all packages that have phase appends.

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

    def apply_phases(self, category: str, package: str, version: str) -> Dict[str, str]:
        """
        Get phases to apply to an ebuild (alias for get_phases).

        This is the primary method called during ebuild generation.

        Args:
            category: Package category
            package: Package name
            version: Version string

        Returns:
            Dictionary mapping phase names to function body content
        """
        return self.get_phases(category, package, version)
