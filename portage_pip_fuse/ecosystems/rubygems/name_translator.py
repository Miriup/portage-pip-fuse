"""
Name translation between RubyGems and Gentoo package names.

This module provides bidirectional translation between RubyGems gem names
and Gentoo dev-ruby package names.

Naming conventions:
- Gems typically use underscores (active_support)
- Gentoo uses hyphens (activerecord -> activerecord, not active-record)
- Some gems have different names in Gentoo (e.g., 'rake' -> 'rake')

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
        'active_support'
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
    REVERSE_MAPPINGS = {v: k for k, v in KNOWN_MAPPINGS.items()}

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
            'my-new-gem'
        """
        # Normalize input
        gem_name = gem_name.strip().lower()

        # Check known mappings first
        if gem_name in self._gem_to_gentoo:
            return self._gem_to_gentoo[gem_name]

        # Apply standard translation rules
        gentoo_name = self._apply_translation_rules(gem_name)

        # Check if translated name exists in Gentoo
        if gentoo_name in self._gentoo_packages:
            return gentoo_name

        # Check alternative translations
        alternatives = self._generate_alternatives(gem_name)
        for alt in alternatives:
            if alt in self._gentoo_packages:
                # Cache this mapping for future use
                self._gem_to_gentoo[gem_name] = alt
                self._gentoo_to_gem[alt] = gem_name
                return alt

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
            'active_support'
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
        2. Replace underscores with hyphens (for most gems)
        3. Remove redundant hyphens
        4. Fix names ending with -NUMBER (would conflict with version parsing)
        """
        name = gem_name.lower()

        # Replace underscores with hyphens (standard Gentoo convention)
        name = name.replace('_', '-')

        # Remove any leading/trailing hyphens
        name = name.strip('-')

        # Remove duplicate hyphens
        while '--' in name:
            name = name.replace('--', '-')

        # Fix names that end with hyphen-digits (e.g., iso-639 -> iso639)
        # These conflict with Gentoo's version parsing
        # Pattern: name ends with -NNN where NNN is all digits
        import re
        match = re.search(r'-(\d+)$', name)
        if match:
            # Remove the hyphen before the trailing digits
            name = name[:match.start()] + match.group(1)

        return name

    def _generate_alternatives(self, gem_name: str) -> list:
        """Generate alternative Gentoo names to check."""
        alternatives = []
        name = gem_name.lower()

        # Try without underscores (joined)
        alternatives.append(name.replace('_', ''))

        # Try with hyphens
        alternatives.append(name.replace('_', '-'))

        # Try with underscores (some Gentoo packages keep them)
        alternatives.append(name)

        # Try common transformations
        if name.startswith('ruby-'):
            alternatives.append(name[5:])
        if not name.startswith('ruby-'):
            alternatives.append(f"ruby-{name}")

        return alternatives

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
