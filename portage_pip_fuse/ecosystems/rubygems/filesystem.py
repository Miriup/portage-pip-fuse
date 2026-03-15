"""
FUSE filesystem implementation for RubyGems adapter.

This module provides a FUSE filesystem that dynamically generates
Gentoo overlay content from RubyGems packages. It integrates:
- Name translation between RubyGems and Gentoo formats
- Version translation from RubyGems to Gentoo
- Dynamic ebuild generation with dependencies
- Manifest file generation with checksums

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import errno
import hashlib
import logging
import os
import re
import stat
import time
from typing import Optional, Dict, List, Set, Tuple, Any

from fuse import FUSE, FuseOSError, Operations

from portage_pip_fuse.constants import DEFAULT_PATCH_FILE
from portage_pip_fuse.slot_patch import SlotPatchStore, is_valid_slot
from portage_pip_fuse.dependency_patch import DependencyPatchStore
from portage_pip_fuse.iuse_patch import IUSEPatchStore, is_valid_use_flag
from portage_pip_fuse.ebuild_append_patch import EbuildAppendPatchStore, is_valid_phase_name
from portage_pip_fuse.git_source_patch import GitSourcePatchStore, is_valid_source_mode
from portage_pip_fuse.name_translation_patch import NameTranslationPatchStore, is_valid_gentoo_atom
from .ruby_compat_patch import RubyCompatPatchStore
from .plugin import RubyGemsPlugin, RubyGemsMetadataProvider, RubyGemsEbuildGenerator
from .name_translator import create_rubygems_translator
from .filters import (
    RubyCompatFilter,
    GemSourceFilter,
    PlatformFilter,
    PreReleaseFilter,
    GentooVersionFilter,
    VersionFilterChain,
)

logger = logging.getLogger(__name__)

# Default RubyGems overlay configuration
RUBYGEMS_REPO_NAME = "portage-gem-fuse"
RUBYGEMS_REPO_LOCATION = "/var/db/repos/rubygems"


class PortageGemFS(Operations):
    """
    FUSE filesystem that provides a virtual interface between gem and portage.

    This filesystem presents RubyGems packages as if they were portage ebuilds,
    allowing transparent access to Ruby packages through Gentoo's package
    management system.

    Features:
    - Dynamic ebuild generation from RubyGems metadata
    - Bidirectional name translation (RubyGems <-> Gentoo)
    - Version translation from RubyGems to Gentoo format
    - Manifest file generation with checksums from RubyGems
    - Thin overlay layout with on-demand content generation
    """

    def __init__(
        self,
        root: str = "/",
        cache_ttl: int = 3600,
        cache_dir: Optional[str] = None,
        filter_config: Optional[Dict] = None,
        mount_point: Optional[str] = None,
        use_ruby: Optional[List[str]] = None,
        patch_file: Optional[str] = None,
        no_patches: bool = False
    ):
        """
        Initialize the RubyGems FUSE filesystem.

        Args:
            root: Root directory for the filesystem operations
            cache_ttl: Cache time-to-live in seconds (default: 1 hour)
            cache_dir: Directory for persistent cache storage
            filter_config: Version filter configuration dictionary
            mount_point: Mount point path for namespaced configuration
            use_ruby: List of USE_RUBY flags (e.g., ['ruby32', 'ruby33'])
            patch_file: Path to patch file for slot/dependency overrides
            no_patches: If True, disable the patching system entirely
        """
        self.root = root
        self.cache_ttl = cache_ttl
        self.mount_point = mount_point
        self.use_ruby = use_ruby or ['ruby32', 'ruby33']
        self.no_patches = no_patches

        # Content cache: path -> (content, timestamp)
        self._content_cache: Dict[str, Tuple[bytes, float]] = {}

        # Package metadata cache: gem_name -> (metadata, timestamp)
        self._metadata_cache: Dict[str, Tuple[dict, float]] = {}

        # Category listing cache: category -> (package_list, timestamp)
        self._category_cache: Dict[str, Tuple[List[str], float]] = {}

        # Versions cache: gem_name -> (versions_list, timestamp)
        self._versions_cache: Dict[str, Tuple[List[str], float]] = {}

        # Initialize components
        self.name_translator = create_rubygems_translator()

        # Initialize metadata provider
        self.metadata_provider = RubyGemsMetadataProvider(
            cache_dir=cache_dir,
            cache_ttl=cache_ttl
        )

        # Initialize ebuild generator
        self.ebuild_generator = RubyGemsEbuildGenerator(
            use_ruby=self.use_ruby,
            name_translator=self.name_translator
        )

        # Set up version filters
        self.version_filter_chain = self._create_version_filter(filter_config or {})

        # Initialize patch stores
        if not no_patches:
            patch_path = patch_file or str(DEFAULT_PATCH_FILE)

            # Slot override store
            self.slot_store = SlotPatchStore(patch_path, mount_point=mount_point)

            # Dependency patch stores (RDEPEND and DEPEND)
            self.dep_patch_store = DependencyPatchStore(patch_path, mount_point=mount_point)

            # Ruby compatibility store (USE_RUBY)
            self.ruby_compat_store = RubyCompatPatchStore(patch_path, mount_point=mount_point)

            # IUSE patch store
            self.iuse_patch_store = IUSEPatchStore(patch_path, mount_point=mount_point)

            # Ebuild append patch store (phase functions)
            self.append_patch_store = EbuildAppendPatchStore(patch_path, mount_point=mount_point)

            # Git source patch store
            self.git_source_patch_store = GitSourcePatchStore(patch_path, mount_point=mount_point)

            # Name translation store
            self.name_translation_store = NameTranslationPatchStore(patch_path, mount_point=mount_point)

            logger.info(f"Patching enabled, using {patch_path}"
                       + (f" (mount: {mount_point})" if mount_point else ""))
        else:
            self.slot_store = None
            self.dep_patch_store = None
            self.ruby_compat_store = None
            self.iuse_patch_store = None
            self.append_patch_store = None
            self.git_source_patch_store = None
            self.name_translation_store = None
            logger.info("Patching disabled")

        # Static overlay structure
        self.static_dirs = {
            "/",
            "/dev-ruby",
            "/profiles",
            "/metadata",
            "/eclass",
            "/.sys",
            # SLOT patching
            "/.sys/slot",
            "/.sys/slot/dev-ruby",
            # RDEPEND/DEPEND patching
            "/.sys/RDEPEND", "/.sys/RDEPEND/dev-ruby",
            "/.sys/RDEPEND-patch", "/.sys/RDEPEND-patch/dev-ruby",
            "/.sys/DEPEND", "/.sys/DEPEND/dev-ruby",
            "/.sys/DEPEND-patch", "/.sys/DEPEND-patch/dev-ruby",
            # Ruby compatibility
            "/.sys/ruby-compat", "/.sys/ruby-compat/dev-ruby",
            "/.sys/ruby-compat-patch", "/.sys/ruby-compat-patch/dev-ruby",
            # IUSE
            "/.sys/iuse", "/.sys/iuse/dev-ruby",
            "/.sys/iuse-patch", "/.sys/iuse-patch/dev-ruby",
            # Ebuild append
            "/.sys/ebuild-append", "/.sys/ebuild-append/dev-ruby",
            "/.sys/ebuild-append-patch", "/.sys/ebuild-append-patch/dev-ruby",
            # Git source
            "/.sys/git-source", "/.sys/git-source/dev-ruby",
            "/.sys/git-source-patch", "/.sys/git-source-patch/dev-ruby",
            # Name translation (global, not per-category)
            "/.sys/name-translation",
        }

        # Static files
        self.static_files = {
            "/profiles/repo_name": (RUBYGEMS_REPO_NAME + "\n").encode('utf-8'),
            "/metadata/layout.conf": self._generate_layout_conf().encode('utf-8')
        }

        # Maximum versions to show per package (0 = unlimited)
        self.max_versions = (filter_config or {}).get('max_versions', 0)

        logger.info(f"PortageGemFS initialized with USE_RUBY: {', '.join(self.use_ruby)}")
        if self.version_filter_chain:
            logger.info(f"Version filters: {self.version_filter_chain.get_description()}")

    def _create_version_filter(self, filter_config: Dict) -> Optional[VersionFilterChain]:
        """Create version filter chain based on configuration.

        Default filters (run unless --no-filter disables them):
        - gentoo-version: Filter out versions that can't be translated to PMS format
        - ruby-compat: Filter by USE_RUBY compatibility
        - platform: Filter out java, mswin, etc.
        - gem-source: Filter to gems with .gem files or git sources

        Optional filters (only run if --filter enables them):
        - pre-release: Filter out alpha/beta/rc versions (prefer portage masking)
        """
        enabled_filters = set(filter_config.get('enabled_filters', []))
        disabled_filters = set(filter_config.get('disabled_filters', []))

        filters = []

        # Gentoo version format filter (default: enabled)
        # Non-translatable versions would produce invalid ebuild names
        if 'gentoo-version' not in disabled_filters:
            filters.append(GentooVersionFilter())

        # Ruby compatibility filter (default: enabled)
        if 'ruby-compat' not in disabled_filters:
            filters.append(RubyCompatFilter(use_ruby=self.use_ruby))

        # Platform filter: DISABLED by default
        # Platform-specific gems now get platform-appropriate KEYWORDS instead
        # of being filtered out. This provides better user feedback (e.g.,
        # "no KEYWORDS for your arch" vs cryptic checksum errors)
        if 'platform' in enabled_filters and 'platform' not in disabled_filters:
            filters.append(PlatformFilter())

        # Pre-release filter (default: disabled, opt-in with --filter pre-release)
        # Prefer handling pre-releases at portage level via package.mask
        if 'pre-release' in enabled_filters and 'pre-release' not in disabled_filters:
            filters.append(PreReleaseFilter(include_pre=False))

        # Source filter (default: enabled)
        if 'gem-source' not in disabled_filters:
            include_git = filter_config.get('include_git', True)
            filters.append(GemSourceFilter(include_git=include_git))

        if filters:
            return VersionFilterChain(filters)
        return None

    def _generate_layout_conf(self) -> str:
        """Generate layout.conf for the overlay."""
        return f"""repo-name = {RUBYGEMS_REPO_NAME}
masters = gentoo
thin-manifests = true
profile-formats = portage-2
cache-formats = md5-dict
"""

    def _parse_path(self, path: str) -> Dict[str, str]:
        """
        Parse filesystem path and return components.

        Examples:
            >>> fs = PortageGemFS.__new__(PortageGemFS)
            >>> fs._parse_path('/')
            {'type': 'root'}
            >>> fs._parse_path('/dev-ruby')
            {'type': 'category', 'category': 'dev-ruby'}
            >>> fs._parse_path('/dev-ruby/rails')
            {'type': 'package', 'category': 'dev-ruby', 'package': 'rails'}
            >>> fs._parse_path('/dev-ruby/rails/rails-7.0.0.ebuild')
            {'type': 'ebuild', 'category': 'dev-ruby', 'package': 'rails', 'version': '7.0.0', 'filename': 'rails-7.0.0.ebuild'}
            >>> fs._parse_path('/dev-ruby/rails/metadata.xml')
            {'type': 'package_metadata', 'category': 'dev-ruby', 'package': 'rails', 'filename': 'metadata.xml'}
            >>> fs._parse_path('/dev-ruby/rails/Manifest')
            {'type': 'manifest', 'category': 'dev-ruby', 'package': 'rails', 'filename': 'Manifest'}
            >>> fs._parse_path('/profiles/repo_name')
            {'type': 'profiles_file', 'filename': 'repo_name'}
            >>> fs._parse_path('/metadata/layout.conf')
            {'type': 'metadata_file', 'filename': 'layout.conf'}
            >>> fs._parse_path('/.sys/slot')
            {'type': 'sys_slot'}
            >>> fs._parse_path('/.sys/slot/dev-ruby')
            {'type': 'sys_slot_category', 'category': 'dev-ruby'}
            >>> fs._parse_path('/.sys/slot/dev-ruby/rails')
            {'type': 'sys_slot_package', 'category': 'dev-ruby', 'package': 'rails'}
            >>> fs._parse_path('/.sys/slot/dev-ruby/rails/_all')
            {'type': 'sys_slot_version', 'category': 'dev-ruby', 'package': 'rails', 'version': '_all'}
        """
        path = path.strip('/')
        if not path:
            return {'type': 'root'}

        parts = path.split('/')

        # Handle .sys virtual filesystem
        if parts[0] == '.sys':
            return self._parse_sys_path(parts)

        if parts[0] == 'profiles':
            if len(parts) == 1:
                return {'type': 'profiles'}
            elif len(parts) == 2 and parts[1] == 'repo_name':
                return {'type': 'profiles_file', 'filename': 'repo_name'}
            else:
                return {'type': 'invalid'}
        elif parts[0] == 'metadata':
            if len(parts) == 1:
                return {'type': 'metadata'}
            elif len(parts) == 2 and parts[1] == 'layout.conf':
                return {'type': 'metadata_file', 'filename': 'layout.conf'}
            else:
                return {'type': 'invalid'}
        elif parts[0] == 'eclass':
            return {'type': 'eclass', 'filename': parts[-1] if len(parts) > 1 else None}
        elif parts[0] == 'dev-ruby' and len(parts) == 1:
            return {'type': 'category', 'category': 'dev-ruby'}
        elif parts[0] == 'dev-ruby' and len(parts) == 2:
            return {'type': 'package', 'category': 'dev-ruby', 'package': parts[1]}
        elif parts[0] == 'dev-ruby' and len(parts) == 3:
            category, package, filename = parts
            if filename == 'metadata.xml':
                return {'type': 'package_metadata', 'category': category, 'package': package, 'filename': filename}
            elif filename == 'Manifest':
                return {'type': 'manifest', 'category': category, 'package': package, 'filename': filename}
            elif filename.endswith('.ebuild'):
                # Extract version from ebuild filename
                name_version = filename[:-7]  # Remove '.ebuild'
                if name_version.startswith(package + '-'):
                    version = name_version[len(package) + 1:]
                    return {'type': 'ebuild', 'category': category, 'package': package, 'version': version, 'filename': filename}

        return {'type': 'unknown'}

    def _parse_sys_path(self, parts: List[str]) -> Dict[str, str]:
        """
        Parse .sys/ virtual filesystem paths.

        Directory structure:
            .sys/
                slot/
                    dev-ruby/{package}/{version}     # SLOT override
                RDEPEND/
                    dev-ruby/{package}/{version}     # Current RDEPEND list
                RDEPEND-patch/
                    dev-ruby/{package}/{version}.patch  # RDEPEND patches
                DEPEND/
                    dev-ruby/{package}/{version}     # Current DEPEND list
                DEPEND-patch/
                    dev-ruby/{package}/{version}.patch  # DEPEND patches
                ruby-compat/
                    dev-ruby/{package}/{version}/{impl}  # Current USE_RUBY
                ruby-compat-patch/
                    dev-ruby/{package}/{version}.patch   # USE_RUBY patches
                iuse/
                    dev-ruby/{package}/{version}/{flag}  # Current IUSE
                iuse-patch/
                    dev-ruby/{package}/{version}.patch   # IUSE patches
                ebuild-append/
                    dev-ruby/{package}/{version}/{phase} # Phase functions
                ebuild-append-patch/
                    dev-ruby/{package}/{version}.patch   # Phase patches
                git-source/
                    dev-ruby/{package}/{version}     # Git source config
                git-source-patch/
                    dev-ruby/{package}/{version}.patch   # Git source patches
                name-translation/
                    {pypi_name}                      # Name translation
        """
        if len(parts) == 1:
            # /.sys
            return {'type': 'sys_root'}

        control_type = parts[1]

        # Slot overrides
        if control_type == 'slot':
            return self._parse_sys_path_standard(parts, 'slot')

        # RDEPEND (view current dependencies)
        if control_type == 'RDEPEND':
            return self._parse_sys_path_standard(parts, 'rdepend')

        # RDEPEND-patch (modify dependencies)
        if control_type == 'RDEPEND-patch':
            return self._parse_sys_path_patch(parts, 'rdepend_patch')

        # DEPEND (view current build dependencies)
        if control_type == 'DEPEND':
            return self._parse_sys_path_standard(parts, 'depend')

        # DEPEND-patch (modify build dependencies)
        if control_type == 'DEPEND-patch':
            return self._parse_sys_path_patch(parts, 'depend_patch')

        # ruby-compat (view current USE_RUBY)
        if control_type == 'ruby-compat':
            return self._parse_sys_path_with_item(parts, 'ruby_compat')

        # ruby-compat-patch (modify USE_RUBY)
        if control_type == 'ruby-compat-patch':
            return self._parse_sys_path_patch(parts, 'ruby_compat_patch')

        # iuse (view current IUSE)
        if control_type == 'iuse':
            return self._parse_sys_path_with_item(parts, 'iuse')

        # iuse-patch (modify IUSE)
        if control_type == 'iuse-patch':
            return self._parse_sys_path_patch(parts, 'iuse_patch')

        # ebuild-append (view current phase functions)
        if control_type == 'ebuild-append':
            return self._parse_sys_path_with_item(parts, 'ebuild_append')

        # ebuild-append-patch (modify phase functions)
        if control_type == 'ebuild-append-patch':
            return self._parse_sys_path_patch(parts, 'ebuild_append_patch')

        # git-source (view current git source config)
        if control_type == 'git-source':
            return self._parse_sys_path_standard(parts, 'git_source')

        # git-source-patch (modify git source config)
        if control_type == 'git-source-patch':
            return self._parse_sys_path_patch(parts, 'git_source_patch')

        # name-translation (global, not per-category)
        if control_type == 'name-translation':
            if len(parts) == 2:
                return {'type': 'sys_name_translation'}
            elif len(parts) == 3:
                return {'type': 'sys_name_translation_entry', 'pypi_name': parts[2]}

        return {'type': 'invalid'}

    def _parse_sys_path_standard(self, parts: List[str], base_type: str) -> Dict[str, str]:
        """Parse standard .sys path: control/category/package/version."""
        if len(parts) == 2:
            return {'type': f'sys_{base_type}'}
        elif len(parts) == 3:
            return {'type': f'sys_{base_type}_category', 'category': parts[2]}
        elif len(parts) == 4:
            return {'type': f'sys_{base_type}_package', 'category': parts[2], 'package': parts[3]}
        elif len(parts) == 5:
            return {
                'type': f'sys_{base_type}_version',
                'category': parts[2],
                'package': parts[3],
                'version': parts[4]
            }
        return {'type': 'invalid'}

    def _parse_sys_path_patch(self, parts: List[str], base_type: str) -> Dict[str, str]:
        """Parse patch .sys path: control-patch/category/package/version.patch."""
        if len(parts) == 2:
            return {'type': f'sys_{base_type}'}
        elif len(parts) == 3:
            return {'type': f'sys_{base_type}_category', 'category': parts[2]}
        elif len(parts) == 4:
            return {'type': f'sys_{base_type}_package', 'category': parts[2], 'package': parts[3]}
        elif len(parts) == 5:
            version = parts[4]
            # Strip .patch suffix if present
            if version.endswith('.patch'):
                version = version[:-6]
            return {
                'type': f'sys_{base_type}_version',
                'category': parts[2],
                'package': parts[3],
                'version': version
            }
        return {'type': 'invalid'}

    def _parse_sys_path_with_item(self, parts: List[str], base_type: str) -> Dict[str, str]:
        """Parse .sys path with item level: control/category/package/version/item."""
        if len(parts) == 2:
            return {'type': f'sys_{base_type}'}
        elif len(parts) == 3:
            return {'type': f'sys_{base_type}_category', 'category': parts[2]}
        elif len(parts) == 4:
            return {'type': f'sys_{base_type}_package', 'category': parts[2], 'package': parts[3]}
        elif len(parts) == 5:
            return {
                'type': f'sys_{base_type}_version',
                'category': parts[2],
                'package': parts[3],
                'version': parts[4]
            }
        elif len(parts) == 6:
            return {
                'type': f'sys_{base_type}_item',
                'category': parts[2],
                'package': parts[3],
                'version': parts[4],
                'item': parts[5]
            }
        return {'type': 'invalid'}

    def _gentoo_to_gem(self, gentoo_name: str) -> Optional[str]:
        """
        Convert Gentoo package name to gem name.

        Since we preserve original case, this is typically identity.
        """
        return self.name_translator.gentoo_to_rubygems(gentoo_name)

    def _get_package_versions(self, gem_name: str) -> List[str]:
        """Get available versions for a gem package."""
        # Check cache first
        cache_key = f"versions_{gem_name}"
        if cache_key in self._versions_cache:
            cached_versions, timestamp = self._versions_cache[cache_key]
            if time.time() - timestamp < self.cache_ttl:
                return cached_versions
            del self._versions_cache[cache_key]

        try:
            # Use get_versions_metadata to get full version info including platform
            versions_data = self.metadata_provider.get_versions_metadata(gem_name)
            if not versions_data:
                return []

            # Convert to dict format for filtering
            # When multiple platforms exist for the same version, prefer 'ruby' (pure Ruby)
            # over platform-specific builds, as it's universal
            versions_metadata = {}
            for v in versions_data:
                if isinstance(v, dict):
                    version = v.get('number', v.get('version', ''))
                    if version:
                        platform = v.get('platform', 'ruby')
                        existing = versions_metadata.get(version)
                        if existing is None:
                            # First occurrence - store it
                            versions_metadata[version] = v
                        elif platform == 'ruby' and existing.get('platform') != 'ruby':
                            # Prefer 'ruby' (pure Ruby) over platform-specific
                            versions_metadata[version] = v
                        # Otherwise keep existing (don't overwrite ruby with platform-specific)
                elif isinstance(v, str):
                    versions_metadata[v] = {}

            # Apply version filters
            if self.version_filter_chain:
                versions_metadata = self.version_filter_chain.filter_versions(
                    gem_name, versions_metadata
                )

            # Sort versions semantically (newest first)
            # Handle non-PEP440 Ruby versions gracefully
            from packaging.version import Version

            def version_key(v):
                try:
                    # Valid versions get (1, Version) - higher priority
                    return (1, Version(v))
                except Exception:
                    # Invalid/Ruby-style versions get (0, string) - appear at end
                    return (0, v)

            versions = sorted(versions_metadata.keys(), key=version_key, reverse=True)

            # Apply max_versions limit
            if self.max_versions > 0:
                versions = versions[:self.max_versions]

            # Translate versions to Gentoo format
            gentoo_versions = []
            for v in versions:
                gentoo_v = self._translate_gem_version(v)
                if gentoo_v is not None:
                    gentoo_versions.append(gentoo_v)

            # Cache the result
            self._versions_cache[cache_key] = (gentoo_versions, time.time())

            return gentoo_versions

        except Exception as e:
            logger.error(f"Error getting versions for {gem_name}: {e}")
            return []

    def _translate_gem_version(self, gem_version: str) -> Optional[str]:
        """
        Translate gem version string to Gentoo format.

        Converts Ruby pre-release markers:
        - .alpha -> _alpha
        - .beta -> _beta
        - .pre -> _pre
        - .rc -> _rc

        Handles compound suffixes (reversibly):
        - .alpha.pre.4 -> _alpha_pre_p4 (standalone numbers become _p)
        - .beta1.1 -> _beta1_p1
        - .alpha.pre4 -> _alpha_pre4 (attached numbers stay attached)

        Returns None for versions with non-standard suffixes.

        Examples:
            >>> fs = PortageGemFS.__new__(PortageGemFS)
            >>> fs._translate_gem_version('1.0.0')
            '1.0.0'
            >>> fs._translate_gem_version('2.0.0.alpha1')
            '2.0.0_alpha1'
            >>> fs._translate_gem_version('3.0.0.beta2')
            '3.0.0_beta2'
            >>> fs._translate_gem_version('4.0.0.rc1')
            '4.0.0_rc1'
            >>> fs._translate_gem_version('5.0.0.pre')
            '5.0.0_pre'
            >>> fs._translate_gem_version('1.2.3.alpha')
            '1.2.3_alpha'
            >>> fs._translate_gem_version('2.0.0.alpha.pre.4')
            '2.0.0_alpha_pre_p4'
            >>> fs._translate_gem_version('5.0.0.beta1.1')
            '5.0.0_beta1_p1'
            >>> fs._translate_gem_version('5.0.0.racecar1') is None
            True
            >>> fs._translate_gem_version('2.0.0.alpha.pre4')
            '2.0.0_alpha_pre4'
        """
        # Standard Gentoo suffix names (excluding 'p' as it's only for patchlevel)
        standard_suffixes = {'alpha', 'beta', 'pre', 'rc'}

        # Ruby shorthand -> Gentoo suffix (e.g., 5.a -> 5_alpha)
        shorthand_map = {'a': 'alpha', 'b': 'beta'}

        # Split into base version (numbers.numbers...) and suffix
        match = re.match(r'^(\d+(?:\.\d+)*)(.*)$', gem_version)
        if not match:
            return None

        base, suffix = match.groups()

        if not suffix:
            return base  # Pure numeric version

        # Parse suffix components
        suffix = suffix.lstrip('.')
        if not suffix:
            return base

        components = suffix.split('.')

        # Build the Gentoo suffix
        gentoo_suffix = ''
        i = 0
        while i < len(components):
            comp = components[i].lower()

            # Check for Ruby shorthand (a, b)
            if comp in shorthand_map:
                gentoo_suffix += f'_{shorthand_map[comp]}'
                i += 1
            elif comp in standard_suffixes:
                gentoo_suffix += f'_{comp}'
                i += 1
            elif comp.isdigit():
                # Standalone number - treat as patchlevel (_p)
                gentoo_suffix += f'_p{comp}'
                i += 1
            elif re.match(r'^([ab])(\d+)$', comp):
                # Shorthand with number (a1 -> alpha1, b2 -> beta2)
                m = re.match(r'^([ab])(\d+)$', comp)
                gentoo_suffix += f'_{shorthand_map[m.group(1)]}{m.group(2)}'
                i += 1
            elif re.match(r'^([a-z]+)(\d+)$', comp):
                # Combined suffix like 'alpha1', 'beta2', 'pre4'
                m = re.match(r'^([a-z]+)(\d+)$', comp)
                name, num = m.groups()
                if name in standard_suffixes:
                    gentoo_suffix += f'_{name}{num}'
                    i += 1
                else:
                    # Non-standard suffix
                    return None
            else:
                # Non-standard suffix
                return None

        return base + gentoo_suffix

    def _gentoo_to_gem_version(self, gentoo_version: str) -> str:
        """
        Convert Gentoo version back to gem version.

        Reverses the pre-release marker translation.

        Examples:
            >>> fs = PortageGemFS.__new__(PortageGemFS)
            >>> fs._gentoo_to_gem_version('1.0.0')
            '1.0.0'
            >>> fs._gentoo_to_gem_version('2.0.0_alpha1')
            '2.0.0.alpha1'
            >>> fs._gentoo_to_gem_version('3.0.0_beta2')
            '3.0.0.beta2'
            >>> fs._gentoo_to_gem_version('4.0.0_rc1')
            '4.0.0.rc1'
            >>> fs._gentoo_to_gem_version('5.0.0_pre')
            '5.0.0.pre'
            >>> fs._gentoo_to_gem_version('2.0.0_alpha_pre_p4')
            '2.0.0.alpha.pre.4'
            >>> fs._gentoo_to_gem_version('5.0.0_beta1_p1')
            '5.0.0.beta1.1'
            >>> fs._gentoo_to_gem_version('2.0.0_alpha_pre4')
            '2.0.0.alpha.pre4'
        """
        version = gentoo_version

        # Reverse the patchlevel suffix first (e.g., _p1 -> .1)
        version = re.sub(r'_p(\d+)', r'.\1', version)

        # Reverse the pre-release marker translation
        # Handle suffixes with and without numbers
        version = re.sub(r'_alpha(\d*)', r'.alpha\1', version)
        version = re.sub(r'_beta(\d*)', r'.beta\1', version)
        version = re.sub(r'_pre(\d*)', r'.pre\1', version)
        version = re.sub(r'_rc(\d*)', r'.rc\1', version)

        return version

    def _get_package_info(self, gem_name: str) -> Optional[Dict[str, Any]]:
        """Get package metadata from RubyGems."""
        # Check cache first
        if gem_name in self._metadata_cache:
            cached_data, timestamp = self._metadata_cache[gem_name]
            if time.time() - timestamp < self.cache_ttl:
                return cached_data
            del self._metadata_cache[gem_name]

        try:
            info = self.metadata_provider.get_package_info(gem_name)
            if info:
                self._metadata_cache[gem_name] = (info, time.time())
            return info
        except Exception as e:
            logger.error(f"Error getting info for {gem_name}: {e}")
            return None

    def _get_version_platform(self, gem_name: str, gem_version: str) -> Optional[str]:
        """
        Get platform for a specific gem version from the versions list.

        This is used when version-specific metadata isn't available but we
        still need to determine the platform for KEYWORDS generation.

        Args:
            gem_name: Name of the gem
            gem_version: Gem version string (not Gentoo format)

        Returns:
            Platform string (e.g., 'ruby', 'java', 'x86_64-linux') or None
        """
        try:
            versions_data = self.metadata_provider.get_versions_metadata(gem_name)
            for v in versions_data:
                if isinstance(v, dict) and v.get('number') == gem_version:
                    return v.get('platform', 'ruby')
        except Exception as e:
            logger.debug(f"Error getting platform for {gem_name}-{gem_version}: {e}")
        return None

    def _generate_ebuild(self, gentoo_name: str, gem_name: str, version: str) -> str:
        """Generate ebuild content for a gem package."""
        # Get gem version (translate back from Gentoo format)
        gem_version = self._gentoo_to_gem_version(version)

        # Gather all patch data
        patch_data = self._gather_patch_data('dev-ruby', gentoo_name, version)

        # Get version-specific metadata (includes correct dependencies for THIS version)
        info = self.metadata_provider.get_version_info(gem_name, gem_version)

        # Extract platform from version metadata (needed for KEYWORDS)
        platform = None
        if info:
            platform = info.get('platform', 'ruby')
        else:
            # Try to get platform from versions list
            platform = self._get_version_platform(gem_name, gem_version)

        if not info:
            # Fall back to package-level info (may have wrong deps, but better than nothing)
            info = self._get_package_info(gem_name)

        if not info:
            # Minimal ebuild if no metadata available
            return self._generate_minimal_ebuild(
                gentoo_name, gem_name, version,
                slot_override=patch_data.get('slot_override'),
                platform=platform
            )

        # Use the ebuild generator
        return self.ebuild_generator.generate_ebuild(
            package_info=info,
            version=gem_version,
            gentoo_name=gentoo_name,
            platform=platform,
            **patch_data
        )

    def _gather_patch_data(self, category: str, package: str, version: str) -> Dict[str, Any]:
        """Gather all patch data for a package version."""
        patch_data: Dict[str, Any] = {}

        # Slot override
        if self.slot_store:
            slot = self.slot_store.get(category, package, version)
            if slot:
                patch_data['slot_override'] = slot

        # Dependency patches
        if self.dep_patch_store:
            rdepend_patches = [p for p in self.dep_patch_store.get_patches(category, package, version)
                              if p.dep_type == 'rdepend']
            depend_patches = [p for p in self.dep_patch_store.get_patches(category, package, version)
                             if p.dep_type == 'depend']
            if rdepend_patches:
                patch_data['rdepend_patches'] = rdepend_patches
            if depend_patches:
                patch_data['depend_patches'] = depend_patches

        # Ruby compatibility patches
        if self.ruby_compat_store:
            ruby_compat_patches = self.ruby_compat_store.get_patches(category, package, version)
            if ruby_compat_patches:
                patch_data['ruby_compat_patches'] = ruby_compat_patches

        # IUSE patches
        if self.iuse_patch_store:
            iuse_patches = self.iuse_patch_store.get_patches(category, package, version)
            if iuse_patches:
                patch_data['iuse_patches'] = iuse_patches

        # Ebuild append patches (phase functions)
        if self.append_patch_store:
            phases = self.append_patch_store.get_phases(category, package, version)
            if phases:
                patch_data['ebuild_append'] = phases

        # Git source patches
        if self.git_source_patch_store:
            mode, url, pattern = self.git_source_patch_store.get_git_source(category, package, version)
            if mode:
                patch_data['git_source'] = {
                    'mode': mode,
                    'url': url,
                    'pattern': pattern
                }

        return patch_data

    def _generate_minimal_ebuild(self, gentoo_name: str, gem_name: str, version: str,
                                   slot_override: Optional[str] = None,
                                   platform: Optional[str] = None) -> str:
        """Generate minimal ebuild when metadata is unavailable."""
        from .plugin import platform_to_keywords

        gem_version = self._gentoo_to_gem_version(version)
        use_ruby = ' '.join(self.use_ruby)
        gem_platform = platform or 'ruby'

        # Get platform-based KEYWORDS
        base_keywords = platform_to_keywords(gem_platform)

        # Check version string for pre-release patterns
        is_prerelease = False
        prerelease_patterns = [
            r'\.alpha\d*$', r'\.beta\d*$', r'\.rc\d*$', r'\.pre\d*$',
            r'\.alpha\.', r'\.beta\.', r'\.rc\.', r'\.pre\.',
        ]
        for pattern in prerelease_patterns:
            if re.search(pattern, gem_version, re.IGNORECASE):
                is_prerelease = True
                break

        # Determine KEYWORDS and comment
        keywords_comment = ""
        if is_prerelease:
            keywords = ''
            keywords_comment = "# Pre-release version - no KEYWORDS (requires explicit keywording)\n"
        elif not base_keywords:
            keywords = ''
            keywords_comment = f"# Platform '{gem_platform}' - no compatible Gentoo architecture\n"
        else:
            keywords = base_keywords

        slot = slot_override if slot_override else '0'

        return f'''# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

USE_RUBY="{use_ruby}"
RUBY_FAKEGEM_RECIPE_TEST="none"
RUBY_FAKEGEM_RECIPE_DOC="none"
RUBY_FAKEGEM_BINWRAP=""

inherit ruby-fakegem

DESCRIPTION="{gem_name} gem"
HOMEPAGE="https://rubygems.org/gems/{gem_name}"
SRC_URI="https://rubygems.org/gems/{gem_name}-{gem_version}.gem"

LICENSE="MIT"
SLOT="{slot}"
{keywords_comment}KEYWORDS="{keywords}"
'''

    def _generate_metadata_xml(self, gentoo_name: str, gem_name: str) -> str:
        """Generate metadata.xml for a package."""
        info = self._get_package_info(gem_name)

        description = gem_name
        homepage = f"https://rubygems.org/gems/{gem_name}"

        if info:
            description = info.get('info', info.get('summary', gem_name))
            homepage = info.get('homepage_uri', info.get('project_uri', homepage))

        # Escape XML entities
        description = (description or gem_name).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE pkgmetadata SYSTEM "https://www.gentoo.org/dtd/metadata.dtd">
<pkgmetadata>
  <maintainer type="project">
    <email>ruby@gentoo.org</email>
    <name>Ruby Project</name>
  </maintainer>
  <upstream>
    <remote-id type="rubygems">{gem_name}</remote-id>
  </upstream>
  <longdescription lang="en">
    {description}
  </longdescription>
</pkgmetadata>
'''

    def _generate_manifest(self, gentoo_name: str, gem_name: str) -> str:
        """
        Generate Manifest file with checksums.

        RubyGems API provides SHA256 checksums but not file sizes.
        We fetch sizes via HEAD requests (cached) when needed.
        """
        lines = []
        versions = self._get_package_versions(gem_name)

        # Build a map of version -> sha from API data
        version_sha_map = {}
        try:
            versions_data = self.metadata_provider.get_versions_metadata(gem_name)
            for v in versions_data:
                if isinstance(v, dict):
                    num = v.get('number', '')
                    sha = v.get('sha', '')
                    if num and sha:
                        version_sha_map[num] = sha
        except Exception:
            pass

        for version in versions:
            gem_version = self._gentoo_to_gem_version(version)
            gem_filename = f"{gem_name}-{gem_version}.gem"

            sha256 = version_sha_map.get(gem_version, '')
            if sha256:
                # Get file size (cached)
                size = self._get_gem_file_size(gem_name, gem_version)
                if size > 0:
                    lines.append(f"DIST {gem_filename} {size} SHA256 {sha256}")

        return '\n'.join(lines) + '\n' if lines else ''

    def _get_gem_file_size(self, gem_name: str, version: str) -> int:
        """
        Get gem file size via HEAD request (cached).

        Args:
            gem_name: Name of the gem
            version: Version string

        Returns:
            File size in bytes, or 0 if unavailable
        """
        cache_key = f"size_{gem_name}_{version}"

        # Check cache
        if cache_key in self._metadata_cache:
            cached_size, timestamp = self._metadata_cache[cache_key]
            if time.time() - timestamp < self.cache_ttl:
                return cached_size

        gem_url = f"https://rubygems.org/gems/{gem_name}-{version}.gem"

        try:
            import urllib.request
            req = urllib.request.Request(gem_url, method='HEAD')
            req.add_header('User-Agent', 'portage-gem-fuse/0.1')

            with urllib.request.urlopen(req, timeout=10) as response:
                size = int(response.headers.get('Content-Length', 0))
                # Cache the result
                self._metadata_cache[cache_key] = (size, time.time())
                return size
        except Exception as e:
            logger.debug(f"Failed to get size for {gem_name}-{version}.gem: {e}")
            return 0

    def getattr(self, path, fh=None):
        """Get file attributes."""
        parsed = self._parse_path(path)

        # Debug logging for .sys paths
        if path.startswith('/.sys'):
            logger.info(f"getattr() .sys path: {path} -> {parsed}")

        # Current time for timestamps
        now = time.time()

        # Common attributes
        uid = os.getuid()
        gid = os.getgid()

        # Directory types for all .sys controls
        sys_dir_types = (
            'sys_root',
            # slot
            'sys_slot', 'sys_slot_category', 'sys_slot_package',
            # rdepend
            'sys_rdepend', 'sys_rdepend_category', 'sys_rdepend_package',
            'sys_rdepend_patch', 'sys_rdepend_patch_category', 'sys_rdepend_patch_package',
            # depend
            'sys_depend', 'sys_depend_category', 'sys_depend_package',
            'sys_depend_patch', 'sys_depend_patch_category', 'sys_depend_patch_package',
            # ruby-compat
            'sys_ruby_compat', 'sys_ruby_compat_category', 'sys_ruby_compat_package', 'sys_ruby_compat_version',
            'sys_ruby_compat_patch', 'sys_ruby_compat_patch_category', 'sys_ruby_compat_patch_package',
            # iuse
            'sys_iuse', 'sys_iuse_category', 'sys_iuse_package', 'sys_iuse_version',
            'sys_iuse_patch', 'sys_iuse_patch_category', 'sys_iuse_patch_package',
            # ebuild-append
            'sys_ebuild_append', 'sys_ebuild_append_category', 'sys_ebuild_append_package', 'sys_ebuild_append_version',
            'sys_ebuild_append_patch', 'sys_ebuild_append_patch_category', 'sys_ebuild_append_patch_package',
            # git-source
            'sys_git_source', 'sys_git_source_category', 'sys_git_source_package',
            'sys_git_source_patch', 'sys_git_source_patch_category', 'sys_git_source_patch_package',
            # name-translation
            'sys_name_translation',
        )

        # Directory attributes
        if parsed['type'] in ('root', 'profiles', 'metadata', 'eclass', 'category', 'package') + sys_dir_types:
            return {
                'st_mode': stat.S_IFDIR | 0o755,
                'st_nlink': 2,
                'st_uid': uid,
                'st_gid': gid,
                'st_size': 4096,
                'st_atime': now,
                'st_mtime': now,
                'st_ctime': now,
            }

        # Static files
        if path in self.static_files:
            content = self.static_files[path]
            return {
                'st_mode': stat.S_IFREG | 0o644,
                'st_nlink': 1,
                'st_uid': uid,
                'st_gid': gid,
                'st_size': len(content),
                'st_atime': now,
                'st_mtime': now,
                'st_ctime': now,
            }

        # Package files (ebuild, metadata.xml, Manifest)
        if parsed['type'] in ('ebuild', 'package_metadata', 'manifest'):
            # Generate content to get size
            content = self._get_file_content(path, parsed)
            if content is not None:
                return {
                    'st_mode': stat.S_IFREG | 0o644,
                    'st_nlink': 1,
                    'st_uid': uid,
                    'st_gid': gid,
                    'st_size': len(content),
                    'st_atime': now,
                    'st_mtime': now,
                    'st_ctime': now,
                }

        # Slot override files (writable)
        if parsed['type'] == 'sys_slot_version':
            if self.slot_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                slot = self.slot_store.get(category, package, version)
                if slot is not None:
                    content = (slot + '\n').encode('utf-8')
                    return {
                        'st_mode': stat.S_IFREG | 0o644,
                        'st_nlink': 1,
                        'st_uid': uid,
                        'st_gid': gid,
                        'st_size': len(content),
                        'st_atime': now,
                        'st_mtime': now,
                        'st_ctime': now,
                    }
            # File doesn't exist yet but can be created
            raise FuseOSError(errno.ENOENT)

        # Handle all other .sys file types
        sys_file_result = self._getattr_sys_file(parsed, uid, gid, now)
        if sys_file_result is not None:
            return sys_file_result

        raise FuseOSError(errno.ENOENT)

    def _getattr_sys_file(self, parsed: Dict, uid: int, gid: int, now: float) -> Optional[Dict]:
        """Get attributes for .sys virtual files."""
        ptype = parsed.get('type', '')

        def file_attr(content_bytes):
            return {
                'st_mode': stat.S_IFREG | 0o644,
                'st_nlink': 1,
                'st_uid': uid,
                'st_gid': gid,
                'st_size': len(content_bytes),
                'st_atime': now,
                'st_mtime': now,
                'st_ctime': now,
            }

        # RDEPEND/DEPEND patch files - always return valid attributes for writable files
        if ptype in ('sys_rdepend_patch_version', 'sys_depend_patch_version'):
            if self.dep_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                dep_type = 'rdepend' if 'rdepend' in ptype else 'depend'
                patches = [p for p in self.dep_patch_store.get_patches(category, package, version)
                           if p.dep_type == dep_type]
                if patches:
                    content = self.dep_patch_store.generate_patch_file(category, package, version)
                    return file_attr(content.encode('utf-8'))
                # Return empty file attributes for writable files that don't exist yet
                return file_attr(b'')
            return None

        # Ruby-compat item files (individual implementation files)
        if ptype == 'sys_ruby_compat_item':
            if self.ruby_compat_store:
                # Item is implementation name like 'ruby34'
                # File exists if that impl is in the patched list
                return file_attr(b'')
            return None

        # Ruby-compat patch files
        if ptype == 'sys_ruby_compat_patch_version':
            if self.ruby_compat_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                if self.ruby_compat_store.has_patches(category, package, version):
                    content = self.ruby_compat_store.generate_patch_file(category, package, version)
                    return file_attr(content.encode('utf-8'))
                return file_attr(b'')
            return None

        # IUSE item files (individual USE flag files)
        if ptype == 'sys_iuse_item':
            if self.iuse_patch_store:
                return file_attr(b'')
            return None

        # IUSE patch files
        if ptype == 'sys_iuse_patch_version':
            if self.iuse_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                if self.iuse_patch_store.has_patches(category, package, version):
                    content = self.iuse_patch_store.generate_patch_file(category, package, version)
                    return file_attr(content.encode('utf-8'))
                return file_attr(b'')
            return None

        # Ebuild append item files (phase function files)
        if ptype == 'sys_ebuild_append_item':
            if self.append_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                phase = parsed['item']
                content = self.append_patch_store.get_phase(category, package, version, phase)
                if content:
                    return file_attr((content + '\n').encode('utf-8'))
            return None

        # Ebuild append patch files
        if ptype == 'sys_ebuild_append_patch_version':
            if self.append_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                if self.append_patch_store.has_phases(category, package, version):
                    content = self.append_patch_store.generate_patch_file(category, package, version)
                    return file_attr(content.encode('utf-8'))
                return file_attr(b'')
            return None

        # Git source version files
        if ptype == 'sys_git_source_version':
            if self.git_source_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                mode, url, pattern = self.git_source_patch_store.get_git_source(category, package, version)
                if mode:
                    content = self.git_source_patch_store.generate_patch_file(category, package, version)
                    return file_attr(content.encode('utf-8'))
                return file_attr(b'')
            return None

        # Git source patch files
        if ptype == 'sys_git_source_patch_version':
            if self.git_source_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                mode, _, _ = self.git_source_patch_store.get_git_source(category, package, version)
                if mode:
                    content = self.git_source_patch_store.generate_patch_file(category, package, version)
                    return file_attr(content.encode('utf-8'))
                return file_attr(b'')
            return None

        # Name translation entry files
        if ptype == 'sys_name_translation_entry':
            if self.name_translation_store:
                pypi_name = parsed['pypi_name']
                atom = self.name_translation_store.get_mapping(pypi_name)
                if atom:
                    return file_attr((atom + '\n').encode('utf-8'))
                return file_attr(b'')
            return None

        return None

    def _readdir_sys(self, parsed: Dict, entries: List[str]) -> None:
        """Handle readdir for .sys virtual directories."""
        ptype = parsed.get('type', '')

        # Category-level directories (show 'dev-ruby')
        category_types = (
            'sys_rdepend', 'sys_rdepend_patch', 'sys_depend', 'sys_depend_patch',
            'sys_ruby_compat', 'sys_ruby_compat_patch', 'sys_iuse', 'sys_iuse_patch',
            'sys_ebuild_append', 'sys_ebuild_append_patch',
            'sys_git_source', 'sys_git_source_patch',
        )
        if ptype in category_types:
            entries.append('dev-ruby')
            return

        # Package-level directories (show packages with patches)
        if ptype == 'sys_rdepend_patch_category' or ptype == 'sys_depend_patch_category':
            if self.dep_patch_store:
                dep_type = 'rdepend' if 'rdepend' in ptype else 'depend'
                category = parsed['category']
                packages = set()
                for cat, pkg, ver in self.dep_patch_store.list_patched_packages():
                    if cat == category:
                        # Check if any patches are for this dep_type
                        patches = self.dep_patch_store.get_patches(cat, pkg, ver)
                        if any(p.dep_type == dep_type for p in patches):
                            packages.add(pkg)
                entries.extend(sorted(packages))
            return

        if ptype in ('sys_rdepend_category', 'sys_depend_category'):
            # These show current deps - nothing to list unless we fetch from metadata
            return

        if ptype == 'sys_ruby_compat_patch_category':
            if self.ruby_compat_store:
                category = parsed['category']
                packages = set()
                for cat, pkg, ver in self.ruby_compat_store.list_patched_packages():
                    if cat == category:
                        packages.add(pkg)
                entries.extend(sorted(packages))
            return

        if ptype == 'sys_ruby_compat_category':
            # Show packages with ruby_compat patches
            if self.ruby_compat_store:
                category = parsed['category']
                packages = set()
                for cat, pkg, ver in self.ruby_compat_store.list_patched_packages():
                    if cat == category:
                        packages.add(pkg)
                entries.extend(sorted(packages))
            return

        if ptype == 'sys_iuse_patch_category':
            if self.iuse_patch_store:
                category = parsed['category']
                packages = set()
                for cat, pkg, ver in self.iuse_patch_store.list_patched_packages():
                    if cat == category:
                        packages.add(pkg)
                entries.extend(sorted(packages))
            return

        if ptype == 'sys_iuse_category':
            if self.iuse_patch_store:
                category = parsed['category']
                packages = set()
                for cat, pkg, ver in self.iuse_patch_store.list_patched_packages():
                    if cat == category:
                        packages.add(pkg)
                entries.extend(sorted(packages))
            return

        if ptype == 'sys_ebuild_append_patch_category':
            if self.append_patch_store:
                category = parsed['category']
                packages = set()
                for cat, pkg, ver in self.append_patch_store.list_patched_packages():
                    if cat == category:
                        packages.add(pkg)
                entries.extend(sorted(packages))
            return

        if ptype == 'sys_ebuild_append_category':
            if self.append_patch_store:
                category = parsed['category']
                packages = set()
                for cat, pkg, ver in self.append_patch_store.list_patched_packages():
                    if cat == category:
                        packages.add(pkg)
                entries.extend(sorted(packages))
            return

        if ptype == 'sys_git_source_patch_category' or ptype == 'sys_git_source_category':
            if self.git_source_patch_store:
                category = parsed['category']
                packages = set()
                for cat, pkg, ver in self.git_source_patch_store.list_patched_packages():
                    if cat == category:
                        packages.add(pkg)
                entries.extend(sorted(packages))
            return

        # Version-level (patch) directories
        if ptype == 'sys_rdepend_patch_package' or ptype == 'sys_depend_patch_package':
            if self.dep_patch_store:
                category = parsed['category']
                package = parsed['package']
                versions = self.dep_patch_store.get_package_versions_with_patches(category, package)
                entries.extend([v + '.patch' for v in versions])
            return

        if ptype == 'sys_ruby_compat_patch_package':
            if self.ruby_compat_store:
                category = parsed['category']
                package = parsed['package']
                versions = self.ruby_compat_store.get_package_versions_with_patches(category, package)
                entries.extend([v + '.patch' for v in versions])
            return

        if ptype == 'sys_ruby_compat_package':
            if self.ruby_compat_store:
                category = parsed['category']
                package = parsed['package']
                versions = self.ruby_compat_store.get_package_versions_with_patches(category, package)
                entries.extend(versions)
            return

        if ptype == 'sys_iuse_patch_package':
            if self.iuse_patch_store:
                category = parsed['category']
                package = parsed['package']
                versions = self.iuse_patch_store.get_package_versions_with_patches(category, package)
                entries.extend([v + '.patch' for v in versions])
            return

        if ptype == 'sys_iuse_package':
            if self.iuse_patch_store:
                category = parsed['category']
                package = parsed['package']
                versions = self.iuse_patch_store.get_package_versions_with_patches(category, package)
                entries.extend(versions)
            return

        if ptype == 'sys_ebuild_append_patch_package':
            if self.append_patch_store:
                category = parsed['category']
                package = parsed['package']
                versions = self.append_patch_store.get_package_versions_with_phases(category, package)
                entries.extend([v + '.patch' for v in versions])
            return

        if ptype == 'sys_ebuild_append_package':
            if self.append_patch_store:
                category = parsed['category']
                package = parsed['package']
                versions = self.append_patch_store.get_package_versions_with_phases(category, package)
                entries.extend(versions)
            return

        if ptype == 'sys_git_source_patch_package' or ptype == 'sys_git_source_package':
            if self.git_source_patch_store:
                category = parsed['category']
                package = parsed['package']
                versions = self.git_source_patch_store.get_package_versions_with_patches(category, package)
                if 'patch' in ptype:
                    entries.extend([v + '.patch' for v in versions])
                else:
                    entries.extend(versions)
            return

        # Version-level directories (show items)
        if ptype == 'sys_ruby_compat_version':
            # Show implementation files (ruby32, ruby33, etc.)
            if self.ruby_compat_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                # Get patched implementations
                patches = self.ruby_compat_store.get_patches(category, package, version)
                impls = set()
                for patch in patches:
                    if patch.operation == 'add' and patch.impl:
                        impls.add(patch.impl)
                    elif patch.operation == 'set' and patch.impls:
                        impls.update(patch.impls)
                entries.extend(sorted(impls))
            return

        if ptype == 'sys_iuse_version':
            # Show USE flag files
            if self.iuse_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                flags = self.iuse_patch_store.get_current_flags(category, package, version)
                entries.extend(sorted(flags))
            return

        if ptype == 'sys_ebuild_append_version':
            # Show phase files (src_configure, etc.)
            if self.append_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                phases = self.append_patch_store.get_phases(category, package, version)
                entries.extend(sorted(phases.keys()))
            return

        # Name translation directory
        if ptype == 'sys_name_translation':
            if self.name_translation_store:
                entries.extend(sorted(self.name_translation_store.list_mappings()))
            return

    def _get_file_content(self, path: str, parsed: Dict) -> Optional[bytes]:
        """Get file content, using cache when available."""
        # Check cache first
        if path in self._content_cache:
            content, timestamp = self._content_cache[path]
            if time.time() - timestamp < self.cache_ttl:
                return content
            del self._content_cache[path]

        content = None

        if parsed['type'] == 'ebuild':
            gentoo_name = parsed['package']
            gem_name = self._gentoo_to_gem(gentoo_name)
            if gem_name:
                ebuild = self._generate_ebuild(gentoo_name, gem_name, parsed['version'])
                content = ebuild.encode('utf-8')

        elif parsed['type'] == 'package_metadata':
            gentoo_name = parsed['package']
            gem_name = self._gentoo_to_gem(gentoo_name)
            if gem_name:
                metadata = self._generate_metadata_xml(gentoo_name, gem_name)
                content = metadata.encode('utf-8')

        elif parsed['type'] == 'manifest':
            gentoo_name = parsed['package']
            gem_name = self._gentoo_to_gem(gentoo_name)
            if gem_name:
                manifest = self._generate_manifest(gentoo_name, gem_name)
                content = manifest.encode('utf-8')

        # Cache the content
        if content is not None:
            self._content_cache[path] = (content, time.time())

        return content

    def readdir(self, path, fh):
        """Read directory contents."""
        parsed = self._parse_path(path)
        entries = ['.', '..']

        if parsed['type'] == 'root':
            entries.extend(['dev-ruby', 'profiles', 'metadata', 'eclass', '.sys'])

        elif parsed['type'] == 'sys_root':
            entries.extend([
                'slot', 'RDEPEND', 'RDEPEND-patch', 'DEPEND', 'DEPEND-patch',
                'ruby-compat', 'ruby-compat-patch', 'iuse', 'iuse-patch',
                'ebuild-append', 'ebuild-append-patch', 'git-source', 'git-source-patch',
                'name-translation'
            ])

        elif parsed['type'] == 'sys_slot':
            entries.append('dev-ruby')
            # Also add categories that have slot overrides
            if self.slot_store:
                entries.extend(self.slot_store.list_categories())
            # Deduplicate
            entries = list(dict.fromkeys(entries))

        elif parsed['type'] == 'sys_slot_category':
            # List packages that have slot overrides in this category
            if self.slot_store:
                category = parsed['category']
                entries.extend(sorted(self.slot_store.list_packages(category)))

        elif parsed['type'] == 'sys_slot_package':
            # List versions that have slot overrides for this package
            if self.slot_store:
                category = parsed['category']
                package = parsed['package']
                entries.extend(sorted(self.slot_store.list_versions(category, package)))

        # Handle other .sys directories
        elif parsed['type'].startswith('sys_'):
            self._readdir_sys(parsed, entries)

        elif parsed['type'] == 'profiles':
            entries.append('repo_name')

        elif parsed['type'] == 'metadata':
            entries.append('layout.conf')

        elif parsed['type'] == 'eclass':
            # Empty for now - could add ruby-fakegem.eclass symlink later
            pass

        elif parsed['type'] == 'category' and parsed['category'] == 'dev-ruby':
            # Check cache first
            cache_key = 'dev-ruby'
            use_cache = False
            if cache_key in self._category_cache:
                cached_packages, timestamp = self._category_cache[cache_key]
                if time.time() - timestamp < self.cache_ttl:
                    logger.debug(f"Using cached package list ({len(cached_packages)} packages)")
                    entries.extend(cached_packages)
                    use_cache = True
                else:
                    del self._category_cache[cache_key]

            if not use_cache:
                # Fetch all gem names from RubyGems.org
                try:
                    start_time = time.time()
                    logger.info("Listing all gems from RubyGems.org...")

                    # Get all gem names from the metadata provider
                    gem_names = self.metadata_provider.list_all_packages()

                    # Convert gem names to Gentoo names (preserves original case)
                    gentoo_packages = []
                    for gem_name in gem_names:
                        gentoo_name = self.name_translator.rubygems_to_gentoo(gem_name)
                        if gentoo_name:
                            gentoo_packages.append(gentoo_name)
                        else:
                            # Use gem name directly
                            gentoo_packages.append(gem_name)

                    sorted_packages = sorted(gentoo_packages)

                    # Cache the result
                    self._category_cache[cache_key] = (sorted_packages, time.time())

                    entries.extend(sorted_packages)

                    elapsed = time.time() - start_time
                    logger.info(f"Listed {len(gentoo_packages)} packages in {elapsed:.2f} seconds")

                except Exception as e:
                    logger.error(f"Error listing packages: {e}")
                    logger.warning("Package listing failed, returning empty directory")

        elif parsed['type'] == 'package':
            # List versions and files for a package
            gentoo_name = parsed['package']
            gem_name = self._gentoo_to_gem(gentoo_name)

            if gem_name:
                try:
                    versions = self._get_package_versions(gem_name)
                    if versions:
                        # Add ebuild files for each version
                        for version in versions:
                            entries.append(f"{gentoo_name}-{version}.ebuild")

                        # Add metadata files
                        entries.extend(['metadata.xml', 'Manifest'])
                    else:
                        logger.debug(f"No versions found for {gem_name}")
                except Exception as e:
                    logger.error(f"Error listing files for {gentoo_name}: {e}")

        return entries

    def read(self, path, length, offset, fh):
        """Read file content."""
        # Static files
        if path in self.static_files:
            content = self.static_files[path]
            return content[offset:offset + length]

        # Dynamic files
        parsed = self._parse_path(path)

        # Slot override files
        if parsed['type'] == 'sys_slot_version':
            if self.slot_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                slot = self.slot_store.get(category, package, version)
                if slot is not None:
                    content = (slot + '\n').encode('utf-8')
                    return content[offset:offset + length]
            raise FuseOSError(errno.ENOENT)

        # Handle other .sys file reads
        sys_content = self._read_sys_file(parsed)
        if sys_content is not None:
            return sys_content[offset:offset + length]

        content = self._get_file_content(path, parsed)

        if content is not None:
            return content[offset:offset + length]

        raise FuseOSError(errno.ENOENT)

    def _read_sys_file(self, parsed: Dict) -> Optional[bytes]:
        """Read content from .sys virtual files."""
        ptype = parsed.get('type', '')

        # RDEPEND/DEPEND patch files
        if ptype in ('sys_rdepend_patch_version', 'sys_depend_patch_version'):
            if self.dep_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                content = self.dep_patch_store.generate_patch_file(category, package, version)
                if content:
                    return content.encode('utf-8')
            return None

        # Ruby-compat patch files
        if ptype == 'sys_ruby_compat_patch_version':
            if self.ruby_compat_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                content = self.ruby_compat_store.generate_patch_file(category, package, version)
                if content:
                    return content.encode('utf-8')
            return None

        # Ruby-compat item files (individual impl)
        if ptype == 'sys_ruby_compat_item':
            # Just return empty content - the item exists if it's in directory listing
            return b''

        # IUSE patch files
        if ptype == 'sys_iuse_patch_version':
            if self.iuse_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                content = self.iuse_patch_store.generate_patch_file(category, package, version)
                if content:
                    return content.encode('utf-8')
            return None

        # IUSE item files (individual flags)
        if ptype == 'sys_iuse_item':
            return b''

        # Ebuild append item files (phase functions)
        if ptype == 'sys_ebuild_append_item':
            if self.append_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                phase = parsed['item']
                content = self.append_patch_store.get_phase(category, package, version, phase)
                if content:
                    return (content + '\n').encode('utf-8')
            return None

        # Ebuild append patch files
        if ptype == 'sys_ebuild_append_patch_version':
            if self.append_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                content = self.append_patch_store.generate_patch_file(category, package, version)
                if content:
                    return content.encode('utf-8')
            return None

        # Git source version files
        if ptype == 'sys_git_source_version':
            if self.git_source_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                content = self.git_source_patch_store.generate_patch_file(category, package, version)
                if content:
                    return content.encode('utf-8')
            return None

        # Git source patch files
        if ptype == 'sys_git_source_patch_version':
            if self.git_source_patch_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                content = self.git_source_patch_store.generate_patch_file(category, package, version)
                if content:
                    return content.encode('utf-8')
            return None

        # Name translation entry files
        if ptype == 'sys_name_translation_entry':
            if self.name_translation_store:
                pypi_name = parsed['pypi_name']
                atom = self.name_translation_store.get_mapping(pypi_name)
                if atom:
                    return (atom + '\n').encode('utf-8')
            return None

        return None

    def open(self, path, flags):
        """Open a file."""
        # Check if file exists
        parsed = self._parse_path(path)

        if path in self.static_files:
            return 0

        if parsed['type'] in ('ebuild', 'package_metadata', 'manifest'):
            return 0

        # Slot override files - allow open for both read and write
        if parsed['type'] == 'sys_slot_version':
            # Check if write access is requested
            if (flags & os.O_WRONLY) or (flags & os.O_RDWR):
                if not self.slot_store:
                    raise FuseOSError(errno.EROFS)
                return 0  # Allow open for writing
            # Read access - check if file exists
            if self.slot_store:
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                if self.slot_store.get(category, package, version) is not None:
                    return 0
            raise FuseOSError(errno.ENOENT)

        # Other .sys writable file types
        writable_types = (
            'sys_rdepend_patch_version', 'sys_depend_patch_version',
            'sys_ruby_compat_patch_version', 'sys_ruby_compat_item',
            'sys_iuse_patch_version', 'sys_iuse_item',
            'sys_ebuild_append_patch_version', 'sys_ebuild_append_item',
            'sys_git_source_version', 'sys_git_source_patch_version',
            'sys_name_translation_entry',
        )
        if parsed['type'] in writable_types:
            if (flags & os.O_WRONLY) or (flags & os.O_RDWR):
                # Allow open for writing
                return 0
            # Read access - check if file exists via _read_sys_file
            content = self._read_sys_file(parsed)
            if content is not None:
                return 0
            # Allow opening for write even if doesn't exist
            return 0

        raise FuseOSError(errno.ENOENT)

    def create(self, path, mode, fi=None):
        """Create a file."""
        logger.info(f"create() called: path={path}, mode={mode}")
        parsed = self._parse_path(path)
        logger.info(f"create() parsed: {parsed}")

        # Slot override files
        if parsed['type'] == 'sys_slot_version':
            if not self.slot_store:
                raise FuseOSError(errno.EROFS)

            category = parsed['category']
            package = parsed['package']
            version = parsed['version']

            # Initialize with a placeholder slot value so getattr finds the file
            # The actual value will be set by write()
            # Use "0" as default since it's a valid slot
            self.slot_store.set(category, package, version, "0")
            logger.info(f"create() initialized slot for {category}/{package}/{version}")
            return 0

        # Allow creating patch files - actual content set via write()
        creatable_types = (
            'sys_rdepend_patch_version', 'sys_depend_patch_version',
            'sys_ruby_compat_patch_version', 'sys_ruby_compat_item',
            'sys_iuse_patch_version', 'sys_iuse_item',
            'sys_ebuild_append_patch_version', 'sys_ebuild_append_item',
            'sys_git_source_version', 'sys_git_source_patch_version',
            'sys_name_translation_entry',
        )
        if parsed['type'] in creatable_types:
            logger.info(f"create() allowing creation of {parsed['type']}")
            return 0

        raise FuseOSError(errno.EROFS)

    def write(self, path, data, offset, fh):
        """Write to a file."""
        parsed = self._parse_path(path)

        if parsed['type'] == 'sys_slot_version':
            if not self.slot_store:
                raise FuseOSError(errno.EROFS)

            category = parsed['category']
            package = parsed['package']
            version = parsed['version']

            # Parse the slot value from the data
            try:
                slot = data.decode('utf-8').strip()
                if not slot:
                    # Empty write - ignore
                    return len(data)

                if not is_valid_slot(slot):
                    logger.warning(f"Invalid SLOT value: {slot}")
                    raise FuseOSError(errno.EINVAL)

                self.slot_store.set(category, package, version, slot)
                self.slot_store.save()

                # Invalidate content cache for affected ebuilds
                self._invalidate_package_cache(category, package)

                return len(data)
            except UnicodeDecodeError:
                raise FuseOSError(errno.EINVAL)

        # Handle other .sys writable types
        return self._write_sys_file(parsed, data)

    def _write_sys_file(self, parsed: Dict, data: bytes) -> int:
        """Write content to .sys virtual files."""
        ptype = parsed.get('type', '')

        try:
            content = data.decode('utf-8').strip()
        except UnicodeDecodeError:
            raise FuseOSError(errno.EINVAL)

        if not content:
            return len(data)  # Empty write - ignore

        category = parsed.get('category', '')
        package = parsed.get('package', '')
        version = parsed.get('version', '')

        # RDEPEND/DEPEND patch files
        if ptype in ('sys_rdepend_patch_version', 'sys_depend_patch_version'):
            if not self.dep_patch_store:
                raise FuseOSError(errno.EROFS)
            # Note: parse_patch_file defaults to 'rdepend' - for DEPEND patches,
            # the patch lines should specify the type explicitly if needed
            self.dep_patch_store.parse_patch_file(content, category, package, version)
            self.dep_patch_store.save()
            self._invalidate_package_cache(category, package)
            return len(data)

        # Ruby-compat patch files
        if ptype == 'sys_ruby_compat_patch_version':
            if not self.ruby_compat_store:
                raise FuseOSError(errno.EROFS)
            self.ruby_compat_store.parse_patch_file(content, category, package, version)
            self.ruby_compat_store.save()
            self._invalidate_package_cache(category, package)
            return len(data)

        # Ruby-compat item (individual impl)
        if ptype == 'sys_ruby_compat_item':
            if not self.ruby_compat_store:
                raise FuseOSError(errno.EROFS)
            impl = parsed.get('item', '')
            if not self.ruby_compat_store.is_valid_impl(impl):
                logger.warning(f"Invalid Ruby implementation: {impl}")
                raise FuseOSError(errno.EINVAL)
            self.ruby_compat_store.add_impl(category, package, version, impl)
            self.ruby_compat_store.save()
            self._invalidate_package_cache(category, package)
            return len(data)

        # IUSE patch files
        if ptype == 'sys_iuse_patch_version':
            if not self.iuse_patch_store:
                raise FuseOSError(errno.EROFS)
            self.iuse_patch_store.parse_patch_file(content, category, package, version)
            self.iuse_patch_store.save()
            self._invalidate_package_cache(category, package)
            return len(data)

        # IUSE item (individual flag)
        if ptype == 'sys_iuse_item':
            if not self.iuse_patch_store:
                raise FuseOSError(errno.EROFS)
            flag = parsed.get('item', '')
            if not is_valid_use_flag(flag):
                logger.warning(f"Invalid USE flag: {flag}")
                raise FuseOSError(errno.EINVAL)
            self.iuse_patch_store.add_flag(category, package, version, flag)
            self.iuse_patch_store.save()
            self._invalidate_package_cache(category, package)
            return len(data)

        # Ebuild append item (phase function)
        if ptype == 'sys_ebuild_append_item':
            if not self.append_patch_store:
                raise FuseOSError(errno.EROFS)
            phase = parsed.get('item', '')
            if not is_valid_phase_name(phase):
                logger.warning(f"Invalid phase name: {phase}")
                raise FuseOSError(errno.EINVAL)
            self.append_patch_store.set_phase(category, package, version, phase, content)
            self.append_patch_store.save()
            self._invalidate_package_cache(category, package)
            return len(data)

        # Ebuild append patch files
        if ptype == 'sys_ebuild_append_patch_version':
            if not self.append_patch_store:
                raise FuseOSError(errno.EROFS)
            self.append_patch_store.parse_patch_file(content, category, package, version)
            self.append_patch_store.save()
            self._invalidate_package_cache(category, package)
            return len(data)

        # Git source version/patch files
        if ptype in ('sys_git_source_version', 'sys_git_source_patch_version'):
            if not self.git_source_patch_store:
                raise FuseOSError(errno.EROFS)
            # Parse git source config: "== git [url] [pattern]"
            parts = content.split()
            if len(parts) >= 2 and parts[0] == '==':
                mode = parts[1]
                if not is_valid_source_mode(mode):
                    logger.warning(f"Invalid source mode: {mode}")
                    raise FuseOSError(errno.EINVAL)
                url = parts[2] if len(parts) > 2 else None
                pattern = parts[3] if len(parts) > 3 else None
                self.git_source_patch_store.set_git_source(category, package, version, mode, url, pattern)
            else:
                logger.warning(f"Invalid git source config format: {content}")
                raise FuseOSError(errno.EINVAL)
            self.git_source_patch_store.save()
            self._invalidate_package_cache(category, package)
            return len(data)

        # Name translation entry
        if ptype == 'sys_name_translation_entry':
            if not self.name_translation_store:
                raise FuseOSError(errno.EROFS)
            pypi_name = parsed.get('pypi_name', '')
            if not is_valid_gentoo_atom(content):
                logger.warning(f"Invalid Gentoo atom: {content}")
                raise FuseOSError(errno.EINVAL)
            self.name_translation_store.set_mapping(pypi_name, content)
            self.name_translation_store.save()
            return len(data)

        raise FuseOSError(errno.EROFS)

    def truncate(self, path, length, fh=None):
        """Truncate a file."""
        parsed = self._parse_path(path)

        # Allow truncate on slot override files (for echo > file pattern)
        if parsed['type'] == 'sys_slot_version':
            if not self.slot_store:
                raise FuseOSError(errno.EROFS)
            return 0

        # Allow truncate on all writable .sys files
        writable_types = (
            'sys_rdepend_patch_version', 'sys_depend_patch_version',
            'sys_ruby_compat_patch_version', 'sys_ruby_compat_item',
            'sys_iuse_patch_version', 'sys_iuse_item',
            'sys_ebuild_append_patch_version', 'sys_ebuild_append_item',
            'sys_git_source_version', 'sys_git_source_patch_version',
            'sys_name_translation_entry',
        )
        if parsed['type'] in writable_types:
            return 0

        raise FuseOSError(errno.EROFS)

    def unlink(self, path):
        """Remove a file."""
        parsed = self._parse_path(path)

        if parsed['type'] == 'sys_slot_version':
            if not self.slot_store:
                raise FuseOSError(errno.EROFS)

            category = parsed['category']
            package = parsed['package']
            version = parsed['version']

            if self.slot_store.remove(category, package, version):
                self.slot_store.save()
                # Invalidate content cache for affected ebuilds
                self._invalidate_package_cache(category, package)
                return 0
            raise FuseOSError(errno.ENOENT)

        # Handle other .sys file removals
        return self._unlink_sys_file(parsed)

    def _unlink_sys_file(self, parsed: Dict) -> int:
        """Remove .sys virtual files."""
        ptype = parsed.get('type', '')
        category = parsed.get('category', '')
        package = parsed.get('package', '')
        version = parsed.get('version', '')

        # RDEPEND/DEPEND patch removal
        if ptype in ('sys_rdepend_patch_version', 'sys_depend_patch_version'):
            if not self.dep_patch_store:
                raise FuseOSError(errno.EROFS)
            dep_type = 'rdepend' if 'rdepend' in ptype else 'depend'
            # Clear all patches for this version
            self.dep_patch_store.clear_patches(category, package, version, dep_type)
            self.dep_patch_store.save()
            self._invalidate_package_cache(category, package)
            return 0

        # Ruby-compat patch removal
        if ptype == 'sys_ruby_compat_patch_version':
            if not self.ruby_compat_store:
                raise FuseOSError(errno.EROFS)
            self.ruby_compat_store.clear_patches(category, package, version)
            self.ruby_compat_store.save()
            self._invalidate_package_cache(category, package)
            return 0

        # Ruby-compat item removal
        if ptype == 'sys_ruby_compat_item':
            if not self.ruby_compat_store:
                raise FuseOSError(errno.EROFS)
            impl = parsed.get('item', '')
            self.ruby_compat_store.remove_impl(category, package, version, impl)
            self.ruby_compat_store.save()
            self._invalidate_package_cache(category, package)
            return 0

        # IUSE patch removal
        if ptype == 'sys_iuse_patch_version':
            if not self.iuse_patch_store:
                raise FuseOSError(errno.EROFS)
            self.iuse_patch_store.clear_patches(category, package, version)
            self.iuse_patch_store.save()
            self._invalidate_package_cache(category, package)
            return 0

        # IUSE item removal
        if ptype == 'sys_iuse_item':
            if not self.iuse_patch_store:
                raise FuseOSError(errno.EROFS)
            flag = parsed.get('item', '')
            self.iuse_patch_store.remove_flag(category, package, version, flag)
            self.iuse_patch_store.save()
            self._invalidate_package_cache(category, package)
            return 0

        # Ebuild append item removal
        if ptype == 'sys_ebuild_append_item':
            if not self.append_patch_store:
                raise FuseOSError(errno.EROFS)
            phase = parsed.get('item', '')
            self.append_patch_store.remove_phase(category, package, version, phase)
            self.append_patch_store.save()
            self._invalidate_package_cache(category, package)
            return 0

        # Ebuild append patch removal
        if ptype == 'sys_ebuild_append_patch_version':
            if not self.append_patch_store:
                raise FuseOSError(errno.EROFS)
            self.append_patch_store.clear_phases(category, package, version)
            self.append_patch_store.save()
            self._invalidate_package_cache(category, package)
            return 0

        # Git source removal
        if ptype in ('sys_git_source_version', 'sys_git_source_patch_version'):
            if not self.git_source_patch_store:
                raise FuseOSError(errno.EROFS)
            self.git_source_patch_store.remove_git_source(category, package, version)
            self.git_source_patch_store.save()
            self._invalidate_package_cache(category, package)
            return 0

        # Name translation removal
        if ptype == 'sys_name_translation_entry':
            if not self.name_translation_store:
                raise FuseOSError(errno.EROFS)
            pypi_name = parsed.get('pypi_name', '')
            self.name_translation_store.remove_mapping(pypi_name)
            self.name_translation_store.save()
            return 0

        raise FuseOSError(errno.EROFS)

    def mkdir(self, path, mode):
        """Create a directory."""
        parsed = self._parse_path(path)

        # Virtual .sys directories always exist - return EEXIST
        if parsed['type'] in ('sys_root', 'sys_slot', 'sys_slot_category', 'sys_slot_package'):
            raise FuseOSError(errno.EEXIST)

        # Don't allow creating other directories
        raise FuseOSError(errno.EROFS)

    def _invalidate_package_cache(self, category: str, package: str):
        """Invalidate content cache for a package's ebuilds."""
        # Remove all cached content for this package
        keys_to_remove = [
            key for key in self._content_cache
            if key.startswith(f"/{category}/{package}/")
        ]
        for key in keys_to_remove:
            del self._content_cache[key]

    def release(self, path, fh):
        """Release an open file."""
        return 0

    def statfs(self, path):
        """Get filesystem statistics."""
        return {
            'f_bsize': 4096,
            'f_frsize': 4096,
            'f_blocks': 1000000,
            'f_bfree': 500000,
            'f_bavail': 500000,
            'f_files': 1000000,
            'f_ffree': 500000,
            'f_favail': 500000,
            'f_flag': 0,
            'f_namemax': 255
        }

    def access(self, path, mode):
        """Check file access permissions."""
        parsed = self._parse_path(path)

        # Allow read access to everything
        if mode == os.R_OK:
            return 0

        # All .sys directory types
        sys_dir_types = (
            'sys_root', 'sys_slot', 'sys_slot_category', 'sys_slot_package',
            'sys_rdepend', 'sys_rdepend_category', 'sys_rdepend_package',
            'sys_rdepend_patch', 'sys_rdepend_patch_category', 'sys_rdepend_patch_package',
            'sys_depend', 'sys_depend_category', 'sys_depend_package',
            'sys_depend_patch', 'sys_depend_patch_category', 'sys_depend_patch_package',
            'sys_ruby_compat', 'sys_ruby_compat_category', 'sys_ruby_compat_package', 'sys_ruby_compat_version',
            'sys_ruby_compat_patch', 'sys_ruby_compat_patch_category', 'sys_ruby_compat_patch_package',
            'sys_iuse', 'sys_iuse_category', 'sys_iuse_package', 'sys_iuse_version',
            'sys_iuse_patch', 'sys_iuse_patch_category', 'sys_iuse_patch_package',
            'sys_ebuild_append', 'sys_ebuild_append_category', 'sys_ebuild_append_package', 'sys_ebuild_append_version',
            'sys_ebuild_append_patch', 'sys_ebuild_append_patch_category', 'sys_ebuild_append_patch_package',
            'sys_git_source', 'sys_git_source_category', 'sys_git_source_package',
            'sys_git_source_patch', 'sys_git_source_patch_category', 'sys_git_source_patch_package',
            'sys_name_translation',
        )

        # All .sys writable file types
        sys_file_types = (
            'sys_slot_version',
            'sys_rdepend_patch_version', 'sys_depend_patch_version',
            'sys_ruby_compat_patch_version', 'sys_ruby_compat_item',
            'sys_iuse_patch_version', 'sys_iuse_item',
            'sys_ebuild_append_patch_version', 'sys_ebuild_append_item',
            'sys_git_source_version', 'sys_git_source_patch_version',
            'sys_name_translation_entry',
        )

        # Allow execute on directories
        if mode == os.X_OK:
            if parsed['type'] in ('root', 'profiles', 'metadata', 'eclass', 'category', 'package') + sys_dir_types:
                return 0

        # Allow write access to .sys paths (directories and files)
        if mode == os.W_OK:
            if not self.no_patches:
                if parsed['type'] in sys_dir_types + sys_file_types:
                    return 0
            raise FuseOSError(errno.EROFS)

        return 0


def mount_rubygems_filesystem(
    mountpoint: str,
    foreground: bool = False,
    debug: bool = False,
    cache_ttl: int = 3600,
    cache_dir: Optional[str] = None,
    filter_config: Optional[Dict] = None,
    use_ruby: Optional[List[str]] = None,
    patch_file: Optional[str] = None,
    no_patches: bool = False
):
    """
    Mount the RubyGems FUSE filesystem.

    Args:
        mountpoint: Path where the filesystem should be mounted
        foreground: Run in foreground instead of daemonizing
        debug: Enable debug output
        cache_ttl: Cache time-to-live in seconds (default: 1 hour)
        cache_dir: Cache directory for RubyGems metadata
        filter_config: Version filter configuration dictionary
        use_ruby: List of USE_RUBY flags (e.g., ['ruby32', 'ruby33'])
        patch_file: Path to patch file for slot/dependency overrides
        no_patches: If True, disable the patching system entirely
    """
    # Only configure logging if it hasn't been configured yet
    if not logging.getLogger().handlers:
        if debug:
            logging.basicConfig(level=logging.DEBUG)
        else:
            logging.basicConfig(level=logging.INFO)

    logger.info(f"Mounting RubyGems FUSE filesystem at {mountpoint}")
    fs = PortageGemFS(
        cache_ttl=cache_ttl,
        cache_dir=cache_dir,
        filter_config=filter_config,
        mount_point=mountpoint,
        use_ruby=use_ruby,
        patch_file=patch_file,
        no_patches=no_patches
    )

    FUSE(fs, mountpoint, nothreads=False, foreground=foreground, debug=debug, allow_other=True,
         entry_timeout=0, attr_timeout=0, negative_timeout=0)
