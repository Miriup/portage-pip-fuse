"""
Package name translator between PyPI and Gentoo naming conventions.

This module provides bidirectional translation between PyPI package names
and Gentoo's dev-python category package names, following the official
naming policies of both ecosystems.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import re
from abc import ABC, abstractmethod
from typing import Optional, Dict, Set, Tuple

# Import canonicalize_name from pip's vendored packaging if available,
# otherwise define our own implementation
try:
    from pip._vendor.packaging.utils import canonicalize_name as pip_canonicalize_name
except ImportError:
    # Fallback implementation based on PEP 503
    _canonicalize_regex = re.compile(r"[-_.]+")
    
    def pip_canonicalize_name(name: str) -> str:
        """Normalize a PyPI package name according to PEP 503."""
        return _canonicalize_regex.sub("-", name).lower()


class NameTranslatorBase(ABC):
    """
    Abstract base class for package name translators.
    
    This design allows for future extensions such as caching or
    preloading of name mappings.
    """
    
    @abstractmethod
    def pypi_to_gentoo(self, pypi_name: str) -> str:
        """
        Translate a PyPI package name to a Gentoo package name.
        
        Args:
            pypi_name: The PyPI package name to translate
            
        Returns:
            The corresponding Gentoo package name in dev-python category
        """
        pass
    
    @abstractmethod
    def gentoo_to_pypi(self, gentoo_name: str, hint: Optional[str] = None) -> str:
        """
        Translate a Gentoo package name back to a PyPI package name.
        
        Args:
            gentoo_name: The Gentoo package name (without dev-python/ prefix)
            hint: Optional hint about the original PyPI name for ambiguous cases
            
        Returns:
            The corresponding PyPI package name
        """
        pass
    
    @abstractmethod
    def is_valid_pypi_name(self, name: str) -> bool:
        """Check if a name is a valid PyPI package name."""
        pass
    
    @abstractmethod
    def is_valid_gentoo_name(self, name: str) -> bool:
        """Check if a name is a valid Gentoo package name."""
        pass


class SimpleNameTranslator(NameTranslatorBase):
    """
    Simple implementation of package name translation.
    
    This translator implements the Gentoo policy that all packages in dev-python/*
    that are published on PyPI must be named to match their respective PyPI names
    after PEP 503 normalization.
    
    Examples:
        >>> translator = SimpleNameTranslator()
        >>> translator.pypi_to_gentoo("Django")
        'django'
        >>> translator.pypi_to_gentoo("python-dateutil")
        'python-dateutil'
        >>> translator.pypi_to_gentoo("Pillow")
        'pillow'
        >>> translator.pypi_to_gentoo("SQLAlchemy")
        'sqlalchemy'
        >>> translator.pypi_to_gentoo("beautifulsoup4")
        'beautifulsoup4'
        >>> translator.pypi_to_gentoo("google.cloud.storage")
        'google-cloud-storage'
        >>> translator.pypi_to_gentoo("backports.zoneinfo")
        'backports-zoneinfo'
        >>> translator.pypi_to_gentoo("typing_extensions")
        'typing-extensions'
        >>> translator.pypi_to_gentoo("ruamel.yaml")
        'ruamel-yaml'
        >>> translator.pypi_to_gentoo("zope.interface")
        'zope-interface'
    """
    
    # Regex patterns for validation
    # PyPI: Letters, numbers, hyphens, underscores, and dots
    # Names can start/end with underscore (e.g., "_private") per PEP 508
    # Single-char names must be alphanumeric; multi-char can start/end with underscore
    _pypi_name_pattern = re.compile(r'^([a-zA-Z0-9]|[a-zA-Z0-9_][a-zA-Z0-9._-]*[a-zA-Z0-9_])$')
    
    # Gentoo: After normalization, only lowercase letters, numbers, and hyphens
    _gentoo_name_pattern = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')
    
    def __init__(self, strict_mode: bool = False):
        """
        Initialize the name translator.
        
        Args:
            strict_mode: If True, raise exceptions for invalid names.
                        If False, attempt best-effort translation.
        """
        self.strict_mode = strict_mode
        # Cache for reverse lookups (gentoo -> original pypi name)
        self._reverse_cache: Dict[str, Set[str]] = {}
    
    def pypi_to_gentoo(self, pypi_name: str) -> str:
        """
        Translate a PyPI package name to a Gentoo package name.
        
        Following Gentoo policy:
        1. Apply PEP 503 normalization (replace [-_.] runs with single hyphen)
        2. Convert to lowercase
        3. Keep the result as-is (no prefix/suffix additions)
        
        Examples:
            >>> translator = SimpleNameTranslator()
            >>> translator.pypi_to_gentoo("Flask-RESTful")
            'flask-restful'
            >>> translator.pypi_to_gentoo("Jinja2")
            'jinja2'
            >>> translator.pypi_to_gentoo("PyYAML")
            'pyyaml'
            >>> translator.pypi_to_gentoo("msgpack-python")
            'msgpack-python'
            >>> translator.pypi_to_gentoo("python-ldap")
            'python-ldap'
            >>> translator.pypi_to_gentoo("websocket_client")
            'websocket-client'
            >>> translator.pypi_to_gentoo("aiohttp")
            'aiohttp'
            >>> translator.pypi_to_gentoo("py.test")
            'py-test'
            >>> translator.pypi_to_gentoo("path.py")
            'path-py'
        """
        if self.strict_mode and not self.is_valid_pypi_name(pypi_name):
            raise ValueError(f"Invalid PyPI package name: {pypi_name}")
        
        # Use pip's canonicalize_name which implements PEP 503
        gentoo_name = pip_canonicalize_name(pypi_name)
        
        # Store the mapping for reverse lookup
        if gentoo_name not in self._reverse_cache:
            self._reverse_cache[gentoo_name] = set()
        self._reverse_cache[gentoo_name].add(pypi_name)
        
        return gentoo_name
    
    def gentoo_to_pypi(self, gentoo_name: str, hint: Optional[str] = None) -> str:
        """
        Translate a Gentoo package name back to a PyPI package name.
        
        This is inherently ambiguous since multiple PyPI names can map to the
        same Gentoo name (e.g., 'my-package', 'my_package', 'my.package').
        
        Args:
            gentoo_name: The Gentoo package name (without dev-python/ prefix)
            hint: Optional hint about the original PyPI name
            
        Returns:
            Best guess at the original PyPI package name
            
        Examples:
            >>> translator = SimpleNameTranslator()
            >>> # First populate cache with known mappings
            >>> _ = translator.pypi_to_gentoo("websocket-client")
            >>> _ = translator.pypi_to_gentoo("websocket_client")
            >>> # Now reverse translation will use cache
            >>> translator.gentoo_to_pypi("websocket-client", hint="websocket_client")
            'websocket_client'
            >>> # Without hint, returns one from cache or normalized form
            >>> result = translator.gentoo_to_pypi("websocket-client")
            >>> result in ['websocket-client', 'websocket_client']
            True
            >>> # Unknown packages return the normalized form
            >>> translator.gentoo_to_pypi("unknown-package")
            'unknown-package'
        """
        if self.strict_mode and not self.is_valid_gentoo_name(gentoo_name):
            raise ValueError(f"Invalid Gentoo package name: {gentoo_name}")
        
        # If a hint is provided and it normalizes to the gentoo name, use it
        if hint and pip_canonicalize_name(hint) == gentoo_name:
            return hint
        
        # Check cache for known mappings
        if gentoo_name in self._reverse_cache:
            candidates = self._reverse_cache[gentoo_name]
            if hint and hint in candidates:
                return hint
            # Return the first one (could be improved with heuristics)
            return next(iter(candidates))
        
        # Default: return the Gentoo name itself (it's a valid PyPI name)
        return gentoo_name
    
    def is_valid_pypi_name(self, name: str) -> bool:
        """
        Check if a name is a valid PyPI package name.
        
        According to PEP 508 and PyPI requirements.
        
        Examples:
            >>> translator = SimpleNameTranslator()
            >>> translator.is_valid_pypi_name("Django")
            True
            >>> translator.is_valid_pypi_name("python-dateutil")
            True
            >>> translator.is_valid_pypi_name("backports.zoneinfo")
            True
            >>> translator.is_valid_pypi_name("_private")
            True
            >>> translator.is_valid_pypi_name("my-pkg_2.0")
            True
            >>> translator.is_valid_pypi_name("")
            False
            >>> translator.is_valid_pypi_name("my--pkg")  # Multiple consecutive hyphens allowed
            True
            >>> translator.is_valid_pypi_name("-startwithhyphen")
            False
            >>> translator.is_valid_pypi_name("endwithhyphen-")
            False
        """
        if not name:
            return False
        return bool(self._pypi_name_pattern.match(name))
    
    def is_valid_gentoo_name(self, name: str) -> bool:
        """
        Check if a name is a valid Gentoo package name.
        
        After normalization, must be lowercase with hyphens.
        
        Examples:
            >>> translator = SimpleNameTranslator()
            >>> translator.is_valid_gentoo_name("django")
            True
            >>> translator.is_valid_gentoo_name("python-dateutil")
            True
            >>> translator.is_valid_gentoo_name("backports-zoneinfo")
            True
            >>> translator.is_valid_gentoo_name("zope-interface")
            True
            >>> translator.is_valid_gentoo_name("Django")  # Must be lowercase
            False
            >>> translator.is_valid_gentoo_name("my_package")  # No underscores
            False
            >>> translator.is_valid_gentoo_name("my.package")  # No dots
            False
            >>> translator.is_valid_gentoo_name("my--pkg")  # No double hyphens
            False
            >>> translator.is_valid_gentoo_name("")
            False
            >>> translator.is_valid_gentoo_name("a")
            True
            >>> translator.is_valid_gentoo_name("1package")  # Can start with number
            True
        """
        if not name:
            return False
        return bool(self._gentoo_name_pattern.match(name))
    
    def normalize_pypi_name(self, name: str) -> str:
        """
        Normalize a PyPI package name according to PEP 503.

        This is useful for comparing package names.

        Examples:
            >>> translator = SimpleNameTranslator()
            >>> translator.normalize_pypi_name("Django")
            'django'
            >>> translator.normalize_pypi_name("websocket_client")
            'websocket-client'
            >>> translator.normalize_pypi_name("backports.zoneinfo")
            'backports-zoneinfo'
            >>> translator.normalize_pypi_name("My__Package---Name...")
            'my-package-name'
        """
        # PEP 503 normalization, then strip leading/trailing hyphens
        # that result from leading/trailing separators in the input
        return pip_canonicalize_name(name).strip('-')
    
    def split_category(self, full_name: str) -> Tuple[str, str]:
        """
        Split a full Gentoo package name into category and package name.
        
        Examples:
            >>> translator = SimpleNameTranslator()
            >>> translator.split_category("dev-python/django")
            ('dev-python', 'django')
            >>> translator.split_category("dev-python/python-dateutil")
            ('dev-python', 'python-dateutil')
            >>> translator.split_category("django")  # No category
            ('', 'django')
            >>> translator.split_category("virtual/python-enum34")
            ('virtual', 'python-enum34')
        """
        parts = full_name.split('/', 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return '', parts[0]


class CachedNameTranslator(SimpleNameTranslator):
    """
    Name translator with persistent caching support.
    
    This class extends SimpleNameTranslator to provide caching of name
    mappings for improved performance and better reverse translation accuracy.
    
    Examples:
        >>> translator = CachedNameTranslator()
        >>> translator.pypi_to_gentoo("Django")
        'django'
        >>> translator.preload_mappings({"Flask": "flask", "Werkzeug": "werkzeug"})
        >>> translator.gentoo_to_pypi("flask")
        'Flask'
        >>> translator.gentoo_to_pypi("werkzeug")
        'Werkzeug'
    """
    
    def __init__(self, strict_mode: bool = False):
        """
        Initialize the cached name translator.
        
        Args:
            strict_mode: If True, raise exceptions for invalid names.
        """
        super().__init__(strict_mode)
        # Additional cache for known PyPI -> Gentoo mappings
        self._forward_cache: Dict[str, str] = {}
        # Cache for preferred PyPI names (for reverse lookup)
        self._preferred_pypi: Dict[str, str] = {}
    
    def preload_mappings(self, mappings: Dict[str, str]) -> None:
        """
        Preload known PyPI to Gentoo name mappings.
        
        This improves reverse translation accuracy by providing
        the original PyPI names for packages.
        
        Args:
            mappings: Dictionary of PyPI name to Gentoo name mappings
            
        Examples:
            >>> translator = CachedNameTranslator()
            >>> translator.preload_mappings({
            ...     "beautifulsoup4": "beautifulsoup4",
            ...     "BeautifulSoup": "beautifulsoup",
            ...     "msgpack-python": "msgpack-python",
            ... })
            >>> translator.gentoo_to_pypi("beautifulsoup4")
            'beautifulsoup4'
            >>> translator.gentoo_to_pypi("beautifulsoup")
            'BeautifulSoup'
        """
        for pypi_name, gentoo_name in mappings.items():
            self._forward_cache[pypi_name] = gentoo_name
            
            # Update reverse cache
            if gentoo_name not in self._reverse_cache:
                self._reverse_cache[gentoo_name] = set()
            self._reverse_cache[gentoo_name].add(pypi_name)
            
            # Set as preferred if not already set
            if gentoo_name not in self._preferred_pypi:
                self._preferred_pypi[gentoo_name] = pypi_name
    
    def pypi_to_gentoo(self, pypi_name: str) -> str:
        """
        Translate a PyPI package name to a Gentoo package name with caching.
        
        Examples:
            >>> translator = CachedNameTranslator()
            >>> translator.pypi_to_gentoo("Werkzeug")
            'werkzeug'
            >>> translator._forward_cache.get("Werkzeug")
            'werkzeug'
        """
        # Check cache first
        if pypi_name in self._forward_cache:
            return self._forward_cache[pypi_name]
        
        # Compute and cache
        gentoo_name = super().pypi_to_gentoo(pypi_name)
        self._forward_cache[pypi_name] = gentoo_name
        
        # Set as preferred if not already set
        if gentoo_name not in self._preferred_pypi:
            self._preferred_pypi[gentoo_name] = pypi_name
        
        return gentoo_name
    
    def gentoo_to_pypi(self, gentoo_name: str, hint: Optional[str] = None) -> str:
        """
        Translate a Gentoo package name back to a PyPI package name using cache.
        
        Examples:
            >>> translator = CachedNameTranslator()
            >>> translator.preload_mappings({"PyYAML": "pyyaml"})
            >>> translator.gentoo_to_pypi("pyyaml")
            'PyYAML'
        """
        if hint and pip_canonicalize_name(hint) == gentoo_name:
            return hint
        
        # Check for preferred name first
        if gentoo_name in self._preferred_pypi:
            return self._preferred_pypi[gentoo_name]
        
        # Fall back to parent implementation
        return super().gentoo_to_pypi(gentoo_name, hint)
    
    def clear_cache(self) -> None:
        """
        Clear all cached mappings.
        
        Examples:
            >>> translator = CachedNameTranslator()
            >>> translator.pypi_to_gentoo("Django")
            'django'
            >>> len(translator._forward_cache) > 0
            True
            >>> translator.clear_cache()
            >>> len(translator._forward_cache)
            0
        """
        self._forward_cache.clear()
        self._reverse_cache.clear()
        self._preferred_pypi.clear()


# Default translator instance for convenience
default_translator = SimpleNameTranslator()


def pypi_to_gentoo(pypi_name: str) -> str:
    """
    Convenience function to translate PyPI name to Gentoo name.
    
    Examples:
        >>> pypi_to_gentoo("Django")
        'django'
        >>> pypi_to_gentoo("python-dateutil")
        'python-dateutil'
    """
    return default_translator.pypi_to_gentoo(pypi_name)


def gentoo_to_pypi(gentoo_name: str, hint: Optional[str] = None) -> str:
    """
    Convenience function to translate Gentoo name to PyPI name.
    
    Examples:
        >>> gentoo_to_pypi("django")
        'django'
        >>> gentoo_to_pypi("python-dateutil")
        'python-dateutil'
    """
    return default_translator.gentoo_to_pypi(gentoo_name, hint)