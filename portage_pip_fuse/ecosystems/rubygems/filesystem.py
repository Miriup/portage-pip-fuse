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
        use_ruby: Optional[List[str]] = None
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
        """
        self.root = root
        self.cache_ttl = cache_ttl
        self.mount_point = mount_point
        self.use_ruby = use_ruby or ['ruby32', 'ruby33']

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

        # Static overlay structure
        self.static_dirs = {
            "/",
            "/dev-ruby",
            "/profiles",
            "/metadata",
            "/eclass",
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

        # Platform filter (default: enabled)
        # Exclude java, mswin, etc.
        if 'platform' not in disabled_filters:
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
        """
        path = path.strip('/')
        if not path:
            return {'type': 'root'}

        parts = path.split('/')

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

    def _gentoo_to_gem(self, gentoo_name: str) -> Optional[str]:
        """
        Convert Gentoo package name to gem name.

        Since underscores are now preserved in Gentoo names, the translation
        is straightforward - the Gentoo name matches the gem name.
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
            versions_data = self.metadata_provider.get_package_versions(gem_name)
            if not versions_data:
                return []

            # Convert to dict format for filtering
            versions_metadata = {}
            for v in versions_data:
                if isinstance(v, dict):
                    version = v.get('number', v.get('version', ''))
                    if version:
                        versions_metadata[version] = v
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

    def _generate_ebuild(self, gentoo_name: str, gem_name: str, version: str) -> str:
        """Generate ebuild content for a gem package."""
        # Get gem version (translate back from Gentoo format)
        gem_version = self._gentoo_to_gem_version(version)

        # Get package metadata
        info = self._get_package_info(gem_name)

        if not info:
            # Minimal ebuild if no metadata available
            return self._generate_minimal_ebuild(gentoo_name, gem_name, version)

        # Use the ebuild generator
        return self.ebuild_generator.generate_ebuild(
            package_info=info,
            version=gem_version,
            gentoo_name=gentoo_name
        )

    def _generate_minimal_ebuild(self, gentoo_name: str, gem_name: str, version: str) -> str:
        """Generate minimal ebuild when metadata is unavailable."""
        gem_version = self._gentoo_to_gem_version(version)
        use_ruby = ' '.join(self.use_ruby)

        return f'''# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

USE_RUBY="{use_ruby}"
RUBY_FAKEGEM_RECIPE_TEST="none"
RUBY_FAKEGEM_RECIPE_DOC="none"

inherit ruby-fakegem

DESCRIPTION="{gem_name} gem"
HOMEPAGE="https://rubygems.org/gems/{gem_name}"
SRC_URI="https://rubygems.org/gems/{gem_name}-{gem_version}.gem"

LICENSE="MIT"
SLOT="0"
KEYWORDS="~amd64 ~arm64"
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

        # Current time for timestamps
        now = time.time()

        # Common attributes
        uid = os.getuid()
        gid = os.getgid()

        # Directory attributes
        if parsed['type'] in ('root', 'profiles', 'metadata', 'eclass', 'category', 'package'):
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

        raise FuseOSError(errno.ENOENT)

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
            entries.extend(['dev-ruby', 'profiles', 'metadata', 'eclass'])

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

                    # Convert gem names to Gentoo names
                    gentoo_packages = []
                    for gem_name in gem_names:
                        gentoo_name = self.name_translator.rubygems_to_gentoo(gem_name)
                        if gentoo_name:
                            gentoo_packages.append(gentoo_name)
                        else:
                            # Use gem name directly (normalized)
                            gentoo_packages.append(gem_name.lower().replace('_', '-'))

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
        content = self._get_file_content(path, parsed)

        if content is not None:
            return content[offset:offset + length]

        raise FuseOSError(errno.ENOENT)

    def open(self, path, flags):
        """Open a file."""
        # Check if file exists
        parsed = self._parse_path(path)

        if path in self.static_files:
            return 0

        if parsed['type'] in ('ebuild', 'package_metadata', 'manifest'):
            return 0

        raise FuseOSError(errno.ENOENT)

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

        # Allow execute on directories
        if mode == os.X_OK:
            if parsed['type'] in ('root', 'profiles', 'metadata', 'eclass', 'category', 'package'):
                return 0

        # Deny write access
        if mode == os.W_OK:
            raise FuseOSError(errno.EROFS)

        return 0


def mount_rubygems_filesystem(
    mountpoint: str,
    foreground: bool = False,
    debug: bool = False,
    cache_ttl: int = 3600,
    cache_dir: Optional[str] = None,
    filter_config: Optional[Dict] = None,
    use_ruby: Optional[List[str]] = None
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
        use_ruby=use_ruby
    )

    FUSE(fs, mountpoint, nothreads=False, foreground=foreground, debug=debug, allow_other=True)
