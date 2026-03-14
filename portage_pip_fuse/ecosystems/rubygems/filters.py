"""
Version and package filters for RubyGems.

This module provides filters to select which gem versions and packages
are visible in the FUSE filesystem.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class RubyCompatFilter:
    """
    Filter gem versions by Ruby version compatibility.

    This filter checks the required_ruby_version from gem metadata
    against the system's USE_RUBY settings.

    Similar to Python's PYTHON_COMPAT filtering.

    Uses dynamic detection from ruby_targets module to get system
    RUBY_TARGETS from Portage configuration.
    """

    def __init__(self, use_ruby: Optional[List[str]] = None):
        """
        Initialize the Ruby compatibility filter.

        Args:
            use_ruby: List of USE_RUBY flags (e.g., ['ruby32', 'ruby33'])
                     If None, detects from system RUBY_TARGETS
        """
        from .ruby_targets import get_ruby_targets, ruby_impl_to_version

        self.use_ruby = use_ruby or get_ruby_targets()
        self._ruby_versions = [
            ruby_impl_to_version(r) for r in self.use_ruby
            if ruby_impl_to_version(r) is not None
        ]
        logger.debug(f"Ruby compat filter using: {self._ruby_versions}")

    @classmethod
    def get_filter_name(cls) -> str:
        """Get the filter name for registry."""
        return "ruby-compat"

    def get_description(self) -> str:
        """Get human-readable description."""
        return f"Filters gems by Ruby compatibility (USE_RUBY: {', '.join(self.use_ruby)})"

    def filter_versions(
        self,
        gem_name: str,
        versions_metadata: Dict[str, Dict]
    ) -> Dict[str, Dict]:
        """
        Filter versions to only those compatible with system Ruby.

        Args:
            gem_name: Name of the gem
            versions_metadata: Dict mapping version -> metadata

        Returns:
            Filtered dict of version -> metadata
        """
        filtered = {}

        for version, metadata in versions_metadata.items():
            if self.should_include_version(gem_name, version, metadata):
                filtered[version] = metadata

        return filtered

    def should_include_version(
        self,
        gem_name: str,
        version: str,
        metadata: Dict[str, Any]
    ) -> bool:
        """
        Check if a specific version is compatible with system Ruby.

        Args:
            gem_name: Name of the gem
            version: Version string
            metadata: Version metadata dict

        Returns:
            True if compatible with at least one system Ruby version
        """
        required_ruby = metadata.get('required_ruby_version', '')

        # No requirement means compatible with all
        if not required_ruby or required_ruby == '>= 0':
            return True

        # Check each system Ruby version
        for ruby_version in self._ruby_versions:
            if self._version_satisfies(ruby_version, required_ruby):
                return True

        return False

    def _version_satisfies(self, ruby_version: str, requirement: str) -> bool:
        """
        Check if a Ruby version satisfies a requirement.

        Args:
            ruby_version: Ruby version string (e.g., "3.2")
            requirement: Version requirement (e.g., ">= 2.7.0")

        Returns:
            True if ruby_version satisfies requirement
        """
        try:
            from packaging.specifiers import SpecifierSet
            from packaging.version import Version

            # Parse the requirement
            spec = SpecifierSet(requirement)
            return Version(ruby_version) in spec
        except Exception:
            # If we can't parse, assume compatible
            return True


class GemSourceFilter:
    """
    Filter gem versions by source availability.

    Filters to only include versions that have a .gem file available,
    or have a git repository as fallback.

    Similar to Python's source-dist filter.
    """

    def __init__(self, include_git: bool = True):
        """
        Initialize the source filter.

        Args:
            include_git: Whether to include gems with git sources
        """
        self.include_git = include_git

    @classmethod
    def get_filter_name(cls) -> str:
        """Get the filter name for registry."""
        return "gem-source"

    def get_description(self) -> str:
        """Get human-readable description."""
        if self.include_git:
            return "Filters gems to those with .gem files or git repositories"
        return "Filters gems to those with .gem files only"

    def filter_versions(
        self,
        gem_name: str,
        versions_metadata: Dict[str, Dict]
    ) -> Dict[str, Dict]:
        """
        Filter versions to only those with available sources.

        Args:
            gem_name: Name of the gem
            versions_metadata: Dict mapping version -> metadata

        Returns:
            Filtered dict of version -> metadata
        """
        filtered = {}

        for version, metadata in versions_metadata.items():
            if self.should_include_version(gem_name, version, metadata):
                filtered[version] = metadata

        return filtered

    def should_include_version(
        self,
        gem_name: str,
        version: str,
        metadata: Dict[str, Any]
    ) -> bool:
        """
        Check if a specific version has sources available.

        Args:
            gem_name: Name of the gem
            version: Version string
            metadata: Version metadata dict

        Returns:
            True if sources are available
        """
        # Check for .gem file (default assumption for RubyGems)
        gem_uri = metadata.get('gem_uri')
        if gem_uri:
            return True

        # Check for yanked status
        if metadata.get('yanked', False):
            return False

        # If include_git, check for git source
        if self.include_git:
            source_code_uri = metadata.get('source_code_uri', '')
            if source_code_uri:
                return True

        # Assume .gem is available if not explicitly marked unavailable
        return True


class PlatformFilter:
    """
    Filter gem versions by platform.

    Filters out platform-specific gems (like java, mswin) that won't
    work on Gentoo Linux.
    """

    # Platforms compatible with Gentoo Linux
    COMPATIBLE_PLATFORMS = {'ruby', '', 'linux', 'linux-gnu'}

    # Platforms that are NOT compatible
    INCOMPATIBLE_PLATFORMS = {'java', 'jruby', 'mswin', 'mingw', 'x64-mingw', 'darwin'}

    @classmethod
    def get_filter_name(cls) -> str:
        """Get the filter name for registry."""
        return "platform"

    def get_description(self) -> str:
        """Get human-readable description."""
        return "Filters gems to Linux-compatible platforms only"

    def filter_versions(
        self,
        gem_name: str,
        versions_metadata: Dict[str, Dict]
    ) -> Dict[str, Dict]:
        """
        Filter versions to Linux-compatible platforms.

        Args:
            gem_name: Name of the gem
            versions_metadata: Dict mapping version -> metadata

        Returns:
            Filtered dict of version -> metadata
        """
        filtered = {}

        for version, metadata in versions_metadata.items():
            if self.should_include_version(gem_name, version, metadata):
                filtered[version] = metadata

        return filtered

    def should_include_version(
        self,
        gem_name: str,
        version: str,
        metadata: Dict[str, Any]
    ) -> bool:
        """
        Check if a specific version is platform-compatible.

        Args:
            gem_name: Name of the gem
            version: Version string
            metadata: Version metadata dict

        Returns:
            True if compatible with Linux
        """
        platform = metadata.get('platform', 'ruby')

        if not platform:
            return True

        platform = platform.lower()

        # Check for incompatible platforms
        for incompatible in self.INCOMPATIBLE_PLATFORMS:
            if incompatible in platform:
                return False

        # Check for compatible platforms
        if platform in self.COMPATIBLE_PLATFORMS:
            return True

        # Unknown platform - include it (might work)
        return True


class PreReleaseFilter:
    """
    Filter to include/exclude pre-release versions.

    By default, excludes pre-release versions (alpha, beta, rc, pre).
    """

    # Patterns that indicate pre-release
    PRE_RELEASE_PATTERNS = [
        r'\.alpha\d*$',
        r'\.beta\d*$',
        r'\.pre\d*$',
        r'\.rc\d*$',
        r'-alpha\d*$',
        r'-beta\d*$',
        r'-pre\d*$',
        r'-rc\d*$',
    ]

    def __init__(self, include_pre: bool = False):
        """
        Initialize the pre-release filter.

        Args:
            include_pre: If True, include pre-release versions
        """
        self.include_pre = include_pre
        self._patterns = [re.compile(p, re.IGNORECASE) for p in self.PRE_RELEASE_PATTERNS]

    @classmethod
    def get_filter_name(cls) -> str:
        """Get the filter name for registry."""
        return "pre-release"

    def get_description(self) -> str:
        """Get human-readable description."""
        if self.include_pre:
            return "Includes all versions (including pre-releases)"
        return "Excludes pre-release versions"

    def filter_versions(
        self,
        gem_name: str,
        versions_metadata: Dict[str, Dict]
    ) -> Dict[str, Dict]:
        """
        Filter pre-release versions.

        Args:
            gem_name: Name of the gem
            versions_metadata: Dict mapping version -> metadata

        Returns:
            Filtered dict of version -> metadata
        """
        if self.include_pre:
            return versions_metadata

        filtered = {}

        for version, metadata in versions_metadata.items():
            if not self._is_pre_release(version, metadata):
                filtered[version] = metadata

        return filtered

    def should_include_version(
        self,
        gem_name: str,
        version: str,
        metadata: Dict[str, Any]
    ) -> bool:
        """Check if version should be included."""
        if self.include_pre:
            return True
        return not self._is_pre_release(version, metadata)

    def _is_pre_release(self, version: str, metadata: Dict[str, Any]) -> bool:
        """Check if a version is a pre-release."""
        # Check prerelease flag in metadata
        if metadata.get('prerelease', False):
            return True

        # Check version string patterns
        for pattern in self._patterns:
            if pattern.search(version):
                return True

        return False


class VersionFilterChain:
    """
    Chain of version filters that applies all filters in sequence.

    A version must pass ALL filters to be included (AND logic).
    """

    def __init__(self, filters: List[Any]):
        """
        Initialize the filter chain.

        Args:
            filters: List of filter instances
        """
        self.filters = filters

    def filter_versions(
        self,
        gem_name: str,
        versions_metadata: Dict[str, Dict]
    ) -> Dict[str, Dict]:
        """
        Apply all filters to versions.

        Args:
            gem_name: Name of the gem
            versions_metadata: Dict mapping version -> metadata

        Returns:
            Filtered dict of version -> metadata
        """
        result = versions_metadata

        for f in self.filters:
            result = f.filter_versions(gem_name, result)
            if not result:
                break

        return result

    def should_include_version(
        self,
        gem_name: str,
        version: str,
        metadata: Dict[str, Any]
    ) -> bool:
        """Check if version passes all filters."""
        for f in self.filters:
            if not f.should_include_version(gem_name, version, metadata):
                return False
        return True

    def get_description(self) -> str:
        """Get combined description."""
        descs = [f.get_description() for f in self.filters]
        return " AND ".join(descs)


# Filter registry
class RubyVersionFilterRegistry:
    """Registry for Ruby version filters."""

    _filters: Dict[str, type] = {}

    @classmethod
    def register(cls, name: str, filter_class: type) -> None:
        """Register a filter class."""
        cls._filters[name] = filter_class

    @classmethod
    def get_filter_class(cls, name: str) -> Optional[type]:
        """Get a filter class by name."""
        return cls._filters.get(name)

    @classmethod
    def get_all_filters(cls) -> Dict[str, type]:
        """Get all registered filters."""
        return cls._filters.copy()


class GentooVersionFilter:
    """
    Filter gem versions to only those that can be translated to valid Gentoo PMS format.

    Gentoo PMS version format:
        version ::= [0-9]+(\\.[0-9]+)*[a-z]?((_alpha|_beta|_pre|_rc|_p)[0-9]*)*(-r[0-9]+)?

    This filter rejects versions with:
    - Non-standard suffixes (e.g., .racecar1, .RELEASE)
    - Invalid suffix combinations that can't be translated
    """

    # Standard Gentoo pre-release suffix names
    STANDARD_SUFFIXES = {'alpha', 'beta', 'pre', 'rc', 'p'}

    # Ruby shorthand -> Gentoo suffix (e.g., 5.a -> 5_alpha)
    SHORTHAND_MAP = {'a': 'alpha', 'b': 'beta'}

    @classmethod
    def get_filter_name(cls) -> str:
        """Get the filter name for registry."""
        return "gentoo-version"

    def get_description(self) -> str:
        """Get human-readable description."""
        return "Filters gems to those with Gentoo-compatible version strings"

    def filter_versions(
        self,
        gem_name: str,
        versions_metadata: Dict[str, Dict]
    ) -> Dict[str, Dict]:
        """
        Filter versions to only Gentoo-compatible ones.

        Args:
            gem_name: Name of the gem
            versions_metadata: Dict mapping version -> metadata

        Returns:
            Filtered dict of version -> metadata
        """
        filtered = {}

        for version, metadata in versions_metadata.items():
            if self.should_include_version(gem_name, version, metadata):
                filtered[version] = metadata

        return filtered

    def should_include_version(
        self,
        gem_name: str,
        version: str,
        metadata: Dict[str, Any]
    ) -> bool:
        """
        Check if a version can be translated to valid Gentoo format.

        Args:
            gem_name: Name of the gem
            version: Version string
            metadata: Version metadata dict

        Returns:
            True if version is Gentoo-compatible
        """
        return self._can_translate_version(version)

    def _can_translate_version(self, gem_version: str) -> bool:
        """
        Check if a gem version can be translated to valid Gentoo PMS format.

        Args:
            gem_version: Ruby gem version string

        Returns:
            True if the version can be translated to valid Gentoo format

        Examples:
            >>> f = GentooVersionFilter()
            >>> f._can_translate_version('1.0.0')
            True
            >>> f._can_translate_version('2.0.0.alpha1')
            True
            >>> f._can_translate_version('2.0.0.alpha.pre.4')
            True
            >>> f._can_translate_version('5.0.0.beta1.1')
            True
            >>> f._can_translate_version('5.a')
            True
            >>> f._can_translate_version('5.b')
            True
            >>> f._can_translate_version('5.a1')
            True
            >>> f._can_translate_version('5.0.0.racecar1')
            False
            >>> f._can_translate_version('1.0.0.RELEASE')
            False
        """
        # Split into base version (numbers.numbers...) and suffix
        match = re.match(r'^(\d+(?:\.\d+)*)(.*)$', gem_version)
        if not match:
            return False

        base, suffix = match.groups()

        if not suffix:
            return True  # Pure numeric version is always valid

        # Parse suffix components
        suffix = suffix.lstrip('.')
        if not suffix:
            return True

        components = suffix.split('.')

        i = 0
        while i < len(components):
            comp = components[i].lower()

            # Check for Ruby shorthand (a, b)
            if comp in self.SHORTHAND_MAP:
                i += 1
            elif comp in self.STANDARD_SUFFIXES:
                # Standard suffix - check if next component is a number
                if i + 1 < len(components) and components[i + 1].isdigit():
                    i += 2
                else:
                    i += 1
            elif comp.isdigit():
                # Standalone number - valid as patchlevel
                i += 1
            elif re.match(r'^([ab])(\d+)$', comp):
                # Shorthand with number (a1 -> alpha1, b2 -> beta2)
                i += 1
            elif re.match(r'^([a-z]+)(\d+)$', comp):
                # Combined suffix like 'alpha1', 'beta2'
                m = re.match(r'^([a-z]+)(\d+)$', comp)
                name = m.group(1)
                if name in self.STANDARD_SUFFIXES:
                    i += 1
                else:
                    # Non-standard suffix like 'racecar1'
                    return False
            else:
                # Non-standard suffix
                return False

        return True


# Register built-in filters
RubyVersionFilterRegistry.register('ruby-compat', RubyCompatFilter)
RubyVersionFilterRegistry.register('gem-source', GemSourceFilter)
RubyVersionFilterRegistry.register('platform', PlatformFilter)
RubyVersionFilterRegistry.register('pre-release', PreReleaseFilter)
RubyVersionFilterRegistry.register('gentoo-version', GentooVersionFilter)
