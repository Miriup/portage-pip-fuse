"""
Dynamic Ruby target detection for Gentoo.

This module provides utilities for detecting available Ruby implementations
from Gentoo's ruby-utils.eclass and system Portage configuration.

Similar to the PyPI ecosystem's PYTHON_TARGETS handling.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Cache TTL in seconds (1 hour, consistent with PyPI)
_CACHE_TTL = 3600

# Module-level cache
_cache: Dict[str, tuple] = {}


class RubyTargetDetector:
    """
    Detects available Ruby implementations from Gentoo configuration.

    This class provides two main methods:
    - get_all_ruby_impls(): All valid Ruby implementations from ruby-utils.eclass
    - get_ruby_targets(): System RUBY_TARGETS from Portage configuration

    Results are cached at the class level with a 1-hour TTL.
    """

    # Default fallback values if detection fails
    DEFAULT_ALL_IMPLS = ['ruby32', 'ruby33', 'ruby34']
    DEFAULT_TARGETS = ['ruby32', 'ruby33']

    # Standard eclass paths
    ECLASS_PATHS = [
        '/var/db/repos/gentoo/eclass/ruby-utils.eclass',
        '/usr/portage/eclass/ruby-utils.eclass',
    ]

    @classmethod
    def get_all_ruby_impls(cls) -> List[str]:
        """
        Get all valid Ruby implementations from ruby-utils.eclass.

        This returns ALL implementations that Gentoo supports, used for
        generating USE_RUBY in ebuilds.

        Returns:
            List of Ruby implementation names (e.g., ['ruby32', 'ruby33', 'ruby34'])

        Examples:
            >>> impls = RubyTargetDetector.get_all_ruby_impls()
            >>> 'ruby32' in impls
            True
        """
        cache_key = 'all_ruby_impls'
        cached = cls._get_cached(cache_key)
        if cached is not None:
            return cached

        # Try sourcing eclass via bash
        result = cls._source_eclass_variable('RUBY_TARGETS_PREFERENCE')
        if result:
            impls = result.split()
            if impls:
                cls._set_cached(cache_key, impls)
                logger.info(f"Detected Ruby implementations from eclass: {impls}")
                return impls

        # Fallback: parse eclass file directly
        result = cls._parse_eclass_variable('RUBY_TARGETS_PREFERENCE')
        if result:
            impls = result.split()
            if impls:
                cls._set_cached(cache_key, impls)
                logger.info(f"Parsed Ruby implementations from eclass: {impls}")
                return impls

        # Last resort: use defaults
        logger.warning(
            f"Could not detect Ruby implementations from eclass, "
            f"using defaults: {cls.DEFAULT_ALL_IMPLS}"
        )
        cls._set_cached(cache_key, cls.DEFAULT_ALL_IMPLS)
        return cls.DEFAULT_ALL_IMPLS

    @classmethod
    def get_ruby_targets(cls) -> List[str]:
        """
        Get system RUBY_TARGETS from Portage configuration.

        This returns the Ruby implementations that the user has enabled,
        used for filtering gem versions by compatibility.

        Checks in order:
        1. RUBY_TARGETS environment variable
        2. Portage API (if available)
        3. /etc/portage/make.conf
        4. Profile defaults

        Returns:
            List of enabled Ruby target names (e.g., ['ruby32', 'ruby33'])

        Examples:
            >>> targets = RubyTargetDetector.get_ruby_targets()
            >>> isinstance(targets, list)
            True
        """
        cache_key = 'ruby_targets'
        cached = cls._get_cached(cache_key)
        if cached is not None:
            return cached

        # Check environment variable first
        env_targets = os.environ.get('RUBY_TARGETS', '')
        if env_targets:
            targets = env_targets.split()
            if targets:
                cls._set_cached(cache_key, targets)
                logger.debug(f"Using RUBY_TARGETS from environment: {targets}")
                return targets

        # Try Portage API
        targets = cls._get_targets_from_portage()
        if targets:
            cls._set_cached(cache_key, targets)
            logger.debug(f"Detected RUBY_TARGETS from Portage API: {targets}")
            return targets

        # Try make.conf
        targets = cls._get_targets_from_make_conf()
        if targets:
            cls._set_cached(cache_key, targets)
            logger.debug(f"Parsed RUBY_TARGETS from make.conf: {targets}")
            return targets

        # Try profile defaults (via emerge --info)
        targets = cls._get_targets_from_emerge_info()
        if targets:
            cls._set_cached(cache_key, targets)
            logger.debug(f"Detected RUBY_TARGETS from emerge --info: {targets}")
            return targets

        # Fallback to defaults
        logger.warning(
            f"Could not detect RUBY_TARGETS, using defaults: {cls.DEFAULT_TARGETS}"
        )
        cls._set_cached(cache_key, cls.DEFAULT_TARGETS)
        return cls.DEFAULT_TARGETS

    @classmethod
    def ruby_impl_to_version(cls, impl: str) -> Optional[str]:
        """
        Convert a Ruby implementation name to a version string.

        Args:
            impl: Ruby implementation name (e.g., 'ruby34')

        Returns:
            Version string (e.g., '3.4') or None if invalid format

        Examples:
            >>> RubyTargetDetector.ruby_impl_to_version('ruby34')
            '3.4'
            >>> RubyTargetDetector.ruby_impl_to_version('ruby32')
            '3.2'
            >>> RubyTargetDetector.ruby_impl_to_version('ruby40')
            '4.0'
            >>> RubyTargetDetector.ruby_impl_to_version('invalid')
        """
        if not impl or not impl.startswith('ruby'):
            return None

        ver_part = impl[4:]  # Remove 'ruby' prefix
        if len(ver_part) < 2 or not ver_part.isdigit():
            return None

        major = ver_part[0]
        minor = ver_part[1:]

        return f"{major}.{minor}"

    @classmethod
    def version_to_ruby_impl(cls, version: str) -> Optional[str]:
        """
        Convert a Ruby version string to implementation name.

        Args:
            version: Ruby version string (e.g., '3.4')

        Returns:
            Implementation name (e.g., 'ruby34') or None if invalid

        Examples:
            >>> RubyTargetDetector.version_to_ruby_impl('3.4')
            'ruby34'
            >>> RubyTargetDetector.version_to_ruby_impl('3.2.0')
            'ruby32'
        """
        if not version:
            return None

        parts = version.split('.')
        if len(parts) < 2:
            return None

        try:
            major = int(parts[0])
            minor = int(parts[1])
            return f"ruby{major}{minor}"
        except ValueError:
            return None

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the detection cache."""
        global _cache
        _cache.clear()
        logger.debug("Ruby target detection cache cleared")

    # --- Private methods ---

    @classmethod
    def _get_cached(cls, key: str) -> Optional[List[str]]:
        """Get value from cache if not expired."""
        global _cache
        if key in _cache:
            value, timestamp = _cache[key]
            if time.time() - timestamp < _CACHE_TTL:
                return value
            del _cache[key]
        return None

    @classmethod
    def _set_cached(cls, key: str, value: List[str]) -> None:
        """Store value in cache."""
        global _cache
        _cache[key] = (value, time.time())

    @classmethod
    def _find_eclass_path(cls) -> Optional[Path]:
        """Find the ruby-utils.eclass file."""
        for path_str in cls.ECLASS_PATHS:
            path = Path(path_str)
            if path.exists():
                return path

        # Check PORTDIR
        portdir = os.environ.get('PORTDIR', '')
        if portdir:
            path = Path(portdir) / 'eclass' / 'ruby-utils.eclass'
            if path.exists():
                return path

        return None

    @classmethod
    def _source_eclass_variable(cls, var_name: str) -> Optional[str]:
        """Source eclass via bash and extract variable value."""
        eclass_path = cls._find_eclass_path()
        if not eclass_path:
            return None

        try:
            # Source the eclass and print the variable
            # We need to set EAPI to avoid the die call
            cmd = [
                'bash', '-c',
                f'EAPI=8; source "{eclass_path}" 2>/dev/null; echo "${var_name}"'
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
            logger.debug(f"Failed to source eclass: {e}")

        return None

    @classmethod
    def _parse_eclass_variable(cls, var_name: str) -> Optional[str]:
        """Parse eclass file to extract variable value."""
        eclass_path = cls._find_eclass_path()
        if not eclass_path:
            return None

        try:
            content = eclass_path.read_text()

            # Look for assignments like:
            # RUBY_TARGETS_PREFERENCE="ruby32 ruby33 "
            # RUBY_TARGETS_PREFERENCE+="ruby34 ruby40"

            # Pattern for initial assignment
            pattern = re.compile(
                rf'^{var_name}\s*=\s*["\']?([^"\']*)["\']?',
                re.MULTILINE
            )
            # Pattern for += append
            append_pattern = re.compile(
                rf'^{var_name}\s*\+=\s*["\']?([^"\']*)["\']?',
                re.MULTILINE
            )

            values = []

            # Get initial value
            match = pattern.search(content)
            if match:
                values.append(match.group(1).strip())

            # Get appended values
            for match in append_pattern.finditer(content):
                values.append(match.group(1).strip())

            if values:
                return ' '.join(values)

        except OSError as e:
            logger.debug(f"Failed to read eclass file: {e}")

        return None

    @classmethod
    def _get_targets_from_portage(cls) -> Optional[List[str]]:
        """Get RUBY_TARGETS using Portage API."""
        try:
            import portage
            settings = portage.settings
            ruby_targets = settings.get('RUBY_TARGETS', '')
            if ruby_targets:
                return ruby_targets.split()
        except ImportError:
            logger.debug("Portage API not available")
        except Exception as e:
            logger.debug(f"Error accessing Portage settings: {e}")

        return None

    @classmethod
    def _get_targets_from_make_conf(cls) -> Optional[List[str]]:
        """Parse RUBY_TARGETS from make.conf."""
        make_conf_paths = [
            '/etc/portage/make.conf',
            '/etc/make.conf',
        ]

        for conf_path in make_conf_paths:
            try:
                path = Path(conf_path)
                if not path.exists():
                    continue

                content = path.read_text()

                # Match RUBY_TARGETS="..." or RUBY_TARGETS='...'
                match = re.search(
                    r'^RUBY_TARGETS\s*=\s*["\']?([^"\']+)["\']?',
                    content,
                    re.MULTILINE
                )
                if match:
                    return match.group(1).split()

            except OSError:
                continue

        return None

    @classmethod
    def _get_targets_from_emerge_info(cls) -> Optional[List[str]]:
        """Get RUBY_TARGETS from emerge --info."""
        try:
            result = subprocess.run(
                ['emerge', '--info'],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                match = re.search(
                    r'^RUBY_TARGETS\s*=\s*"([^"]*)"',
                    result.stdout,
                    re.MULTILINE
                )
                if match:
                    return match.group(1).split()

        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError) as e:
            logger.debug(f"Failed to run emerge --info: {e}")

        return None


# Convenience functions for module-level access
def get_all_ruby_impls() -> List[str]:
    """
    Get all valid Ruby implementations from ruby-utils.eclass.

    This is a convenience wrapper around RubyTargetDetector.get_all_ruby_impls().

    Returns:
        List of Ruby implementation names (e.g., ['ruby32', 'ruby33', 'ruby34'])
    """
    return RubyTargetDetector.get_all_ruby_impls()


def get_ruby_targets() -> List[str]:
    """
    Get system RUBY_TARGETS from Portage configuration.

    This is a convenience wrapper around RubyTargetDetector.get_ruby_targets().

    Returns:
        List of enabled Ruby target names (e.g., ['ruby32', 'ruby33'])
    """
    return RubyTargetDetector.get_ruby_targets()


def ruby_impl_to_version(impl: str) -> Optional[str]:
    """
    Convert a Ruby implementation name to a version string.

    This is a convenience wrapper around RubyTargetDetector.ruby_impl_to_version().

    Args:
        impl: Ruby implementation name (e.g., 'ruby34')

    Returns:
        Version string (e.g., '3.4') or None if invalid format
    """
    return RubyTargetDetector.ruby_impl_to_version(impl)


def version_to_ruby_impl(version: str) -> Optional[str]:
    """
    Convert a Ruby version string to implementation name.

    This is a convenience wrapper around RubyTargetDetector.version_to_ruby_impl().

    Args:
        version: Ruby version string (e.g., '3.4')

    Returns:
        Implementation name (e.g., 'ruby34') or None if invalid
    """
    return RubyTargetDetector.version_to_ruby_impl(version)
