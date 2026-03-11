"""
Name translation between RubyGems and Gentoo package names.

This module provides bidirectional translation between RubyGems gem names
and Gentoo dev-ruby package names.

Design principles:
- Use exact gem names by default (no heuristic matching)
- Only apply explicit mappings from KNOWN_MAPPINGS or Gentoo metadata.xml
- Minimal transformations for PMS compatibility (trailing digits)
- For mismatches, use the .sys patching mechanism to configure mappings

Transformations applied:
- Lowercase normalization
- Trailing digit separator removal (iso-639 -> iso639) for PMS compatibility
- Underscores preserved (valid per PMS 3.1.2)

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import logging
import os
import re
from pathlib import Path
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


class RubyGemsNameTranslator:
    """
    Translator for RubyGems <-> Gentoo package names.

    This class handles bidirectional translation between gem names and
    Gentoo dev-ruby package names.

    Examples:
        >>> translator = RubyGemsNameTranslator()
        >>> translator.rubygems_to_gentoo('active_support')
        'activesupport'
        >>> translator.gentoo_to_rubygems('activesupport')
        'activesupport'
    """

    # Known mappings where gem name differs from Gentoo name
    # Format: gem_name -> gentoo_name
    KNOWN_MAPPINGS = {
        # Rails ecosystem
        'actioncable': 'actioncable',
        'actionmailbox': 'actionmailbox',
        'actionmailer': 'actionmailer',
        'actionpack': 'actionpack',
        'actiontext': 'actiontext',
        'actionview': 'actionview',
        'activejob': 'activejob',
        'activemodel': 'activemodel',
        'activerecord': 'activerecord',
        'activestorage': 'activestorage',
        'activesupport': 'activesupport',
        'active_support': 'activesupport',
        'active_record': 'activerecord',
        'active_model': 'activemodel',

        # Common gems with different names
        'pg': 'pg',
        'nokogiri': 'nokogiri',
        'json': 'json',
        'rake': 'rake',
        'bundler': 'bundler',
        'rspec': 'rspec',
        'rspec-core': 'rspec-core',
        'rspec-expectations': 'rspec-expectations',
        'rspec-mocks': 'rspec-mocks',
        'rspec-support': 'rspec-support',
        'puma': 'puma',
        'sidekiq': 'sidekiq',
        'redis': 'redis',

        # Gems that map differently
        'i18n': 'i18n',
        'mail': 'mail',
        'mime-types': 'mime-types',
        'mime-types-data': 'mime-types-data',
        'msgpack': 'msgpack',
        'ffi': 'ffi',
        'nio4r': 'nio4r',
        'websocket-driver': 'websocket-driver',
        'websocket-extensions': 'websocket-extensions',

        # Gems with trailing numbers (conflict with Gentoo version parsing)
        'iso-639': 'iso639',
        'oauth2': 'oauth2',
        'net-http2': 'net-http2',
    }

    # Reverse mappings (gentoo -> gem)
    # When multiple gems map to the same Gentoo name, prefer canonical names
    # (where gem_name == gentoo_name) to avoid issues like:
    # 'activemodel' -> 'active_model' when it should be 'activemodel' -> 'activemodel'
    @classmethod
    def _build_reverse_mappings(cls) -> Dict[str, str]:
        result = {}
        for gem, gentoo in cls.KNOWN_MAPPINGS.items():
            # Prefer canonical mappings (gem == gentoo) over aliases
            if gentoo not in result or gem == gentoo:
                result[gentoo] = gem
        return result

    REVERSE_MAPPINGS: Dict[str, str] = {}  # Will be populated after class definition

    def __init__(self, preload_gentoo: bool = True):
        """
        Initialize the translator.

        Args:
            preload_gentoo: If True, scan Gentoo repos for existing packages
        """
        self._gentoo_packages: Set[str] = set()
        self._gem_to_gentoo: Dict[str, str] = dict(self.KNOWN_MAPPINGS)
        self._gentoo_to_gem: Dict[str, str] = dict(self.REVERSE_MAPPINGS)

        if preload_gentoo:
            self._preload_gentoo_packages()

    def _preload_gentoo_packages(self):
        """Scan Gentoo repositories for existing dev-ruby packages."""
        repo_paths = [
            Path('/var/db/repos/gentoo/dev-ruby'),
            Path('/var/db/repos/ruby-overlay/dev-ruby'),
        ]

        for repo_path in repo_paths:
            if repo_path.exists():
                for pkg_dir in repo_path.iterdir():
                    if pkg_dir.is_dir() and not pkg_dir.name.startswith('.'):
                        gentoo_name = pkg_dir.name
                        self._gentoo_packages.add(gentoo_name)

                        # Try to determine the gem name from metadata.xml
                        metadata_xml = pkg_dir / 'metadata.xml'
                        if metadata_xml.exists():
                            gem_name = self._extract_gem_name_from_metadata(metadata_xml)
                            if gem_name and gem_name != gentoo_name:
                                self._gem_to_gentoo[gem_name] = gentoo_name
                                self._gentoo_to_gem[gentoo_name] = gem_name

        logger.debug(f"Loaded {len(self._gentoo_packages)} existing dev-ruby packages")

    def _extract_gem_name_from_metadata(self, metadata_path: Path) -> Optional[str]:
        """Extract gem name from metadata.xml if present."""
        try:
            content = metadata_path.read_text()
            # Look for upstream remote-id type="rubygems"
            match = re.search(
                r'<remote-id\s+type="rubygems">([^<]+)</remote-id>',
                content
            )
            if match:
                return match.group(1).strip()
        except Exception:
            pass
        return None

    def rubygems_to_gentoo(self, gem_name: str) -> str:
        """
        Translate RubyGems package name to Gentoo package name.

        Uses exact gem names by default. Explicit mappings from KNOWN_MAPPINGS
        or extracted from Gentoo metadata.xml are used when available.
        For mismatches, use the .sys patching mechanism to configure mappings.

        Args:
            gem_name: RubyGems gem name

        Returns:
            Gentoo package name (without category)

        Examples:
            >>> translator = RubyGemsNameTranslator(preload_gentoo=False)
            >>> translator.rubygems_to_gentoo('active_support')
            'activesupport'
            >>> translator.rubygems_to_gentoo('rspec-core')
            'rspec-core'
            >>> translator.rubygems_to_gentoo('my_new_gem')
            'my_new_gem'
            >>> translator.rubygems_to_gentoo('ruby-debug')
            'ruby-debug'
        """
        # Normalize input
        gem_name = gem_name.strip().lower()

        # Check known mappings first (from KNOWN_MAPPINGS or Gentoo metadata.xml)
        if gem_name in self._gem_to_gentoo:
            return self._gem_to_gentoo[gem_name]

        # Apply minimal translation rules (lowercase, fix PMS-incompatible names)
        gentoo_name = self._apply_translation_rules(gem_name)

        return gentoo_name

    def gentoo_to_rubygems(self, gentoo_name: str, hint: Optional[str] = None) -> str:
        """
        Translate Gentoo package name to RubyGems name.

        Args:
            gentoo_name: Gentoo package name (without category)
            hint: Optional hint for disambiguation

        Returns:
            RubyGems gem name

        Examples:
            >>> translator = RubyGemsNameTranslator(preload_gentoo=False)
            >>> translator.gentoo_to_rubygems('activesupport')
            'activesupport'
            >>> translator.gentoo_to_rubygems('rspec-core')
            'rspec-core'
        """
        gentoo_name = gentoo_name.strip().lower()

        # Check known mappings first
        if gentoo_name in self._gentoo_to_gem:
            return self._gentoo_to_gem[gentoo_name]

        # Most gems use the same name as Gentoo (with hyphens)
        # Only a few legacy gems use underscores instead of hyphens
        # Return the name as-is - it's more likely to be correct
        return gentoo_name

    def _apply_translation_rules(self, gem_name: str) -> str:
        """
        Apply standard translation rules to convert gem name to Gentoo name.

        Rules:
        1. Lowercase
        2. Preserve underscores (valid in Gentoo names per PMS 3.1.2)
        3. Remove leading/trailing hyphens or underscores
        4. Fix names ending with hyphen-digits (e.g., iso-639 -> iso639)
           Only hyphens are problematic as they look like version suffixes.
           Underscores before digits are fine (e.g., rubocop-ruby3_2 stays as-is).

        Note: Underscores are preserved to distinguish gems like:
        - devise-secure_password (underscore)
        - devise-secure-password (hyphen)
        These are different gems and should remain distinguishable.
        """
        import re

        name = gem_name.lower()

        # Remove any leading/trailing hyphens or underscores
        name = name.strip('-_')

        # Remove duplicate hyphens (but preserve single underscores)
        while '--' in name:
            name = name.replace('--', '-')

        # Fix names that end with hyphen-digits (e.g., iso-639 -> iso639)
        # These conflict with Gentoo's version parsing (looks like a version suffix)
        # Only hyphens are problematic - underscores are valid per PMS 3.1.2
        # and don't conflict with version parsing (e.g., rubocop-ruby3_2 is fine)
        match = re.search(r'-(\d+)$', name)
        if match:
            # Remove the hyphen before the trailing digits
            name = name[:match.start()] + match.group(1)

        return name

    def is_valid_gem_name(self, name: str) -> bool:
        """
        Check if a string is a valid gem name.

        Gem names must:
        - Start with a letter or underscore
        - Contain only letters, digits, underscores, and hyphens
        - Not be empty
        """
        if not name:
            return False
        return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_-]*$', name))

    def is_valid_gentoo_name(self, name: str) -> bool:
        """
        Check if a string is a valid Gentoo package name.

        Gentoo package names must:
        - Start with a lowercase letter
        - Contain only lowercase letters, digits, hyphens, and plus signs
        - Not be empty
        """
        if not name:
            return False
        return bool(re.match(r'^[a-z][a-z0-9+-]*$', name))


# Populate REVERSE_MAPPINGS after class definition
RubyGemsNameTranslator.REVERSE_MAPPINGS = RubyGemsNameTranslator._build_reverse_mappings()


class CachedRubyGemsTranslator(RubyGemsNameTranslator):
    """
    Cached version of the RubyGems name translator.

    This version maintains caches for forward and reverse translations,
    improving performance for repeated lookups.
    """

    def __init__(self, preload_gentoo: bool = True):
        """Initialize with caches."""
        super().__init__(preload_gentoo=preload_gentoo)
        self._forward_cache: Dict[str, str] = {}
        self._reverse_cache: Dict[str, str] = {}

    def rubygems_to_gentoo(self, gem_name: str) -> str:
        """Translate with caching."""
        gem_name = gem_name.strip().lower()
        if gem_name in self._forward_cache:
            return self._forward_cache[gem_name]

        result = super().rubygems_to_gentoo(gem_name)
        self._forward_cache[gem_name] = result
        return result

    def gentoo_to_rubygems(self, gentoo_name: str, hint: Optional[str] = None) -> str:
        """Translate with caching."""
        gentoo_name = gentoo_name.strip().lower()
        cache_key = f"{gentoo_name}:{hint}" if hint else gentoo_name
        if cache_key in self._reverse_cache:
            return self._reverse_cache[cache_key]

        result = super().gentoo_to_rubygems(gentoo_name, hint)
        self._reverse_cache[cache_key] = result
        return result


def create_rubygems_translator() -> RubyGemsNameTranslator:
    """
    Create a pre-configured RubyGems name translator.

    Returns:
        Configured translator instance with Gentoo package data loaded
    """
    return CachedRubyGemsTranslator(preload_gentoo=True)
