"""
Tests for the dependency patching system.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import json
import os
import tempfile
import time

import pytest

from portage_pip_fuse.dependency_patch import (
    DependencyPatch,
    PackagePatches,
    DependencyPatchStore
)


class TestDependencyPatch:
    """Test the DependencyPatch dataclass."""

    def test_create_add_patch(self):
        """Test creating an add patch."""
        patch = DependencyPatch('add', None, '>=dev-python/new-1.0', time.time())
        assert patch.operation == 'add'
        assert patch.old_dep is None
        assert patch.new_dep == '>=dev-python/new-1.0'

    def test_create_remove_patch(self):
        """Test creating a remove patch."""
        patch = DependencyPatch('remove', '=dev-python/old-1.0', None, time.time())
        assert patch.operation == 'remove'
        assert patch.old_dep == '=dev-python/old-1.0'
        assert patch.new_dep is None

    def test_create_modify_patch(self):
        """Test creating a modify patch."""
        patch = DependencyPatch('modify', '=dev-python/pkg-1.0', '>=dev-python/pkg-1.0', time.time())
        assert patch.operation == 'modify'
        assert patch.old_dep == '=dev-python/pkg-1.0'
        assert patch.new_dep == '>=dev-python/pkg-1.0'

    def test_invalid_operation(self):
        """Test that invalid operations raise ValueError."""
        with pytest.raises(ValueError):
            DependencyPatch('invalid', None, None, time.time())

    def test_add_requires_new_dep(self):
        """Test that add operation requires new_dep."""
        with pytest.raises(ValueError):
            DependencyPatch('add', None, None, time.time())

    def test_remove_requires_old_dep(self):
        """Test that remove operation requires old_dep."""
        with pytest.raises(ValueError):
            DependencyPatch('remove', None, None, time.time())

    def test_modify_requires_both(self):
        """Test that modify operation requires both deps."""
        with pytest.raises(ValueError):
            DependencyPatch('modify', '=old', None, time.time())
        with pytest.raises(ValueError):
            DependencyPatch('modify', None, '>=new', time.time())

    def test_to_dict(self):
        """Test conversion to dictionary."""
        patch = DependencyPatch('modify', '=old', '>=new', 1700000000.0)
        d = patch.to_dict()
        assert d['operation'] == 'modify'
        assert d['old_dep'] == '=old'
        assert d['new_dep'] == '>=new'
        assert d['timestamp'] == 1700000000.0

    def test_from_dict(self):
        """Test creation from dictionary."""
        d = {
            'operation': 'add',
            'old_dep': None,
            'new_dep': '>=dev-python/new-1.0',
            'timestamp': 1700000000.0
        }
        patch = DependencyPatch.from_dict(d)
        assert patch.operation == 'add'
        assert patch.new_dep == '>=dev-python/new-1.0'

    def test_to_patch_line_modify(self):
        """Test patch line generation for modify."""
        patch = DependencyPatch('modify', '=old', '>=new', 0)
        assert patch.to_patch_line() == '-> =old >=new'

    def test_to_patch_line_remove(self):
        """Test patch line generation for remove."""
        patch = DependencyPatch('remove', '=old', None, 0)
        assert patch.to_patch_line() == '-- =old'

    def test_to_patch_line_add(self):
        """Test patch line generation for add."""
        patch = DependencyPatch('add', None, '>=new', 0)
        assert patch.to_patch_line() == '++ >=new'

    def test_from_patch_line_modify(self):
        """Test parsing modify patch line."""
        patch = DependencyPatch.from_patch_line('-> =old >=new')
        assert patch is not None
        assert patch.operation == 'modify'
        assert patch.old_dep == '=old'
        assert patch.new_dep == '>=new'

    def test_from_patch_line_remove(self):
        """Test parsing remove patch line."""
        patch = DependencyPatch.from_patch_line('-- =old')
        assert patch is not None
        assert patch.operation == 'remove'
        assert patch.old_dep == '=old'

    def test_from_patch_line_add(self):
        """Test parsing add patch line."""
        patch = DependencyPatch.from_patch_line('++ >=new')
        assert patch is not None
        assert patch.operation == 'add'
        assert patch.new_dep == '>=new'

    def test_from_patch_line_comment(self):
        """Test that comments are skipped."""
        assert DependencyPatch.from_patch_line('# comment') is None

    def test_from_patch_line_empty(self):
        """Test that empty lines are skipped."""
        assert DependencyPatch.from_patch_line('') is None
        assert DependencyPatch.from_patch_line('   ') is None


class TestPackagePatches:
    """Test the PackagePatches dataclass."""

    def test_create(self):
        """Test creating PackagePatches."""
        pp = PackagePatches('dev-python', 'requests', '2.31.0', [])
        assert pp.category == 'dev-python'
        assert pp.package == 'requests'
        assert pp.version == '2.31.0'
        assert len(pp.patches) == 0

    def test_is_all_versions(self):
        """Test _all version detection."""
        pp = PackagePatches('dev-python', 'requests', '2.31.0', [])
        assert not pp.is_all_versions

        pp_all = PackagePatches('dev-python', 'requests', '_all', [])
        assert pp_all.is_all_versions

    def test_key(self):
        """Test key generation."""
        pp = PackagePatches('dev-python', 'requests', '2.31.0', [])
        assert pp.key == 'dev-python/requests/2.31.0'

    def test_to_dict(self):
        """Test conversion to dictionary."""
        patch = DependencyPatch('add', None, '>=new', 0)
        pp = PackagePatches('dev-python', 'requests', '2.31.0', [patch])
        d = pp.to_dict()
        assert d['category'] == 'dev-python'
        assert d['package'] == 'requests'
        assert d['version'] == '2.31.0'
        assert len(d['patches']) == 1

    def test_from_dict(self):
        """Test creation from dictionary."""
        d = {
            'category': 'dev-python',
            'package': 'requests',
            'version': '2.31.0',
            'patches': [{
                'operation': 'add',
                'old_dep': None,
                'new_dep': '>=new',
                'timestamp': 0
            }]
        }
        pp = PackagePatches.from_dict(d)
        assert pp.category == 'dev-python'
        assert pp.package == 'requests'
        assert len(pp.patches) == 1


class TestDependencyPatchStore:
    """Test the DependencyPatchStore class."""

    def test_memory_only_store(self):
        """Test store without persistence."""
        store = DependencyPatchStore()
        store.add_dependency('dev-python', 'test', '1.0', '>=dep-1.0')
        patches = store.get_patches('dev-python', 'test', '1.0')
        assert len(patches) == 1
        assert patches[0].operation == 'add'

    def test_add_dependency(self):
        """Test adding a dependency."""
        store = DependencyPatchStore()
        store.add_dependency('dev-python', 'requests', '2.31.0', '>=dev-python/urllib3-2.0[${PYTHON_USEDEP}]')

        patches = store.get_patches('dev-python', 'requests', '2.31.0')
        assert len(patches) == 1
        assert patches[0].operation == 'add'
        assert patches[0].new_dep == '>=dev-python/urllib3-2.0[${PYTHON_USEDEP}]'

    def test_remove_dependency(self):
        """Test removing a dependency."""
        store = DependencyPatchStore()
        store.remove_dependency('dev-python', 'requests', '2.31.0', '=dev-python/urllib3-1.21[${PYTHON_USEDEP}]')

        patches = store.get_patches('dev-python', 'requests', '2.31.0')
        assert len(patches) == 1
        assert patches[0].operation == 'remove'

    def test_modify_dependency(self):
        """Test modifying a dependency."""
        store = DependencyPatchStore()
        store.modify_dependency('dev-python', 'requests', '2.31.0',
                               '=dev-python/urllib3-1.21[${PYTHON_USEDEP}]',
                               '>=dev-python/urllib3-1.21[${PYTHON_USEDEP}]')

        patches = store.get_patches('dev-python', 'requests', '2.31.0')
        assert len(patches) == 1
        assert patches[0].operation == 'modify'
        assert patches[0].old_dep == '=dev-python/urllib3-1.21[${PYTHON_USEDEP}]'
        assert patches[0].new_dep == '>=dev-python/urllib3-1.21[${PYTHON_USEDEP}]'

    def test_persistence(self):
        """Test saving and loading patches."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name

        try:
            # Create store and add patches
            store1 = DependencyPatchStore(path)
            store1.add_dependency('dev-python', 'requests', '2.31.0', '>=dep-1.0')
            store1.modify_dependency('dev-python', 'requests', '2.31.0', '=old', '>=new')
            store1.save()

            # Create new store and load
            store2 = DependencyPatchStore(path)
            patches = store2.get_patches('dev-python', 'requests', '2.31.0')
            assert len(patches) == 2

        finally:
            os.unlink(path)

    def test_all_version_patches(self):
        """Test that _all patches apply to all versions."""
        store = DependencyPatchStore()

        # Add _all patch
        store.add_dependency('dev-python', 'requests', '_all', '>=global-dep')

        # Add version-specific patch
        store.add_dependency('dev-python', 'requests', '2.31.0', '>=version-dep')

        # Get patches for a specific version - should include both
        patches = store.get_patches('dev-python', 'requests', '2.31.0')
        assert len(patches) == 2

        # _all patches should come first
        assert patches[0].new_dep == '>=global-dep'
        assert patches[1].new_dep == '>=version-dep'

    def test_apply_patches_add(self):
        """Test applying add patches."""
        store = DependencyPatchStore()
        store.add_dependency('dev-python', 'test', '1.0', '>=new-dep')

        deps = ['>=existing-dep']
        result = store.apply_patches('dev-python', 'test', '1.0', deps)

        assert '>=existing-dep' in result
        assert '>=new-dep' in result

    def test_apply_patches_remove(self):
        """Test applying remove patches."""
        store = DependencyPatchStore()
        store.remove_dependency('dev-python', 'test', '1.0', '>=remove-me')

        deps = ['>=remove-me', '>=keep-me']
        result = store.apply_patches('dev-python', 'test', '1.0', deps)

        assert '>=remove-me' not in result
        assert '>=keep-me' in result

    def test_apply_patches_modify(self):
        """Test applying modify patches."""
        store = DependencyPatchStore()
        store.modify_dependency('dev-python', 'test', '1.0', '=old-1.0', '>=old-1.0')

        deps = ['=old-1.0', '>=other']
        result = store.apply_patches('dev-python', 'test', '1.0', deps)

        assert '=old-1.0' not in result
        assert '>=old-1.0' in result
        assert '>=other' in result

    def test_apply_patches_order(self):
        """Test that patches are applied in timestamp order."""
        store = DependencyPatchStore()

        # Add patches with specific timestamps
        store.add_dependency('dev-python', 'test', '1.0', '>=first')
        time.sleep(0.01)  # Ensure different timestamps
        store.add_dependency('dev-python', 'test', '1.0', '>=second')

        deps = []
        result = store.apply_patches('dev-python', 'test', '1.0', deps)

        # Both should be added
        assert '>=first' in result
        assert '>=second' in result

    def test_generate_patch_file(self):
        """Test generating patch file content."""
        store = DependencyPatchStore()
        store.modify_dependency('dev-python', 'requests', '2.31.0', '=old', '>=new')
        store.remove_dependency('dev-python', 'requests', '2.31.0', '=remove')
        store.add_dependency('dev-python', 'requests', '2.31.0', '>=add')

        content = store.generate_patch_file('dev-python', 'requests', '2.31.0')

        assert '-> =old >=new' in content
        assert '-- =remove' in content
        assert '++ >=add' in content

    def test_parse_patch_file(self):
        """Test parsing patch file content."""
        store = DependencyPatchStore()

        content = """
# Dependency patches
-> =old >=new
-- =remove
++ >=add
"""
        count = store.parse_patch_file(content, 'dev-python', 'test', '1.0')
        assert count == 3

        patches = store.get_patches('dev-python', 'test', '1.0')
        assert len(patches) == 3

    def test_clear_patches(self):
        """Test clearing patches."""
        store = DependencyPatchStore()
        store.add_dependency('dev-python', 'test', '1.0', '>=dep')
        store.add_dependency('dev-python', 'test', '1.0', '>=dep2')

        count = store.clear_patches('dev-python', 'test', '1.0')
        assert count == 2

        patches = store.get_patches('dev-python', 'test', '1.0')
        assert len(patches) == 0

    def test_list_patched_packages(self):
        """Test listing packages with patches."""
        store = DependencyPatchStore()
        store.add_dependency('dev-python', 'requests', '2.31.0', '>=dep1')
        store.add_dependency('dev-python', 'urllib3', '2.0.0', '>=dep2')
        store.add_dependency('dev-python', 'requests', '_all', '>=dep3')

        packages = store.list_patched_packages()
        assert ('dev-python', 'requests', '2.31.0') in packages
        assert ('dev-python', 'urllib3', '2.0.0') in packages
        assert ('dev-python', 'requests', '_all') in packages

    def test_get_package_versions_with_patches(self):
        """Test getting versions with patches for a package."""
        store = DependencyPatchStore()
        store.add_dependency('dev-python', 'requests', '2.31.0', '>=dep1')
        store.add_dependency('dev-python', 'requests', '2.30.0', '>=dep2')
        store.add_dependency('dev-python', 'requests', '_all', '>=dep3')

        versions = store.get_package_versions_with_patches('dev-python', 'requests')
        assert '2.31.0' in versions
        assert '2.30.0' in versions
        assert '_all' in versions

    def test_has_patches(self):
        """Test checking if patches exist."""
        store = DependencyPatchStore()
        assert not store.has_patches('dev-python', 'test', '1.0')

        store.add_dependency('dev-python', 'test', '1.0', '>=dep')
        assert store.has_patches('dev-python', 'test', '1.0')

    def test_is_dirty(self):
        """Test dirty flag tracking."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = f.name

        try:
            store = DependencyPatchStore(path)
            assert not store.is_dirty

            store.add_dependency('dev-python', 'test', '1.0', '>=dep')
            assert store.is_dirty

            store.save()
            assert not store.is_dirty

        finally:
            os.unlink(path)

    def test_extract_package_name(self):
        """Test extracting package name from dependency atom."""
        store = DependencyPatchStore()

        assert store._extract_package_name('>=dev-python/urllib3-1.21[${PYTHON_USEDEP}]') == 'dev-python/urllib3'
        assert store._extract_package_name('dev-python/requests') == 'dev-python/requests'
        assert store._extract_package_name('=dev-python/pkg-1.2.3') == 'dev-python/pkg'
        assert store._extract_package_name('!=dev-python/pkg-2.0_alpha1') == 'dev-python/pkg'

    def test_deps_match(self):
        """Test dependency matching."""
        store = DependencyPatchStore()

        # Exact match
        assert store._deps_match('>=dev-python/pkg-1.0', '>=dev-python/pkg-1.0')

        # Same package, different version
        assert store._deps_match('>=dev-python/pkg-1.0', '>=dev-python/pkg-2.0')

        # Different packages
        assert not store._deps_match('>=dev-python/pkg1-1.0', '>=dev-python/pkg2-1.0')


class TestDependencyPatchStoreEdgeCases:
    """Test edge cases and error handling."""

    def test_load_corrupted_json(self):
        """Test handling corrupted JSON file."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            f.write('not valid json')
            path = f.name

        try:
            store = DependencyPatchStore(path)
            # Should not raise, just have empty patches
            assert len(store.patches) == 0
        finally:
            os.unlink(path)

    def test_load_nonexistent_file(self):
        """Test loading from non-existent file."""
        store = DependencyPatchStore('/nonexistent/path/patches.json')
        # Should not raise, just have empty patches
        assert len(store.patches) == 0

    def test_save_to_unwritable_location(self):
        """Test saving to unwritable location."""
        store = DependencyPatchStore('/nonexistent/path/patches.json')
        store.add_dependency('dev-python', 'test', '1.0', '>=dep')
        # Should return False, not raise
        assert not store.save()

    def test_apply_patches_no_patches(self):
        """Test applying patches when none exist."""
        store = DependencyPatchStore()
        deps = ['>=dep1', '>=dep2']
        result = store.apply_patches('dev-python', 'test', '1.0', deps)
        assert result == deps

    def test_apply_patches_preserves_order(self):
        """Test that apply_patches preserves dependency order."""
        store = DependencyPatchStore()
        store.add_dependency('dev-python', 'test', '1.0', '>=new')

        deps = ['>=a', '>=b', '>=c']
        result = store.apply_patches('dev-python', 'test', '1.0', deps)

        # Original deps should be in original order
        assert result.index('>=a') < result.index('>=b')
        assert result.index('>=b') < result.index('>=c')

    def test_parse_patch_file_with_spaces(self):
        """Test parsing patch file with extra whitespace."""
        store = DependencyPatchStore()

        content = """
  # Comment with leading spaces

   -> =old >=new

-- =remove

"""
        count = store.parse_patch_file(content, 'dev-python', 'test', '1.0')
        assert count == 2
