"""
Version-level filters for controlling which ebuilds are visible.

These filters operate at the version/ebuild level, determining which specific
versions of a package should be made available. They run when listing package
versions or generating ebuilds, after we've already fetched PyPI metadata.

This is more efficient than package-level filtering for checks that require
PyPI metadata, such as:
- Checking if source distributions are available
- Verifying Python compatibility
- Filtering by release date or stability

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import logging
import os
import subprocess
from abc import ABC, abstractmethod
from typing import Dict, List, Set, Optional, Any
from pathlib import Path

logger = logging.getLogger(__name__)


class VersionFilterBase(ABC):
    """
    Abstract base class for version-level filters.
    
    Version filters determine which versions of a package should be
    visible as ebuilds. They operate on PyPI metadata that's already
    been fetched.
    """
    
    @abstractmethod
    def filter_versions(self, pypi_name: str, versions_metadata: Dict[str, Dict]) -> Dict[str, Dict]:
        """
        Filter available versions of a package.
        
        Args:
            pypi_name: PyPI package name
            versions_metadata: Dict mapping version strings to metadata dicts
            
        Returns:
            Filtered dict with only versions that should be visible
        """
        pass
    
    @abstractmethod
    def should_include_version(self, pypi_name: str, version: str, metadata: Dict) -> bool:
        """
        Check if a specific version should be included.
        
        Args:
            pypi_name: PyPI package name
            version: Version string
            metadata: Version metadata from PyPI
            
        Returns:
            True if version should be visible as an ebuild
        """
        pass
    
    @abstractmethod
    def get_description(self) -> str:
        """Get human-readable description of this filter."""
        pass
    
    @classmethod
    def get_filter_name(cls) -> str:
        """Get the name used to identify this filter."""
        name = cls.__name__
        if name.startswith('VersionFilter'):
            name = name[13:]  # Remove 'VersionFilter' prefix
        
        # Convert CamelCase to kebab-case
        result = []
        for i, char in enumerate(name):
            if char.isupper() and i > 0:
                result.append('-')
            result.append(char.lower())
        return ''.join(result)


class VersionFilterSourceDist(VersionFilterBase):
    """
    Filter to only show versions that have source distributions available.
    
    This filter excludes wheel-only releases that don't have source code,
    which are not suitable for Gentoo's build-from-source philosophy.
    """
    
    def filter_versions(self, pypi_name: str, versions_metadata: Dict[str, Dict]) -> Dict[str, Dict]:
        """Filter to only versions with source distributions."""
        filtered = {}
        for version, metadata in versions_metadata.items():
            if self.should_include_version(pypi_name, version, metadata):
                filtered[version] = metadata
        return filtered
    
    def should_include_version(self, pypi_name: str, version: str, metadata: Dict) -> bool:
        """Check if version has a source distribution."""
        # Check if this version has a source distribution
        urls = metadata.get('urls', [])
        if not urls:
            # Try releases format (from package JSON)
            releases = metadata.get('releases', {})
            if version in releases:
                urls = releases[version]
        
        for url_info in urls:
            packagetype = url_info.get('packagetype', '')
            if packagetype == 'sdist':
                return True
        
        logger.debug(f"Version {version} of {pypi_name} has no source distribution")
        return False
    
    def get_description(self) -> str:
        """Get description of this filter."""
        return "Only versions with source distributions"


class VersionFilterPythonCompat(VersionFilterBase):
    """
    Filter to only show versions compatible with system Python implementations.
    
    This filter checks the Python version requirements and only shows
    versions that are compatible with at least one system Python.
    """
    
    def __init__(self):
        """Initialize with system Python targets."""
        self.valid_impls = self._get_python_all_impls()
        self.supported_pythons = self._get_system_python_versions()
        logger.info(f"Valid Python implementations: {sorted(self.valid_impls)}")
        logger.info(f"System Python versions: {self.supported_pythons}")
    
    def _get_system_python_versions(self) -> Set[str]:
        """Get Python versions supported by the system."""
        # First, get the valid implementations from the eclass
        valid_impls = self._get_python_all_impls()
        
        # Then intersect with system's PYTHON_TARGETS
        try:
            import portage
            settings = portage.config()
            python_targets = settings.get('PYTHON_TARGETS', '').split()
            
            # Only consider targets that are in _PYTHON_ALL_IMPLS
            valid_targets = [t for t in python_targets if t in valid_impls]
            
            # Extract version numbers from python3_11 -> 3.11
            versions = set()
            for target in valid_targets:
                if target.startswith('python'):
                    parts = target.replace('python', '').split('_')
                    if len(parts) == 2:
                        versions.add(f"{parts[0]}.{parts[1]}")
            
            if versions:
                return versions
        except ImportError:
            pass
        
        # Fallback: check installed Python versions that are in _PYTHON_ALL_IMPLS
        versions = set()
        for impl in valid_impls:
            if impl.startswith('python3_'):
                # Convert python3_11 to 3.11
                parts = impl.replace('python', '').split('_')
                if len(parts) == 2:
                    version = f"{parts[0]}.{parts[1]}"
                    # Check if this Python is actually installed
                    python_exe = f"python{version}"
                    if os.path.exists(f"/usr/bin/{python_exe}"):
                        versions.add(version)
        
        if not versions:
            # Ultimate fallback - use known current versions
            versions = {'3.11', '3.12'}
            logger.warning(f"Could not detect Python versions, using defaults: {versions}")
        
        return versions
    
    def _get_python_all_impls(self) -> Set[str]:
        """
        Get _PYTHON_ALL_IMPLS from python-utils-r1.eclass using Portage APIs.
        
        This is the list of Python implementations currently supported by Gentoo.
        """
        try:
            import portage
            import subprocess
            
            # Get the Gentoo repo location using Portage API
            settings = portage.config()
            repo_path = settings.repositories.get_location_for_name("gentoo")
            if not repo_path:
                raise ValueError("Could not find Gentoo repository")
            
            eclass_path = os.path.join(repo_path, "eclass", "python-utils-r1.eclass")
            
            # Source the eclass and get _PYTHON_ALL_IMPLS
            # We use subprocess but with Portage's repo path discovery
            cmd = f'EAPI=8 source {eclass_path} && echo "${{_PYTHON_ALL_IMPLS[@]}}"'
            result = subprocess.run(['bash', '-c', cmd], 
                                  capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0 and result.stdout:
                impls = result.stdout.strip().split()
                logger.debug(f"Got _PYTHON_ALL_IMPLS from eclass: {impls}")
                return set(impls)
            else:
                logger.warning(f"Failed to read eclass: {result.stderr}")
            
        except ImportError:
            logger.debug("Portage not available, using fallback Python implementations")
        except subprocess.TimeoutExpired:
            logger.warning("Timeout reading eclass, using fallback")
        except Exception as e:
            logger.warning(f"Could not read _PYTHON_ALL_IMPLS from eclass: {e}")
        
        # Fallback to hardcoded current values (as of 2024/2025)
        return {
            'pypy3_11',
            'python3_11', 'python3_12', 'python3_13', 'python3_14',
            'python3_13t', 'python3_14t'
        }
    
    def filter_versions(self, pypi_name: str, versions_metadata: Dict[str, Dict]) -> Dict[str, Dict]:
        """Filter to only Python-compatible versions."""
        filtered = {}
        for version, metadata in versions_metadata.items():
            if self.should_include_version(pypi_name, version, metadata):
                filtered[version] = metadata
        return filtered
    
    def should_include_version(self, pypi_name: str, version: str, metadata: Dict) -> bool:
        """Check if version is compatible with valid Gentoo Python implementations."""
        # Get requires_python from metadata
        info = metadata.get('info', metadata)
        requires_python = info.get('requires_python', '')
        
        if not requires_python:
            # No requirement - check if we have ANY valid Python implementation
            return len(self.supported_pythons) > 0
        
        # Check if any valid Python implementation satisfies the requirement
        try:
            from packaging.specifiers import SpecifierSet
            spec = SpecifierSet(requires_python)
            
            # We need to check against implementations that are BOTH:
            # 1. In _PYTHON_ALL_IMPLS (valid for Gentoo)
            # 2. Satisfy the package's requires_python
            
            compatible_impls = []
            for impl in self.valid_impls:
                # Convert impl name to version (python3_11 -> 3.11)
                if impl.startswith('python3_'):
                    # Handle both python3_11 and python3_13t
                    suffix = impl[8:]  # Remove 'python3_'
                    if suffix.endswith('t'):  # Free-threading build
                        version_part = suffix[:-1]  # Remove 't' suffix
                    else:
                        version_part = suffix
                    
                    try:
                        py_version = f"3.{version_part}"
                        if py_version in spec:
                            compatible_impls.append(impl)
                    except ValueError:
                        # Skip malformed versions
                        continue
                elif impl.startswith('pypy3_'):
                    # PyPy3_11 corresponds roughly to Python 3.10
                    # This is a simplification - we could look this up
                    if impl == 'pypy3_11':
                        try:
                            if '3.10' in spec:
                                compatible_impls.append(impl)
                        except ValueError:
                            continue
            
            if compatible_impls:
                logger.debug(f"Version {version} of {pypi_name} compatible with: {compatible_impls}")
                return True
            else:
                logger.debug(f"Version {version} of {pypi_name} requires Python {requires_python}, "
                           f"no valid Gentoo Python implementations available")
                return False
            
        except Exception as e:
            logger.warning(f"Could not parse Python requirement '{requires_python}': {e}")
            # Be permissive on parse errors - let it through if we have valid Pythons
            return len(self.supported_pythons) > 0
    
    def get_description(self) -> str:
        """Get description of this filter."""
        return f"Python-compatible versions ({', '.join(sorted(self.supported_pythons))})"


class VersionFilterLatest(VersionFilterBase):
    """
    Filter to only show the latest N versions of each package.
    
    This helps reduce clutter by limiting the number of versions shown.
    """
    
    def __init__(self, max_versions: int = 5):
        """
        Initialize the filter.
        
        Args:
            max_versions: Maximum number of versions to show per package
        """
        self.max_versions = max_versions
    
    def filter_versions(self, pypi_name: str, versions_metadata: Dict[str, Dict]) -> Dict[str, Dict]:
        """Keep only the latest N versions."""
        if len(versions_metadata) <= self.max_versions:
            return versions_metadata
        
        # Sort versions (newest first)
        try:
            from packaging.version import Version
            sorted_versions = sorted(
                versions_metadata.keys(),
                key=Version,
                reverse=True
            )[:self.max_versions]
            
            return {v: versions_metadata[v] for v in sorted_versions}
        except Exception as e:
            logger.warning(f"Could not sort versions for {pypi_name}: {e}")
            # Fallback: just take first N
            items = list(versions_metadata.items())[:self.max_versions]
            return dict(items)
    
    def should_include_version(self, pypi_name: str, version: str, metadata: Dict) -> bool:
        """This filter needs the full version list to work."""
        # This method isn't ideal for this filter type
        # We need the full list to determine latest versions
        return True
    
    def get_description(self) -> str:
        """Get description of this filter."""
        return f"Latest {self.max_versions} versions only"


class VersionFilterChain:
    """
    Combine multiple version filters with AND logic.
    
    A version must pass ALL filters to be included.
    """
    
    def __init__(self, filters: List[VersionFilterBase]):
        """
        Initialize the filter chain.
        
        Args:
            filters: List of filters to apply
        """
        self.filters = filters
    
    def filter_versions(self, pypi_name: str, versions_metadata: Dict[str, Dict]) -> Dict[str, Dict]:
        """Apply all filters to the version list."""
        filtered = versions_metadata
        for filter_obj in self.filters:
            filtered = filter_obj.filter_versions(pypi_name, filtered)
            if not filtered:
                break  # No point continuing if no versions left
        return filtered
    
    def should_include_version(self, pypi_name: str, version: str, metadata: Dict) -> bool:
        """Check if version passes all filters."""
        for filter_obj in self.filters:
            if not filter_obj.should_include_version(pypi_name, version, metadata):
                return False
        return True
    
    def get_description(self) -> str:
        """Get combined description."""
        if not self.filters:
            return "No version filters"
        descriptions = [f.get_description() for f in self.filters]
        return " AND ".join(descriptions)


class VersionFilterRegistry:
    """Registry of available version filters."""
    
    _filters = {}
    
    @classmethod
    def register(cls, name: str, filter_class: type):
        """Register a version filter."""
        cls._filters[name] = filter_class
    
    @classmethod
    def get_filter_class(cls, name: str) -> Optional[type]:
        """Get a filter class by name."""
        return cls._filters.get(name)
    
    @classmethod
    def get_all_filters(cls) -> Dict[str, type]:
        """Get all registered filters."""
        return cls._filters.copy()


# Register built-in filters
VersionFilterRegistry.register('source-dist', VersionFilterSourceDist)
VersionFilterRegistry.register('python-compat', VersionFilterPythonCompat)
VersionFilterRegistry.register('latest', VersionFilterLatest)