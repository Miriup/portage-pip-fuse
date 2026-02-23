"""
Package filter system for controlling which PyPI packages are visible.

This module provides flexible filtering mechanisms to control which packages
appear in the FUSE filesystem directory listings. The most important filter
is the dependency tree resolver which shows only packages needed for a
specific installation.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import logging
import os
import time
import requests
from abc import ABC, abstractmethod
from typing import Set, List, Optional, Dict, Tuple
from pathlib import Path
from xml.etree import ElementTree as ET
from packaging.requirements import Requirement
from packaging.markers import Marker, UndefinedEnvironmentName

logger = logging.getLogger(__name__)


class FilterBase(ABC):
    """
    Abstract base class for all package filters.
    
    Each filter determines which packages should be visible in the
    /dev-python/ directory listing.
    """
    
    @abstractmethod
    def get_packages(self) -> Set[str]:
        """
        Return set of PyPI package names that pass this filter.
        
        Returns:
            Set of PyPI package names (not Gentoo names)
        """
        pass
    
    @abstractmethod
    def get_description(self) -> str:
        """Get human-readable description of this filter."""
        pass
    
    @classmethod
    def get_filter_name(cls) -> str:
        """Get the name used to identify this filter in CLI arguments."""
        # Default: convert class name from FilterFooBar to foo-bar
        name = cls.__name__
        if name.startswith('Filter'):
            name = name[6:]  # Remove 'Filter' prefix
        
        # Convert CamelCase to kebab-case
        result = []
        for i, char in enumerate(name):
            if char.isupper() and i > 0:
                result.append('-')
            result.append(char.lower())
        return ''.join(result)
    
    @classmethod
    def is_default_filter(cls) -> bool:
        """Return True if this filter should be enabled by default."""
        return False


class FilterDependencyTree(FilterBase):
    """
    Filter that shows only packages in the dependency tree of specified packages.
    
    This is the most practical filter for actual installations - when you know
    you want to install a specific package, this shows only the packages
    that will actually be needed, considering USE flags (Python extras).
    
    Examples:
        >>> filter = FilterDependencyTree(['requests'])
        >>> 'urllib3' in filter.get_packages()
        True
        >>> 'django' in filter.get_packages()  # Not a dependency
        False
    """
    
    def __init__(self, root_packages: List[str], use_flags: Optional[List[str]] = None,
                 cache_dir: Optional[Path] = None, max_depth: int = 10):
        """
        Initialize dependency tree filter.
        
        Args:
            root_packages: List of packages to resolve dependencies for
            use_flags: List of USE flags (Python extras) to include
            cache_dir: Directory for caching package metadata
            max_depth: Maximum recursion depth for dependency resolution
        """
        self.root_packages = root_packages or []
        self.use_flags = use_flags or []
        self.cache_dir = cache_dir
        self.max_depth = max_depth
        self._resolved_packages: Optional[Set[str]] = None
        self._resolution_cache: Dict[str, dict] = {}
        
    def get_packages(self) -> Set[str]:
        """Resolve and return all packages in the dependency tree."""
        if self._resolved_packages is None:
            self._resolved_packages = self._resolve_all_dependencies()
        return self._resolved_packages
    
    def get_description(self) -> str:
        """Get description of this filter."""
        use_str = f" with USE flags {','.join(self.use_flags)}" if self.use_flags else ""
        return f"Dependencies of {','.join(self.root_packages)}{use_str}"
    
    def _resolve_all_dependencies(self) -> Set[str]:
        """Resolve complete dependency tree for all root packages."""
        all_packages = set()
        
        for package in self.root_packages:
            logger.info(f"Resolving dependencies for {package}")
            deps = self._resolve_package_dependencies(package, depth=0)
            all_packages.update(deps)
            logger.info(f"Package {package} has {len(deps)} total dependencies")
            
        return all_packages
    
    def _resolve_package_dependencies(self, package_name: str, depth: int = 0,
                                     visited: Optional[Set[str]] = None) -> Set[str]:
        """
        Recursively resolve dependencies for a single package.
        
        Args:
            package_name: PyPI package name to resolve
            depth: Current recursion depth
            visited: Set of already visited packages (for cycle detection)
            
        Returns:
            Set of all package names in the dependency tree
        """
        if visited is None:
            visited = set()
            
        # Normalize package name
        package_name = package_name.lower().replace('_', '-').replace('.', '-')
        
        # Check for cycles and depth limit
        if package_name in visited or depth > self.max_depth:
            return visited
            
        visited.add(package_name)
        
        # Get package metadata
        metadata = self._get_package_metadata(package_name)
        if not metadata:
            logger.warning(f"Could not get metadata for {package_name}")
            return visited
            
        # Parse dependencies
        requires_dist = metadata.get('info', {}).get('requires_dist', [])
        
        for req_str in requires_dist:
            try:
                req = Requirement(req_str)
                
                # Check if this dependency applies based on markers and extras
                if self._should_include_dependency(req):
                    # Recursively resolve this dependency
                    self._resolve_package_dependencies(
                        req.name, depth + 1, visited
                    )
                    
            except Exception as e:
                logger.debug(f"Error parsing requirement '{req_str}': {e}")
                
        return visited
    
    def _should_include_dependency(self, requirement: Requirement) -> bool:
        """
        Check if a dependency should be included based on markers and extras.
        
        Args:
            requirement: Parsed requirement object
            
        Returns:
            True if dependency should be included
        """
        if not requirement.marker:
            # No marker means unconditional dependency
            return True
            
        # Build environment for marker evaluation
        # Include the extras (USE flags) we're interested in
        environment = {
            'extra': ','.join(self.use_flags) if self.use_flags else ''
        }
        
        try:
            # Evaluate the marker in our environment
            return requirement.marker.evaluate(environment)
        except UndefinedEnvironmentName:
            # If marker uses undefined variables, include it to be safe
            return True
            
    def _get_package_metadata(self, package_name: str) -> Optional[dict]:
        """
        Fetch package metadata from PyPI.
        
        Uses caching to avoid repeated API calls.
        
        Args:
            package_name: PyPI package name
            
        Returns:
            Package metadata dict or None if not found
        """
        # Check memory cache first
        if package_name in self._resolution_cache:
            return self._resolution_cache[package_name]
            
        try:
            # Fetch from PyPI
            response = requests.get(
                f'https://pypi.org/pypi/{package_name}/json',
                timeout=10
            )
            
            if response.status_code == 200:
                metadata = response.json()
                self._resolution_cache[package_name] = metadata
                return metadata
            else:
                logger.debug(f"Package {package_name} not found on PyPI")
                return None
                
        except Exception as e:
            logger.warning(f"Error fetching metadata for {package_name}: {e}")
            return None


class FilterRecent(FilterBase):
    """
    Filter for packages updated within a certain time period.
    
    Uses PyPI's RSS feed to get recently updated packages efficiently.
    
    Examples:
        >>> filter = FilterRecent(days=7)
        >>> len(filter.get_packages()) <= 100  # RSS feed limit
        True
    """
    
    def __init__(self, days: int = 30):
        """
        Initialize recent packages filter.
        
        Args:
            days: Number of days to look back (RSS feed provides last 100 updates)
        """
        self.days = days
        self._packages: Optional[Set[str]] = None
        
    def get_packages(self) -> Set[str]:
        """Get recently updated packages from RSS feed."""
        if self._packages is None:
            self._packages = self._fetch_recent_packages()
        return self._packages
    
    def get_description(self) -> str:
        """Get description of this filter."""
        return f"Packages updated in last {self.days} days"
    
    def _fetch_recent_packages(self) -> Set[str]:
        """Fetch recently updated packages from PyPI RSS feed."""
        packages = set()
        
        try:
            # Fetch RSS feed
            response = requests.get('https://pypi.org/rss/updates.xml', timeout=10)
            if response.status_code != 200:
                logger.warning(f"Failed to fetch RSS feed: {response.status_code}")
                return packages
                
            # Parse XML
            root = ET.fromstring(response.text)
            
            # Extract package names from items
            for item in root.findall('.//item'):
                title_elem = item.find('title')
                if title_elem is not None and title_elem.text:
                    # Title format is "package-name version"
                    parts = title_elem.text.rsplit(' ', 1)
                    if parts:
                        package_name = parts[0]
                        packages.add(package_name)
                        
            logger.info(f"Found {len(packages)} recently updated packages")
            
        except Exception as e:
            logger.error(f"Error fetching recent packages: {e}")
            
        return packages


class FilterNewest(FilterBase):
    """
    Filter for newly created packages.
    
    Uses PyPI's RSS feed for new packages.
    
    Examples:
        >>> filter = FilterNewest(count=50)
        >>> len(filter.get_packages()) <= 100  # RSS feed limit
        True
    """
    
    def __init__(self, count: int = 100):
        """
        Initialize newest packages filter.
        
        Args:
            count: Maximum number of packages (RSS provides last 100)
        """
        self.count = min(count, 100)  # RSS feed limit
        self._packages: Optional[Set[str]] = None
        
    def get_packages(self) -> Set[str]:
        """Get newest packages from RSS feed."""
        if self._packages is None:
            self._packages = self._fetch_newest_packages()
        return self._packages
    
    def get_description(self) -> str:
        """Get description of this filter."""
        return f"Last {self.count} new packages on PyPI"
    
    def _fetch_newest_packages(self) -> Set[str]:
        """Fetch newest packages from PyPI RSS feed."""
        packages = set()
        
        try:
            # Fetch RSS feed for new packages
            response = requests.get('https://pypi.org/rss/packages.xml', timeout=10)
            if response.status_code != 200:
                logger.warning(f"Failed to fetch RSS feed: {response.status_code}")
                return packages
                
            # Parse XML
            root = ET.fromstring(response.text)
            
            # Extract package names from items
            count = 0
            for item in root.findall('.//item'):
                if count >= self.count:
                    break
                    
                title_elem = item.find('title')
                if title_elem is not None and title_elem.text:
                    # Title format is "package-name version"
                    parts = title_elem.text.rsplit(' ', 1)
                    if parts:
                        package_name = parts[0]
                        packages.add(package_name)
                        count += 1
                        
            logger.info(f"Found {len(packages)} newest packages")
            
        except Exception as e:
            logger.error(f"Error fetching newest packages: {e}")
            
        return packages


class FilterCurated(FilterBase):
    """
    Filter with manually curated list of popular/useful packages.
    
    Examples:
        >>> filter = FilterCurated()
        >>> 'requests' in filter.get_packages()
        True
        >>> 'django' in filter.get_packages()
        True
    """
    
    # Default curated list of popular Python packages
    DEFAULT_PACKAGES = {
        # Web frameworks
        'django', 'flask', 'fastapi', 'bottle', 'tornado', 'pyramid',
        'aiohttp', 'starlette', 'sanic', 'quart',
        
        # HTTP/Networking
        'requests', 'urllib3', 'httpx', 'websockets', 'certifi',
        'charset-normalizer', 'idna',
        
        # Database
        'sqlalchemy', 'alembic', 'psycopg2', 'pymongo', 'redis',
        'peewee', 'dataset', 'databases',
        
        # Data science
        'numpy', 'scipy', 'pandas', 'matplotlib', 'seaborn',
        'scikit-learn', 'statsmodels', 'networkx',
        
        # ML/AI
        'tensorflow', 'torch', 'transformers', 'openai', 'anthropic',
        'langchain', 'llama-index', 'huggingface-hub',
        
        # Testing
        'pytest', 'pytest-cov', 'pytest-mock', 'tox', 'coverage',
        'unittest-xml-reporting', 'nose2', 'hypothesis',
        
        # Development tools
        'black', 'flake8', 'mypy', 'isort', 'pylint', 'ruff',
        'pre-commit', 'autopep8', 'yapf',
        
        # Utilities
        'click', 'typer', 'rich', 'pydantic', 'attrs', 'marshmallow',
        'python-dotenv', 'pyyaml', 'toml', 'configparser',
        
        # Special additions
        'open-webui', 'streamlit', 'gradio', 'jupyter', 'ipython'
    }
    
    def __init__(self, packages: Optional[Set[str]] = None):
        """
        Initialize curated filter.
        
        Args:
            packages: Custom set of packages, or None for defaults
        """
        self.packages = packages if packages is not None else self.DEFAULT_PACKAGES
    
    @classmethod
    def is_default_filter(cls) -> bool:
        """This filter is enabled by default."""
        return True
        
    def get_packages(self) -> Set[str]:
        """Return the curated package list."""
        return self.packages
    
    def get_description(self) -> str:
        """Get description of this filter."""
        return f"Curated list of {len(self.packages)} popular packages"


class FilterChain(FilterBase):
    """
    Combine multiple filters with AND or OR logic.
    
    Examples:
        >>> curated = FilterCurated({'requests', 'django'})
        >>> recent = FilterCurated({'flask', 'django'})  # Mock as curated for test
        >>> chain = FilterChain([curated, recent], operator='OR')
        >>> combined = chain.get_packages()
        >>> 'requests' in combined and 'flask' in combined
        True
    """
    
    def __init__(self, filters: List[FilterBase], operator: str = 'OR',
                 max_results: int = 5000):
        """
        Initialize filter chain.
        
        Args:
            filters: List of filters to combine
            operator: 'AND' or 'OR' logic
            max_results: Maximum packages to return
        """
        self.filters = filters
        self.operator = operator.upper()
        self.max_results = max_results
        
        if self.operator not in ('AND', 'OR'):
            raise ValueError(f"Invalid operator: {operator}")
            
    def get_packages(self) -> Set[str]:
        """Combine filters and return package set."""
        if not self.filters:
            return set()
            
        if self.operator == 'OR':
            # Union of all filters
            combined = set()
            for filter_obj in self.filters:
                combined.update(filter_obj.get_packages())
                if len(combined) >= self.max_results:
                    break
        else:  # AND
            # Intersection of all filters
            combined = self.filters[0].get_packages()
            for filter_obj in self.filters[1:]:
                combined &= filter_obj.get_packages()
                
        # Limit results
        if len(combined) > self.max_results:
            combined = set(list(combined)[:self.max_results])
            
        return combined
    
    def get_description(self) -> str:
        """Get description of this filter chain."""
        descriptions = [f.get_description() for f in self.filters]
        return f" {self.operator} ".join(descriptions)


class FilterAll(FilterBase):
    """
    No filtering - shows all packages (warning: 746k+ packages).
    
    This is mainly for testing and should be used with caution.
    """
    
    def __init__(self):
        """Initialize the all-packages filter."""
        self._packages: Optional[Set[str]] = None
        
    def get_packages(self) -> Set[str]:
        """Fetch all package names from PyPI simple index."""
        if self._packages is None:
            self._packages = self._fetch_all_packages()
        return self._packages
    
    def get_description(self) -> str:
        """Get description of this filter."""
        return "All PyPI packages (WARNING: 746k+ packages)"
    
    def _fetch_all_packages(self) -> Set[str]:
        """Fetch complete package list from PyPI simple index."""
        logger.warning("Fetching ALL PyPI packages - this will be slow!")
        packages = set()
        
        try:
            import re
            response = requests.get('https://pypi.org/simple/', timeout=30)
            if response.status_code == 200:
                # Parse package names from HTML
                matches = re.findall(r'<a href="/simple/([^/]+)/">', response.text)
                packages.update(matches)
                logger.info(f"Found {len(packages)} total packages on PyPI")
        except Exception as e:
            logger.error(f"Error fetching all packages: {e}")
            
        return packages


class FilterPythonCompat(FilterBase):
    """
    Filter packages based on Python compatibility with the current Gentoo system.
    
    This filter checks if packages have any overlap with the Python implementations
    supported by the current Gentoo installation (from PYTHON_TARGETS).
    
    NOTE: This filter is NOT enabled by default as it's too expensive for large sets.
    Python compatibility is enforced at the ebuild level through PYTHON_COMPAT instead.
    """
    
    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or Path('/tmp/portage-pip-fuse-cache')
        self.supported_python_impls = self._get_portage_python_targets()
        logger.info(f"System Python targets: {sorted(self.supported_python_impls)}")
    
    @classmethod
    def is_default_filter(cls) -> bool:
        """This filter is NOT enabled by default - it's too expensive for large package sets."""
        return False
    
    def get_packages(self) -> Set[str]:
        """
        Return ALL packages - compatibility check happens at ebuild generation time.
        
        The python-compat filter doesn't pre-filter packages, but instead ensures
        that generated ebuilds have correct PYTHON_COMPAT. Packages incompatible
        with the system will have no valid PYTHON_COMPAT and won't be installable.
        
        This is more efficient than checking every package upfront and allows
        browsing all packages while preventing installation of incompatible ones.
        """
        # For now, return all packages from simple index
        # The actual filtering happens when PYTHON_COMPAT is generated in ebuilds
        # This avoids the performance issue of checking 746k+ packages
        
        # Since we need some reasonable base set and fetching all 746k is too slow,
        # we'll just pass through - the filter chain will intersect with other filters
        # This filter acts as a "validator" rather than a "selector"
        
        # Return a special marker that indicates "no restriction"
        # The FilterChain should handle this specially
        return FilterBase.NO_RESTRICTION
    
    def get_description(self) -> str:
        """Get description of this filter."""
        return f"Python-compatible packages (targets: {', '.join(sorted(self.supported_python_impls))})"
    
    def _get_portage_python_targets(self) -> Set[str]:
        """Get supported Python implementations from Portage configuration."""
        try:
            # Try to use portage directly
            import portage
            settings = portage.config()
            python_targets = settings.get('PYTHON_TARGETS', '').split()
            if python_targets:
                return set(python_targets)
        except ImportError:
            logger.debug("Portage module not available, falling back to config file parsing")
        
        # Fallback: parse from configuration files
        targets = self._parse_python_targets_from_config()
        if targets:
            return targets
        
        # Ultimate fallback: assume common current targets
        logger.warning("Could not determine PYTHON_TARGETS, using default: python3_11 python3_12")
        return {'python3_11', 'python3_12'}
    
    def _parse_python_targets_from_config(self) -> Set[str]:
        """Parse PYTHON_TARGETS from Gentoo configuration files."""
        config_files = [
            '/etc/portage/make.conf',
            '/etc/make.conf',
            '/usr/share/portage/config/make.globals'
        ]
        
        for config_file in config_files:
            if os.path.exists(config_file):
                try:
                    with open(config_file, 'r') as f:
                        content = f.read()
                        
                    # Look for PYTHON_TARGETS="python3_11 python3_12"
                    import re
                    match = re.search(r'PYTHON_TARGETS\s*=\s*["\']([^"\']+)["\']', content)
                    if match:
                        targets = match.group(1).split()
                        if targets:
                            return set(targets)
                            
                except (IOError, OSError) as e:
                    logger.debug(f"Could not read {config_file}: {e}")
                    continue
        
        return set()


class FilterSourceDistribution(FilterBase):
    """
    Filter packages that have source distributions available.
    
    This filter excludes wheel-only packages that don't have source code available,
    which are generally not suitable for Gentoo's build-from-source philosophy.
    """
    
    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or Path('/tmp/portage-pip-fuse-cache')
        # Import here to avoid circular imports
        from .pip_metadata import PyPIMetadataExtractor
        self.pypi_extractor = PyPIMetadataExtractor(cache_dir=self.cache_dir)
        self._packages_with_source = None
    
    @classmethod
    def is_default_filter(cls) -> bool:
        """This filter is enabled by default."""
        return True
    
    def get_packages(self) -> Set[str]:
        """Return packages that have source distributions available."""
        if self._packages_with_source is None:
            self._packages_with_source = self._find_packages_with_source()
        return self._packages_with_source
    
    def get_description(self) -> str:
        """Get description of this filter."""
        return "Packages with source distributions (excludes wheel-only packages)"
    
    def _find_packages_with_source(self) -> Set[str]:
        """Find all packages that have source distributions."""
        logger.info("Finding packages with source distributions...")
        
        # Start with a base set of packages to check
        # We'll use the curated list as a starting point for efficiency
        from .pip_metadata import PyPIMetadataExtractor
        
        # Get popular packages from RSS feed as candidates
        packages_to_check = set()
        
        try:
            # Use recent packages as a practical base set
            response = requests.get('https://pypi.org/rss/updates.xml', timeout=30)
            response.raise_for_status()
            
            root = ET.fromstring(response.content)
            
            # Extract package names from RSS items
            for item in root.findall('.//item'):
                title = item.find('title')
                if title is not None and title.text:
                    # Title format: "package-name version"
                    package_name = title.text.split()[0] if ' ' in title.text else title.text
                    packages_to_check.add(package_name)
            
            logger.info(f"Checking {len(packages_to_check)} packages for source distributions")
            
        except Exception as e:
            logger.warning(f"Could not fetch package list from RSS: {e}")
            # Fallback to curated list
            curated_filter = FilterCurated()
            packages_to_check = curated_filter.get_packages()
            logger.info(f"Using curated list fallback: {len(packages_to_check)} packages")
        
        # Check each package for source distribution availability
        packages_with_source = set()
        
        for package_name in packages_to_check:
            try:
                # Get latest package info
                package_info = self.pypi_extractor.get_complete_package_info(package_name)
                if package_info:
                    source_dist = package_info.get('source_distribution')
                    if source_dist and source_dist.get('url'):
                        packages_with_source.add(package_name)
                        
            except Exception as e:
                logger.debug(f"Error checking source distribution for {package_name}: {e}")
                continue
        
        logger.info(f"Found {len(packages_with_source)} packages with source distributions")
        return packages_with_source


class FilterRegistry:
    """Registry for managing all available filters."""
    
    _filters = {
        'all': FilterAll,
        'curated': FilterCurated,
        'recent': FilterRecent, 
        'newest': FilterNewest,
        'deps': FilterDependencyTree,
        'python-compat': FilterPythonCompat,
        'source-dist': FilterSourceDistribution,
    }
    
    @classmethod
    def get_filter_class(cls, name: str):
        """Get filter class by name."""
        return cls._filters.get(name)
    
    @classmethod
    def get_all_filters(cls) -> Dict[str, type]:
        """Get all available filters."""
        return cls._filters.copy()
    
    @classmethod
    def get_default_filters(cls) -> List[str]:
        """Get names of filters that should be enabled by default."""
        defaults = []
        for name, filter_class in cls._filters.items():
            if filter_class.is_default_filter():
                defaults.append(name)
        return defaults
    
    @classmethod
    def register_filter(cls, name: str, filter_class: type):
        """Register a new filter."""
        cls._filters[name] = filter_class