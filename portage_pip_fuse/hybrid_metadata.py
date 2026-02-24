"""
Hybrid PyPI metadata extractor using SQLite backend with JSON API fallback.

This module provides a high-performance metadata extractor that uses the bulk
SQLite database from pypi-data/pypi-json-data as the primary source, with
automatic fallback to the individual PyPI JSON API for missing packages.

This approach combines the performance benefits of bulk data access with
the completeness guarantee of the JSON API fallback.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import json
import logging
import time
from typing import Dict, List, Optional, Any, Set, Tuple
from urllib.error import URLError

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from portage_pip_fuse.sqlite_metadata import SQLiteMetadataBackend
from portage_pip_fuse.pip_metadata import PyPIMetadataExtractor

logger = logging.getLogger(__name__)


class HybridMetadataExtractor:
    """
    Hybrid metadata extractor using SQLite backend with JSON API fallback.
    
    This extractor attempts to retrieve package metadata from the local SQLite
    database first, and falls back to the PyPI JSON API for packages that are
    not found in the bulk data or when the bulk data is incomplete.
    
    Features:
    - Primary: SQLite database for fast bulk access
    - Secondary: PyPI JSON API for missing/newer packages
    - Automatic fallback with transparent error handling
    - Performance tracking for both backends
    - Configurable cache behavior
    """
    
    def __init__(self, 
                 cache_dir: Optional[str] = None,
                 cache_ttl: int = 3600,
                 enable_fallback: bool = True,
                 sqlite_max_age_days: int = 7):
        """
        Initialize hybrid metadata extractor.
        
        Args:
            cache_dir: Directory for caching (used by both backends)
            cache_ttl: Cache time-to-live for JSON API fallback
            enable_fallback: Enable fallback to PyPI JSON API
            sqlite_max_age_days: Maximum age for SQLite database staleness
        """
        # Store cache_dir for access by other components
        self.cache_dir = cache_dir

        # SQLite backend (primary)
        self.sqlite_backend = SQLiteMetadataBackend(
            cache_dir=cache_dir,
            max_age_days=sqlite_max_age_days
        )
        
        # JSON API backend (fallback) 
        self.json_backend = None
        if enable_fallback:
            self.json_backend = PyPIMetadataExtractor(
                cache_dir=cache_dir,
                cache_ttl=cache_ttl
            )
        
        self.enable_fallback = enable_fallback
        
        # Performance tracking
        self._stats = {
            'sqlite_hits': 0,
            'sqlite_misses': 0,
            'fallback_calls': 0,
            'fallback_successes': 0,
            'fallback_failures': 0,
            'total_requests': 0
        }
        
        # Initialize SQLite backend
        self._sqlite_ready = False
        
    def _ensure_sqlite_backend(self) -> bool:
        """
        Ensure SQLite backend is ready.
        
        Returns:
            True if SQLite backend is available, False otherwise
        """
        if self._sqlite_ready:
            return True
            
        self._sqlite_ready = self.sqlite_backend.ensure_database()
        
        if not self._sqlite_ready:
            logger.warning("SQLite backend not available - using fallback only")
            
        return self._sqlite_ready
        
    def get_package_json(self, package_name: str) -> Optional[Dict[str, Any]]:
        """
        Get complete package JSON metadata.
        
        Attempts SQLite backend first, falls back to PyPI JSON API.
        
        Args:
            package_name: Name of PyPI package
            
        Returns:
            Package metadata dict, or None if not found
        """
        self._stats['total_requests'] += 1
        
        # Try SQLite backend first
        if self._ensure_sqlite_backend():
            try:
                metadata = self.sqlite_backend.get_package_metadata(package_name)
                if metadata:
                    self._stats['sqlite_hits'] += 1
                    logger.debug(f"SQLite hit for {package_name}")
                    return self._convert_sqlite_to_json_format(package_name, metadata)
                else:
                    self._stats['sqlite_misses'] += 1
                    logger.debug(f"SQLite miss for {package_name}")
            except Exception as e:
                logger.debug(f"SQLite error for {package_name}: {e}")
                self._stats['sqlite_misses'] += 1
        
        # Fallback to JSON API
        if self.enable_fallback and self.json_backend:
            try:
                self._stats['fallback_calls'] += 1
                logger.debug(f"Using JSON API fallback for {package_name}")
                
                result = self.json_backend.get_package_json(package_name)
                if result:
                    self._stats['fallback_successes'] += 1
                    logger.debug(f"JSON API success for {package_name}")
                else:
                    self._stats['fallback_failures'] += 1
                    logger.debug(f"JSON API failure for {package_name}")
                    
                return result
                
            except Exception as e:
                logger.debug(f"JSON API error for {package_name}: {e}")
                self._stats['fallback_failures'] += 1
                
        logger.debug(f"No metadata found for {package_name}")
        return None
        
    def get_package_versions(self, package_name: str) -> List[str]:
        """
        Get all versions for a package.
        
        Args:
            package_name: Name of PyPI package
            
        Returns:
            List of version strings, sorted newest first
        """
        # Try SQLite backend first
        if self._ensure_sqlite_backend():
            try:
                versions = self.sqlite_backend.get_package_versions(package_name)
                if versions:
                    logger.debug(f"SQLite versions for {package_name}: {len(versions)}")
                    return versions
            except Exception as e:
                logger.debug(f"SQLite error getting versions for {package_name}: {e}")
        
        # Fallback to JSON API
        if self.enable_fallback and self.json_backend:
            try:
                logger.debug(f"Using JSON API fallback for versions of {package_name}")
                
                package_json = self.json_backend.get_package_json(package_name)
                if package_json and 'releases' in package_json:
                    # Extract versions from releases
                    versions = list(package_json['releases'].keys())
                    # Sort by version (newest first)
                    # Note: Proper version sorting would require packaging.version
                    versions.sort(reverse=True)
                    return versions
                    
            except Exception as e:
                logger.debug(f"JSON API error getting versions for {package_name}: {e}")
                
        return []
        
    def get_package_release_info(self, package_name: str, version: str) -> List[Dict[str, Any]]:
        """
        Get release information for a specific package version.
        
        Args:
            package_name: Name of PyPI package  
            version: Package version
            
        Returns:
            List of release file dictionaries
        """
        # Try SQLite backend first
        if self._ensure_sqlite_backend():
            try:
                releases = self.sqlite_backend.get_package_releases(package_name, version)
                if releases:
                    logger.debug(f"SQLite releases for {package_name} {version}: {len(releases)}")
                    return releases
            except Exception as e:
                logger.debug(f"SQLite error getting releases for {package_name} {version}: {e}")
        
        # Fallback to JSON API
        if self.enable_fallback and self.json_backend:
            try:
                logger.debug(f"Using JSON API fallback for releases of {package_name} {version}")
                
                package_json = self.json_backend.get_package_json(package_name)
                if package_json and 'releases' in package_json:
                    releases = package_json['releases'].get(version, [])
                    return releases
                    
            except Exception as e:
                logger.debug(f"JSON API error getting releases for {package_name} {version}: {e}")
                
        return []
        
    def _convert_sqlite_to_json_format(self, package_name: str, sqlite_metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert SQLite metadata format to PyPI JSON format.
        
        The SQLite database has a different schema than the PyPI JSON API,
        so we need to convert between formats for compatibility.
        
        Args:
            package_name: Package name
            sqlite_metadata: Metadata from SQLite database
            
        Returns:
            Metadata in PyPI JSON API format
        """
        # Map SQLite fields to JSON API format
        info = {
            'name': sqlite_metadata.get('name', package_name),
            'summary': sqlite_metadata.get('summary', ''),
            'author': sqlite_metadata.get('author', ''),
            'author_email': sqlite_metadata.get('author_email', ''),
            'home_page': sqlite_metadata.get('home_page', ''),
            'license': sqlite_metadata.get('license', ''),
            'requires_python': sqlite_metadata.get('requires_python', ''),
            'version': sqlite_metadata.get('version', ''),
            'description': sqlite_metadata.get('description', ''),
            'keywords': sqlite_metadata.get('keywords', ''),
        }
        
        # Get releases from SQLite backend
        releases = {}
        try:
            versions = self.sqlite_backend.get_package_versions(package_name)
            for version in versions:
                version_releases = self.sqlite_backend.get_package_releases(package_name, version)
                if version_releases:
                    releases[version] = version_releases
        except Exception as e:
            logger.debug(f"Error getting releases for {package_name}: {e}")
        
        return {
            'info': info,
            'releases': releases,
            'last_serial': None  # Not available in SQLite data
        }
        
    def get_performance_stats(self) -> Dict[str, Any]:
        """
        Get performance statistics for both backends.
        
        Returns:
            Dictionary with performance statistics
        """
        total_requests = self._stats['total_requests']
        sqlite_requests = self._stats['sqlite_hits'] + self._stats['sqlite_misses']
        
        stats = {
            'total_requests': total_requests,
            'sqlite_backend': {
                'hits': self._stats['sqlite_hits'],
                'misses': self._stats['sqlite_misses'],
                'hit_rate': self._stats['sqlite_hits'] / max(sqlite_requests, 1),
                'available': self._sqlite_ready
            },
            'fallback_backend': {
                'calls': self._stats['fallback_calls'],
                'successes': self._stats['fallback_successes'],
                'failures': self._stats['fallback_failures'],
                'success_rate': self._stats['fallback_successes'] / max(self._stats['fallback_calls'], 1),
                'enabled': self.enable_fallback
            }
        }
        
        return stats
        
    def print_performance_stats(self):
        """Print human-readable performance statistics."""
        stats = self.get_performance_stats()
        
        print("\n" + "="*60)
        print("HYBRID METADATA EXTRACTOR PERFORMANCE")
        print("="*60)
        
        print(f"Total requests: {stats['total_requests']}")
        
        sqlite_stats = stats['sqlite_backend']
        print(f"\nSQLite Backend (available: {sqlite_stats['available']}):")
        print(f"  Hits: {sqlite_stats['hits']}")
        print(f"  Misses: {sqlite_stats['misses']}")
        print(f"  Hit rate: {sqlite_stats['hit_rate']:.2%}")
        
        fallback_stats = stats['fallback_backend']
        print(f"\nFallback Backend (enabled: {fallback_stats['enabled']}):")
        print(f"  Calls: {fallback_stats['calls']}")
        print(f"  Successes: {fallback_stats['successes']}")
        print(f"  Failures: {fallback_stats['failures']}")
        print(f"  Success rate: {fallback_stats['success_rate']:.2%}")
        
        # Calculate performance improvement
        if stats['total_requests'] > 0:
            sqlite_ratio = sqlite_stats['hits'] / stats['total_requests']
            print(f"\nPerformance improvement: {sqlite_ratio:.2%} of requests served from SQLite")
            
    def close(self):
        """Close both backends."""
        if self.sqlite_backend:
            self.sqlite_backend.close()
        # JSON backend doesn't need explicit closing
        
    def __enter__(self):
        """Context manager entry."""
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()