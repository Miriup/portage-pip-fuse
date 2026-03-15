"""
Slot patching system for manual SLOT override configuration.

This module provides a virtual filesystem API for overriding SLOT values
for packages when the default SLOT="0" differs from what Gentoo uses.

The patches are stored in the .sys/slot/ directory:
- .sys/slot/{category}/{package}/_all  - Override for all versions
- .sys/slot/{category}/{package}/{version}  - Override for specific version

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .constants import get_mount_point_key

logger = logging.getLogger(__name__)

# Current patch file format version
PATCH_FILE_VERSION = 3

# Valid SLOT pattern per PMS
SLOT_PATTERN = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9+_.-]*(/[A-Za-z0-9_][A-Za-z0-9+_.-]*)?$')


def is_valid_slot(slot: str) -> bool:
    """
    Check if a SLOT value is valid per PMS.

    Args:
        slot: The SLOT value to validate

    Returns:
        True if valid, False otherwise

    Examples:
        >>> is_valid_slot('0')
        True
        >>> is_valid_slot('2.0')
        True
        >>> is_valid_slot('7.0/7.0')
        True
        >>> is_valid_slot('')
        False
        >>> is_valid_slot('0/')
        False
        >>> is_valid_slot('/0')
        False
    """
    if not slot:
        return False
    return bool(SLOT_PATTERN.match(slot))


class SlotPatchStore:
    """
    Storage and management of SLOT overrides.

    This class manages patches that override the SLOT value for packages,
    persisting them to JSON and applying them during ebuild generation.

    Attributes:
        storage_path: Path to the JSON file storing patches
        overrides: Dictionary mapping "category/package/version" to SLOT value

    Examples:
        >>> import tempfile
        >>> with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        ...     store = SlotPatchStore(f.name)
        >>> store.set('dev-ruby', 'database_cleaner-core', '_all', '2.0')
        >>> store.get('dev-ruby', 'database_cleaner-core', '2.0.1')
        '2.0'
        >>> import os; os.unlink(f.name)
    """

    def __init__(self, storage_path: Optional[str] = None, mount_point: Optional[str] = None):
        """
        Initialize the slot patch store.

        Args:
            storage_path: Path to JSON file for persistence (None for memory-only)
            mount_point: Mount point path for namespaced configuration
        """
        self.storage_path = Path(storage_path) if storage_path else None
        self.mount_point = get_mount_point_key(mount_point) if mount_point else None
        self.overrides: Dict[str, str] = {}  # "cat/pkg/ver" -> slot
        self._dirty = False

        if self.storage_path and self.storage_path.exists():
            self._load()

    def _load(self) -> None:
        """Load slot overrides from JSON file."""
        if not self.storage_path or not self.storage_path.exists():
            return

        try:
            with self.storage_path.open('r', encoding='utf-8') as f:
                data = json.load(f)

            self.overrides = {}

            if 'mount_points' in data:
                # v3 format: mount_points -> {mount_point -> {slot_overrides: {...}}}
                mp_key = self.mount_point or '_default'
                if mp_key in data['mount_points']:
                    mp_data = data['mount_points'][mp_key]
                    self.overrides = mp_data.get('slot_overrides', {})

            logger.info(f"Loaded {len(self.overrides)} slot overrides from {self.storage_path}"
                       + (f" (mount: {self.mount_point})" if self.mount_point else ""))

        except (json.JSONDecodeError, KeyError, OSError) as e:
            logger.error(f"Failed to load slot overrides from {self.storage_path}: {e}")
            self.overrides = {}

    def save(self) -> bool:
        """
        Save slot overrides to JSON file atomically.

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

            existing_data['version'] = max(existing_data.get('version', 1), PATCH_FILE_VERSION)
            if 'mount_points' not in existing_data:
                existing_data['mount_points'] = {}

            # Update slot overrides for this mount point
            mp_key = self.mount_point or '_default'
            if mp_key not in existing_data['mount_points']:
                existing_data['mount_points'][mp_key] = {}
            existing_data['mount_points'][mp_key]['slot_overrides'] = self.overrides

            # Write to temporary file first
            temp_path = self.storage_path.with_suffix('.tmp')
            with temp_path.open('w', encoding='utf-8') as f:
                json.dump(existing_data, f, indent=2)

            # Atomic rename
            temp_path.rename(self.storage_path)
            self._dirty = False

            logger.debug(f"Saved {len(self.overrides)} slot overrides to {self.storage_path}")
            return True

        except OSError as e:
            logger.error(f"Failed to save slot overrides to {self.storage_path}: {e}")
            return False

    def get(self, category: str, package: str, version: str) -> Optional[str]:
        """
        Get the SLOT override for a package version.

        Checks version-specific overrides first, then _all overrides.

        Args:
            category: Package category (e.g., 'dev-ruby')
            package: Package name
            version: Version string

        Returns:
            SLOT override if set, None otherwise

        Examples:
            >>> store = SlotPatchStore()
            >>> store.set('dev-ruby', 'rails', '_all', '7.0')
            >>> store.get('dev-ruby', 'rails', '7.0.8')
            '7.0'
            >>> store.get('dev-ruby', 'other', '1.0') is None
            True
        """
        # First check version-specific override
        ver_key = f"{category}/{package}/{version}"
        if ver_key in self.overrides:
            return self.overrides[ver_key]

        # Then check _all override
        all_key = f"{category}/{package}/_all"
        if all_key in self.overrides:
            return self.overrides[all_key]

        return None

    def set(self, category: str, package: str, version: str, slot: str) -> None:
        """
        Set the SLOT override for a package version.

        Args:
            category: Package category (e.g., 'dev-ruby')
            package: Package name
            version: Version string or '_all'
            slot: SLOT value to set

        Raises:
            ValueError: If slot value is invalid

        Examples:
            >>> store = SlotPatchStore()
            >>> store.set('dev-ruby', 'database_cleaner-core', '_all', '2.0')
            >>> store.get('dev-ruby', 'database_cleaner-core', '2.0.1')
            '2.0'
        """
        if not is_valid_slot(slot):
            raise ValueError(f"Invalid SLOT value: {slot}")

        key = f"{category}/{package}/{version}"
        self.overrides[key] = slot
        self._dirty = True
        logger.info(f"Set SLOT override for {key}: {slot}")

    def remove(self, category: str, package: str, version: str) -> bool:
        """
        Remove the SLOT override for a package version.

        Args:
            category: Package category
            package: Package name
            version: Version string or '_all'

        Returns:
            True if removed, False if not found
        """
        key = f"{category}/{package}/{version}"
        if key in self.overrides:
            del self.overrides[key]
            self._dirty = True
            logger.info(f"Removed SLOT override for {key}")
            return True
        return False

    def has_override(self, category: str, package: str, version: str) -> bool:
        """Check if a SLOT override exists for a package version."""
        return self.get(category, package, version) is not None

    def list_categories(self) -> Set[str]:
        """
        Get all categories that have SLOT overrides.

        Returns:
            Set of category names
        """
        categories = set()
        for key in self.overrides:
            parts = key.split('/')
            if len(parts) >= 1:
                categories.add(parts[0])
        return categories

    def list_packages(self, category: str) -> Set[str]:
        """
        Get all packages in a category that have SLOT overrides.

        Args:
            category: Package category

        Returns:
            Set of package names
        """
        packages = set()
        prefix = f"{category}/"
        for key in self.overrides:
            if key.startswith(prefix):
                parts = key[len(prefix):].split('/')
                if parts:
                    packages.add(parts[0])
        return packages

    def list_versions(self, category: str, package: str) -> Set[str]:
        """
        Get all versions that have SLOT overrides for a package.

        Args:
            category: Package category
            package: Package name

        Returns:
            Set of version strings (including '_all' if present)
        """
        versions = set()
        prefix = f"{category}/{package}/"
        for key in self.overrides:
            if key.startswith(prefix):
                version = key[len(prefix):]
                if version:
                    versions.add(version)
        return versions

    def generate_patch_content(self, category: str, package: str, version: str) -> str:
        """
        Generate patch file content for a SLOT override.

        Args:
            category: Package category
            package: Package name
            version: Version string

        Returns:
            Slot value as file content (with trailing newline)
        """
        slot = self.get(category, package, version)
        if slot:
            return slot + '\n'
        return ''

    def parse_patch_content(self, content: str) -> Optional[str]:
        """
        Parse SLOT value from patch file content.

        Args:
            content: File content (SLOT value)

        Returns:
            Validated SLOT value, or None if invalid
        """
        slot = content.strip()
        if is_valid_slot(slot):
            return slot
        return None

    def list_all_overrides(self) -> List[Tuple[str, str, str, str]]:
        """
        List all SLOT overrides.

        Returns:
            List of (category, package, version, slot) tuples
        """
        result = []
        for key, slot in sorted(self.overrides.items()):
            parts = key.split('/')
            if len(parts) == 3:
                result.append((parts[0], parts[1], parts[2], slot))
        return result

    @property
    def is_dirty(self) -> bool:
        """Check if there are unsaved changes."""
        return self._dirty
