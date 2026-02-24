"""
Module for gathering PyPI package metadata using pip infrastructure.

This module uses pip's internal API to gather all information needed for
generating ebuild and Manifest files, including download URLs, checksums,
package metadata, and dependency information.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Set
from urllib.parse import urlparse

# Try to import pip's internal APIs
try:
    from pip._internal.index.api import PyPIRepository
    from pip._internal.models.index import PyPI
    from pip._internal.models.link import Link
    from pip._internal.network.session import PipSession
    from pip._internal.req import parse_requirements
    from pip._internal.req.constructors import install_req_from_line
    HAS_PIP_INTERNAL = True
except ImportError:
    HAS_PIP_INTERNAL = False

# Try to import requests for PyPI JSON API
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

logger = logging.getLogger(__name__)


class PyPIMetadataExtractor:
    """
    Extractor for PyPI package metadata using pip infrastructure.
    
    This class uses pip's internal APIs and PyPI's JSON API to gather
    comprehensive package information for ebuild generation.
    
    Examples:
        >>> extractor = PyPIMetadataExtractor()
        >>> isinstance(extractor.timeout, int)
        True
        >>> extractor.session_timeout >= 10
        True
    """
    
    def __init__(self, 
                 session_timeout: int = 30,
                 user_agent: str = "portage-pip-fuse/0.1.0",
                 cache_ttl: int = 3600,
                 cache_dir: Optional[str] = None):
        """
        Initialize the metadata extractor.
        
        Args:
            session_timeout: HTTP session timeout in seconds
            user_agent: User agent string for HTTP requests
            cache_ttl: Cache time-to-live in seconds (default: 1 hour)
            cache_dir: Directory for persistent cache (default: system temp)
        """
        self.timeout = session_timeout
        self.session_timeout = session_timeout  # For backwards compatibility
        self.user_agent = user_agent
        self.cache_ttl = cache_ttl
        
        # Set up cache directory
        if cache_dir is None:
            cache_dir = os.path.join(tempfile.gettempdir(), 'portage-pip-fuse-cache')
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # In-memory cache: package_name -> (data, timestamp)
        self._memory_cache: Dict[str, Tuple[dict, float]] = {}
        self._session = None
        
        logger.info(f"PyPI metadata cache initialized at {self.cache_dir}")
        
    def _get_cache_key(self, package_name: str, version: Optional[str] = None) -> str:
        """Generate cache key for package metadata."""
        if version:
            return f"{package_name.lower()}_{version}"
        return package_name.lower()
        
    def _get_cache_path(self, cache_key: str) -> Path:
        """Get filesystem path for cache key."""
        # Use first two characters for subdirectory to avoid too many files in one dir
        subdir = cache_key[:2] if len(cache_key) >= 2 else '00'
        cache_subdir = self.cache_dir / subdir
        cache_subdir.mkdir(exist_ok=True)
        return cache_subdir / f"{cache_key}.json"
        
    def _get_memory_cache(self, cache_key: str) -> Optional[dict]:
        """Get data from in-memory cache if valid."""
        if cache_key in self._memory_cache:
            data, timestamp = self._memory_cache[cache_key]
            if time.time() - timestamp < self.cache_ttl:
                return data
            else:
                # Expired, remove from memory cache
                del self._memory_cache[cache_key]
        return None
        
    def _set_memory_cache(self, cache_key: str, data: dict):
        """Store data in in-memory cache."""
        self._memory_cache[cache_key] = (data, time.time())
        
    def _get_disk_cache(self, cache_key: str) -> Optional[dict]:
        """Get data from disk cache if valid."""
        cache_path = self._get_cache_path(cache_key)
        
        if not cache_path.exists():
            return None
            
        try:
            # Check if cache is still valid
            if time.time() - cache_path.stat().st_mtime > self.cache_ttl:
                # Cache expired, remove file
                cache_path.unlink(missing_ok=True)
                return None
                
            # Load cached data
            with cache_path.open('r', encoding='utf-8') as f:
                data = json.load(f)
                
            # Also populate memory cache
            self._set_memory_cache(cache_key, data)
            return data
            
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.warning(f"Failed to load cache for {cache_key}: {e}")
            # Remove corrupted cache file
            cache_path.unlink(missing_ok=True)
            return None
            
    def _set_disk_cache(self, cache_key: str, data: dict):
        """Store data in disk cache."""
        cache_path = self._get_cache_path(cache_key)
        
        try:
            # Write to temporary file first, then rename for atomicity
            temp_path = cache_path.with_suffix('.tmp')
            with temp_path.open('w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            temp_path.rename(cache_path)
            
            # Also store in memory cache
            self._set_memory_cache(cache_key, data)
            
        except (OSError, TypeError) as e:
            logger.warning(f"Failed to cache data for {cache_key}: {e}")
            
    def _list_cached_packages(self) -> List[str]:
        """List all cached package names sorted by modification time (most recent first)."""
        try:
            package_names = set()
            
            # Walk through all subdirectories in cache
            for subdir in self.cache_dir.iterdir():
                if not subdir.is_dir():
                    continue
                    
                # Get all JSON cache files in this subdirectory
                cache_files = list(subdir.glob("*.json"))
                
                for cache_file in cache_files:
                    cache_key = cache_file.stem  # Remove .json extension
                    
                    # Extract package name from cache key (cache keys are "package_name" or "package_name_version")
                    # Look for patterns that suggest this is a versioned cache key
                    if '_' in cache_key:
                        parts = cache_key.split('_')
                        # Try to identify version-like patterns in the last parts
                        for i in range(len(parts)-1, 0, -1):
                            potential_version = '_'.join(parts[i:])
                            # Check if this looks like a version (contains digits and dots/dashes)
                            if any(c.isdigit() for c in potential_version) and any(c in potential_version for c in '.'):
                                package_name = '_'.join(parts[:i])
                                package_names.add(package_name)
                                break
                        else:
                            # No version pattern found, use the whole cache_key as package name
                            package_names.add(cache_key)
                    else:
                        # No underscore, must be just a package name
                        package_names.add(cache_key)
            
            return list(package_names)
            
        except Exception as e:
            logger.warning(f"Error listing cached packages: {e}")
            return []
            
    def _get_cached_data(self, package_name: str, version: Optional[str] = None) -> Optional[dict]:
        """Get cached data from memory or disk."""
        cache_key = self._get_cache_key(package_name, version)
        
        # Try memory cache first (fastest)
        data = self._get_memory_cache(cache_key)
        if data is not None:
            return data
            
        # Try disk cache
        return self._get_disk_cache(cache_key)
        
    def _cache_data(self, package_name: str, data: dict, version: Optional[str] = None):
        """Cache data to both memory and disk."""
        cache_key = self._get_cache_key(package_name, version)
        self._set_disk_cache(cache_key, data)
        
    def _get_session(self):
        """Get or create HTTP session for PyPI requests."""
        if self._session is None and HAS_PIP_INTERNAL:
            try:
                self._session = PipSession(timeout=self.timeout)
                self._session.headers.update({'User-Agent': self.user_agent})
            except Exception as e:
                logger.warning(f"Failed to create pip session: {e}")
        return self._session
    
    def get_package_json(self, package_name: str, 
                        version: Optional[str] = None) -> Optional[Dict]:
        """
        Get package metadata from PyPI JSON API with caching.
        
        Args:
            package_name: The PyPI package name
            version: Specific version, or None for latest
            
        Returns:
            Package metadata dictionary or None if not found
            
        Examples:
            >>> extractor = PyPIMetadataExtractor()
            >>> metadata = extractor.get_package_json("setuptools")
            >>> metadata is None or isinstance(metadata, dict)
            True
            >>> metadata = extractor.get_package_json("nonexistent-package-xyz")
            >>> metadata is None
            True
        """
        if not HAS_REQUESTS:
            logger.warning("requests library not available")
            return None
        
        # Create cache key
        cache_key = f"{package_name}_{version}" if version else package_name
        
        # Check memory cache first
        if cache_key in self._memory_cache:
            data, timestamp = self._memory_cache[cache_key]
            if time.time() - timestamp < self.cache_ttl:
                logger.debug(f"Using memory cached data for {cache_key}")
                return data
            else:
                del self._memory_cache[cache_key]
        
        # Check disk cache
        cache_file = self.cache_dir / f"{cache_key.replace('/', '_')}.json"
        if cache_file.exists():
            try:
                mtime = cache_file.stat().st_mtime
                if time.time() - mtime < self.cache_ttl:
                    with open(cache_file, 'r') as f:
                        data = json.load(f)
                    logger.debug(f"Using disk cached data for {cache_key}")
                    # Also cache in memory
                    self._memory_cache[cache_key] = (data, time.time())
                    return data
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to read cache file {cache_file}: {e}")
                # Continue to fetch from PyPI
            
        try:
            if version:
                url = f"https://pypi.org/pypi/{package_name}/{version}/json"
            else:
                url = f"https://pypi.org/pypi/{package_name}/json"
            
            response = requests.get(url, timeout=self.session_timeout)
            
            if response.status_code == 200:
                data = response.json()
                
                # Cache the result
                self._memory_cache[cache_key] = (data, time.time())
                
                # Save to disk cache
                try:
                    with open(cache_file, 'w') as f:
                        json.dump(data, f)
                    logger.debug(f"Cached {cache_key} to disk")
                except IOError as e:
                    logger.warning(f"Failed to write cache file {cache_file}: {e}")
                
                return data
            elif response.status_code == 404:
                logger.info(f"Package {package_name} not found on PyPI")
                # Cache the negative result to avoid repeated lookups
                self._memory_cache[cache_key] = (None, time.time())
                return None
            else:
                logger.warning(f"PyPI API returned status {response.status_code} for {package_name}")
                return None
                
        except requests.RequestException as e:
            logger.error(f"Failed to fetch PyPI metadata for {package_name}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching {package_name}: {e}")
            return None
    
    def extract_download_info(self, package_json: Dict) -> List[Dict[str, Any]]:
        """
        Extract download information from PyPI JSON metadata.
        
        Args:
            package_json: Package metadata from PyPI JSON API
            
        Returns:
            List of download info dictionaries
            
        Examples:
            >>> extractor = PyPIMetadataExtractor()
            >>> # Mock data structure similar to PyPI JSON
            >>> mock_json = {
            ...     'urls': [{
            ...         'filename': 'example-1.0.tar.gz',
            ...         'url': 'https://files.pythonhosted.org/example-1.0.tar.gz',
            ...         'size': 12345,
            ...         'packagetype': 'sdist',
            ...         'digests': {
            ...             'md5': 'abc123',
            ...             'sha256': 'def456'
            ...         }
            ...     }]
            ... }
            >>> downloads = extractor.extract_download_info(mock_json)
            >>> len(downloads)
            1
            >>> downloads[0]['filename']
            'example-1.0.tar.gz'
            >>> downloads[0]['size']
            12345
            >>> 'sha256' in downloads[0]['digests']
            True
        """
        if not package_json or 'urls' not in package_json:
            return []
        
        downloads = []
        for url_info in package_json['urls']:
            download_info = {
                'filename': url_info.get('filename', ''),
                'url': url_info.get('url', ''),
                'size': url_info.get('size', 0),
                'packagetype': url_info.get('packagetype', 'unknown'),
                'python_version': url_info.get('python_version', 'source'),
                'digests': url_info.get('digests', {}),
                'upload_time': url_info.get('upload_time_iso_8601', ''),
            }
            downloads.append(download_info)
        
        return downloads
    
    def get_source_distribution(self, downloads: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Find the source distribution (sdist) from download list.
        
        Args:
            downloads: List of download info dictionaries
            
        Returns:
            Source distribution info or None if not found
            
        Examples:
            >>> extractor = PyPIMetadataExtractor()
            >>> downloads = [
            ...     {'packagetype': 'bdist_wheel', 'filename': 'example-1.0-py3-none-any.whl'},
            ...     {'packagetype': 'sdist', 'filename': 'example-1.0.tar.gz', 'size': 12345}
            ... ]
            >>> sdist = extractor.get_source_distribution(downloads)
            >>> sdist['packagetype']
            'sdist'
            >>> sdist['filename']
            'example-1.0.tar.gz'
        """
        # Prefer .tar.gz files
        for download in downloads:
            if (download.get('packagetype') == 'sdist' and 
                download.get('filename', '').endswith('.tar.gz')):
                return download
        
        # Fall back to any sdist
        for download in downloads:
            if download.get('packagetype') == 'sdist':
                return download
        
        # Last resort: any download that looks like a source archive
        for download in downloads:
            filename = download.get('filename', '').lower()
            if any(filename.endswith(ext) for ext in ['.tar.gz', '.tar.bz2', '.tar.xz', '.zip']):
                return download
        
        return None
    
    def generate_manifest_entry(self, download_info: Dict[str, Any], 
                               wanted_hashes: Optional[List[str]] = None) -> str:
        """
        Generate a Gentoo Manifest DIST entry from download information.
        
        Uses the hash algorithms that PyPI actually provides: md5, sha256, blake2b_256.
        Note that modern Gentoo packages use BLAKE2B and SHA512, but PyPI doesn't
        provide SHA512, so we use what's available and let portage compute the rest.
        
        Args:
            download_info: Download information dictionary
            wanted_hashes: List of hash types to include (default: use PyPI available)
            
        Returns:
            Manifest DIST entry string
            
        Examples:
            >>> extractor = PyPIMetadataExtractor()
            >>> download_info = {
            ...     'filename': 'numpy-1.21.0.tar.gz',
            ...     'size': 10485760,
            ...     'digests': {
            ...         'md5': '1234567890abcdef1234567890abcdef',
            ...         'sha256': '3ffb289b9edc1cc4cdcb3f7b0ac5c1d8e8c2b0b1f1e0a1f1e0a1f1e0a1f1e0a1',
            ...         'blake2b_256': '5ffb289b9edc1cc4cdcb3f7b0ac5c1d8e8c2b0b1f1e0a1f1e0a1f1e0a1f1e0a1'
            ...     }
            ... }
            >>> entry = extractor.generate_manifest_entry(download_info)
            >>> entry.startswith('DIST numpy-1.21.0.tar.gz 10485760')
            True
            >>> 'MD5' in entry
            True
            >>> 'SHA256' in entry
            True
        """
        filename = download_info.get('filename', '')
        size = download_info.get('size', 0)
        digests = download_info.get('digests', {})
        
        # Start with DIST entry
        entry_parts = ['DIST', filename, str(size)]
        
        # PyPI provides these hash types (in this order for consistency)
        # We use what PyPI actually provides rather than what modern Gentoo prefers
        pypi_hash_order = [
            ('MD5', 'md5'),
            ('SHA256', 'sha256'), 
            ('BLAKE2B', 'blake2b_256'),  # PyPI's blake2b is 256-bit variant
        ]
        
        if wanted_hashes:
            # Use requested hashes if specified
            hash_mapping = {
                'BLAKE2B': 'blake2b_256',  # PyPI uses 256-bit variant
                'SHA256': 'sha256',
                'MD5': 'md5'
            }
            
            for hash_type in wanted_hashes:
                hash_key = hash_mapping.get(hash_type)
                if hash_key and hash_key in digests:
                    entry_parts.extend([hash_type, digests[hash_key]])
        else:
            # Use all available hashes from PyPI in standard order
            for gentoo_name, pypi_name in pypi_hash_order:
                if pypi_name in digests:
                    entry_parts.extend([gentoo_name, digests[pypi_name]])
        
        return ' '.join(entry_parts)
    
    def get_package_metadata(self, package_json: Dict) -> Dict[str, Any]:
        """
        Extract general package metadata for ebuild generation.
        
        Args:
            package_json: Package metadata from PyPI JSON API
            
        Returns:
            Dictionary with ebuild-relevant metadata
            
        Examples:
            >>> extractor = PyPIMetadataExtractor()
            >>> mock_json = {
            ...     'info': {
            ...         'name': 'example-package',
            ...         'version': '1.0.0',
            ...         'summary': 'An example package',
            ...         'description': 'A longer description',
            ...         'home_page': 'https://example.com',
            ...         'author': 'John Doe',
            ...         'author_email': 'john@example.com',
            ...         'license': 'MIT',
            ...         'keywords': 'example test',
            ...         'classifiers': [
            ...             'Development Status :: 4 - Beta',
            ...             'Programming Language :: Python :: 3'
            ...         ],
            ...         'requires_dist': [
            ...             'requests>=2.0.0',
            ...             'click; extra == "cli"'
            ...         ]
            ...     }
            ... }
            >>> metadata = extractor.get_package_metadata(mock_json)
            >>> metadata['name']
            'example-package'
            >>> metadata['version']
            '1.0.0'
            >>> 'requests' in str(metadata['dependencies'])
            True
            >>> len(metadata['classifiers']) >= 1
            True
        """
        if not package_json or 'info' not in package_json:
            return {}
        
        info = package_json['info']
        
        metadata = {
            'name': info.get('name', ''),
            'version': info.get('version', ''),
            'summary': info.get('summary', ''),
            'description': info.get('description', ''),
            'homepage': info.get('home_page') or info.get('project_url', ''),
            'author': info.get('author', ''),
            'author_email': info.get('author_email', ''),
            'maintainer': info.get('maintainer', ''),
            'maintainer_email': info.get('maintainer_email', ''),
            'license': info.get('license', ''),
            'keywords': info.get('keywords', ''),
            'classifiers': info.get('classifiers', []),
            'dependencies': info.get('requires_dist', []),
            'python_requires': info.get('requires_python', ''),
            'project_urls': info.get('project_urls', {}),
        }
        
        return metadata
    
    def extract_python_versions(self, classifiers: List[str]) -> List[str]:
        """
        Extract supported Python versions from classifiers.
        
        Args:
            classifiers: List of PyPI classifiers
            
        Returns:
            List of supported Python versions
            
        Examples:
            >>> extractor = PyPIMetadataExtractor()
            >>> classifiers = [
            ...     'Development Status :: 4 - Beta',
            ...     'Programming Language :: Python :: 3',
            ...     'Programming Language :: Python :: 3.8',
            ...     'Programming Language :: Python :: 3.9',
            ...     'Programming Language :: Python :: 3.10',
            ...     'Programming Language :: Python :: 3.11',
            ...     'Programming Language :: Python :: Implementation :: CPython'
            ... ]
            >>> versions = extractor.extract_python_versions(classifiers)
            >>> '3.8' in versions
            True
            >>> '3.9' in versions
            True
            >>> '3.10' in versions
            True
            >>> len(versions) >= 3
            True
        """
        versions = []
        for classifier in classifiers:
            if 'Programming Language :: Python ::' in classifier:
                parts = classifier.split('::')
                if len(parts) >= 3:
                    version_part = parts[2].strip()
                    # Match specific versions like 3.8, 3.9, etc.
                    if version_part.replace('.', '').isdigit():
                        versions.append(version_part)
        
        # Sort versions numerically
        versions.sort(key=lambda x: tuple(map(int, x.split('.'))))
        return versions
    
    def parse_dependencies(self, requires_dist: Optional[List[str]]) -> Tuple[List[str], List[str]]:
        """
        Parse requirements from requires_dist field.
        
        Args:
            requires_dist: List of requirement strings, or None
            
        Returns:
            Tuple of (runtime_dependencies, optional_dependencies)
            
        Examples:
            >>> extractor = PyPIMetadataExtractor()
            >>> requires_dist = [
            ...     'requests>=2.0.0',
            ...     'click>=7.0',
            ...     'pytest>=6.0; extra == "test"',
            ...     'sphinx; extra == "docs"',
            ...     'typing_extensions; python_version<"3.8"'
            ... ]
            >>> runtime, optional = extractor.parse_dependencies(requires_dist)
            >>> 'requests' in str(runtime)
            True
            >>> 'click' in str(runtime)
            True
            >>> any('pytest' in dep for dep in optional)
            True
            >>> len(runtime) >= 2
            True
            >>> # Test with None
            >>> runtime, optional = extractor.parse_dependencies(None)
            >>> len(runtime)
            0
            >>> len(optional)
            0
        """
        runtime_deps = []
        optional_deps = []
        
        if not requires_dist:
            return runtime_deps, optional_deps
        
        for req_str in requires_dist:
            # Simple parsing - just split on semicolon for extras/markers
            base_req = req_str.split(';')[0].strip()
            
            # Check if it has extras or conditional markers
            if ';' in req_str:
                marker = req_str.split(';', 1)[1].strip()
                if 'extra' in marker:
                    optional_deps.append(req_str)
                else:
                    # Conditional dependency (e.g., python version)
                    runtime_deps.append(req_str)
            else:
                # Unconditional runtime dependency
                runtime_deps.append(req_str)
        
        return runtime_deps, optional_deps
    
    def get_complete_package_info(self, package_name: str, 
                                 version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Get complete package information for ebuild generation.
        
        Args:
            package_name: PyPI package name
            version: Specific version, or None for latest
            
        Returns:
            Complete package information dictionary
            
        Examples:
            >>> extractor = PyPIMetadataExtractor()
            >>> # This will return None if network is unavailable
            >>> info = extractor.get_complete_package_info("setuptools")
            >>> info is None or isinstance(info, dict)
            True
            >>> # Test with non-existent package
            >>> info = extractor.get_complete_package_info("nonexistent-package-xyz-123")
            >>> info is None
            True
        """
        # Check cache first
        cached_data = self._get_cached_data(package_name, version)
        if cached_data is not None:
            logger.debug(f"Using cached data for {package_name} {version or 'latest'}")
            return cached_data
        
        # Get JSON metadata
        package_json = self.get_package_json(package_name, version)
        if not package_json:
            return None
        
        # Extract all information
        downloads = self.extract_download_info(package_json)
        metadata = self.get_package_metadata(package_json)
        sdist = self.get_source_distribution(downloads)
        
        # Parse Python versions and dependencies
        python_versions = self.extract_python_versions(metadata.get('classifiers', []))
        runtime_deps, optional_deps = self.parse_dependencies(metadata.get('dependencies', []))
        
        complete_info = {
            'metadata': metadata,
            'downloads': downloads,
            'source_distribution': sdist,
            'python_versions': python_versions,
            'runtime_dependencies': runtime_deps,
            'optional_dependencies': optional_deps,
        }
        
        # Generate Manifest entry if we have source distribution
        if sdist:
            manifest_entry = self.generate_manifest_entry(sdist)
            complete_info['manifest_entry'] = manifest_entry
        
        # Cache the complete information
        self._cache_data(package_name, complete_info, version)
        logger.debug(f"Cached metadata for {package_name} {version or 'latest'}")
        
        return complete_info


class EbuildDataExtractor:
    """
    Extract data specifically needed for Gentoo ebuild generation.
    
    This class processes PyPI metadata and formats it for ebuild templates.
    
    Examples:
        >>> extractor = EbuildDataExtractor()
        >>> isinstance(extractor, EbuildDataExtractor)
        True
    """
    
    def __init__(self):
        """Initialize the ebuild data extractor."""
        self.pypi_extractor = PyPIMetadataExtractor()
        
        # PyPI to Gentoo license mapping
        self.license_map = {
            'MIT': 'MIT',
            'MIT License': 'MIT', 
            'Apache-2.0': 'Apache-2.0',
            'Apache 2.0': 'Apache-2.0',
            'Apache License 2.0': 'Apache-2.0',
            'Apache Software License': 'Apache-2.0',
            'BSD': 'BSD',
            'BSD License': 'BSD',
            'BSD-2-Clause': 'BSD-2',
            'BSD-3-Clause': 'BSD',
            'GPL-2.0': 'GPL-2',
            'GPL-3.0': 'GPL-3', 
            'GPL-2.0+': 'GPL-2+',
            'GPL-3.0+': 'GPL-3+',
            'GNU General Public License v2': 'GPL-2+',
            'GNU General Public License v3': 'GPL-3+',
            'PSF-2.0': 'PSF-2',
            'Python Software Foundation License': 'PSF-2',
            'LGPL-2.1': 'LGPL-2.1',
            'LGPL-3.0': 'LGPL-3',
            'ISC': 'ISC',
            'MPL-2.0': 'MPL-2.0',
            'CC0-1.0': 'CC0-1.0',
            'Unlicense': 'Unlicense',
        }
    
    def _get_valid_python_impls(self) -> Set[str]:
        """Get valid Python implementations from _PYTHON_ALL_IMPLS."""
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
            cmd = f'EAPI=8 source {eclass_path} && echo "${{_PYTHON_ALL_IMPLS[@]}}"'
            result = subprocess.run(['bash', '-c', cmd], 
                                  capture_output=True, text=True, timeout=5)
            
            if result.returncode == 0 and result.stdout:
                impls = result.stdout.strip().split()
                return set(impls)
                
        except Exception as e:
            logger.warning(f"Could not read _PYTHON_ALL_IMPLS: {e}")
        
        # Fallback to hardcoded current values
        return {
            'pypy3_11',
            'python3_11', 'python3_12', 'python3_13', 'python3_14',
            'python3_13t', 'python3_14t'
        }
    
    def format_python_compat(self, python_versions: List[str]) -> str:
        """
        Format Python versions for PYTHON_COMPAT variable.
        
        Only includes Python versions explicitly supported by the package.
        Does NOT auto-extend to newer versions - that's wrong and can cause build failures.
        
        Args:
            python_versions: List of Python version strings from package metadata
            
        Returns:
            Formatted PYTHON_COMPAT list containing only supported versions
            
        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> versions = ['3.8', '3.9', '3.10']
            >>> compat = extractor.format_python_compat(versions)
            >>> compat
            ['python3_10', 'python3_8', 'python3_9']
            >>> 'python3_11' in compat  # Should not auto-extend
            False
        """
        if not python_versions:
            # Get system PYTHON_TARGETS as fallback
            try:
                import subprocess
                result = subprocess.run(['portageq', 'envvar', 'PYTHON_TARGETS'], 
                                      capture_output=True, text=True, check=True)
                system_targets = result.stdout.strip().split()
                # Convert to standard format (e.g., python3_11 -> 3.11)
                fallback_versions = []
                for target in system_targets:
                    if target.startswith('python3_'):
                        minor = target[8:]  # Remove 'python3_'
                        fallback_versions.append(f'3.{minor}')
                if fallback_versions:
                    python_versions = fallback_versions
                else:
                    # Ultimate fallback
                    python_versions = ['3.10', '3.11', '3.12']
            except Exception:
                # Ultimate fallback if portageq fails
                python_versions = ['3.10', '3.11', '3.12']
        
        # Get valid implementations from eclass
        valid_impls = self._get_valid_python_impls()
        compat_versions = []
        
        # Only include explicitly supported versions that are also in _PYTHON_ALL_IMPLS
        has_generic = False
        specific_versions = []
        
        for version in python_versions:
            if version == '3':
                has_generic = True
            elif '.' in version:
                major, minor = version.split('.', 1)
                if major == '3' and minor.isdigit():
                    version_str = f'python{major}_{minor}'
                    # Only include if it's in _PYTHON_ALL_IMPLS
                    if version_str in valid_impls:
                        if version_str not in compat_versions:
                            compat_versions.append(version_str)
                            specific_versions.append(int(minor))
                    else:
                        logger.debug(f"Skipping {version_str} - not in _PYTHON_ALL_IMPLS")
        
        # If we have generic '3' AND specific versions, only use the specific versions
        # If we have ONLY generic '3', use system targets that are valid
        if has_generic and not specific_versions:
            # Only generic "3" - use system targets that are in _PYTHON_ALL_IMPLS
            try:
                import subprocess
                result = subprocess.run(['portageq', 'envvar', 'PYTHON_TARGETS'], 
                                      capture_output=True, text=True, check=True)
                system_targets = result.stdout.strip().split()
                for target in system_targets:
                    if (target.startswith('python3_') and 
                        target in valid_impls and 
                        target not in compat_versions):
                        compat_versions.append(target)
            except Exception:
                # Fallback for generic "3" - use only valid current implementations
                fallback_impls = [impl for impl in valid_impls if impl.startswith('python3_')]
                compat_versions.extend(fallback_impls)
        # If we have both generic '3' and specific versions, ignore the generic '3'
        
        # Remove duplicates and sort
        return sorted(list(set(compat_versions)))
    
    def translate_license(self, pypi_license: str) -> str:
        """
        Translate PyPI license string to Gentoo license format.
        
        Args:
            pypi_license: License string from PyPI metadata
            
        Returns:
            Gentoo-compatible license string
            
        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> extractor.translate_license('MIT')
            'MIT'
            >>> extractor.translate_license('Apache-2.0')
            'Apache-2.0'
            >>> extractor.translate_license('Unknown License')
            'all-rights-reserved'
            >>> extractor.translate_license('')
            'all-rights-reserved'
            >>> extractor.translate_license('BSD-3-Clause')
            'BSD'
            >>> extractor.translate_license('BSD-2-Clause')
            'BSD-2'
            >>> extractor.translate_license('GNU General Public License v3')
            'GPL-3+'
            >>> extractor.translate_license('python software foundation')
            'PSF-2'
            >>> extractor.translate_license('GPL v2 or later')
            'GPL-2+'
            >>> extractor.translate_license('some weird mit license')
            'MIT'
        """
        if not pypi_license:
            return 'all-rights-reserved'
            
        # Direct mapping
        if pypi_license in self.license_map:
            return self.license_map[pypi_license]
        
        # Case-insensitive partial matching for common cases
        pypi_lower = pypi_license.lower()
        
        if 'mit' in pypi_lower:
            return 'MIT'
        elif 'apache' in pypi_lower and '2' in pypi_lower:
            return 'Apache-2.0'
        elif 'bsd' in pypi_lower:
            if '2' in pypi_lower:
                return 'BSD-2'
            else:
                return 'BSD'
        elif 'gpl' in pypi_lower:
            if '3' in pypi_lower:
                return 'GPL-3+' if '+' in pypi_lower or 'later' in pypi_lower else 'GPL-3'
            elif '2' in pypi_lower:
                return 'GPL-2+' if '+' in pypi_lower or 'later' in pypi_lower else 'GPL-2'
            else:
                return 'GPL-3+'  # Default to GPL-3+ for unspecified GPL
        elif 'lgpl' in pypi_lower:
            if '2.1' in pypi_lower:
                return 'LGPL-2.1'
            elif '3' in pypi_lower:
                return 'LGPL-3'
            else:
                return 'LGPL-2.1'  # Default to 2.1
        elif 'python' in pypi_lower or 'psf' in pypi_lower:
            return 'PSF-2'
        elif 'isc' in pypi_lower:
            return 'ISC'
        elif 'mozilla' in pypi_lower or 'mpl' in pypi_lower:
            return 'MPL-2.0'
        elif 'unlicense' in pypi_lower:
            return 'Unlicense'
        elif 'cc0' in pypi_lower:
            return 'CC0-1.0'
        else:
            # Unknown license - use all-rights-reserved per Gentoo policy
            return 'all-rights-reserved'
    
    def format_dependencies(self, dependencies: List[str]) -> List[str]:
        """
        Format Python dependencies for ebuild DEPEND/RDEPEND.
        
        Args:
            dependencies: List of requirement strings
            
        Returns:
            List of formatted Gentoo dependencies
            
        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> deps = ['requests>=2.0.0', 'click>=7.0']
            >>> formatted = extractor.format_dependencies(deps)
            >>> any('dev-python/requests' in dep for dep in formatted)
            True
            >>> any('dev-python/click' in dep for dep in formatted)
            True
        """
        from portage_pip_fuse.name_translator import default_translator
        
        formatted_deps = []
        
        for dep_str in dependencies:
            # Parse dependency using pip's requirement parsing
            try:
                from pip._vendor.packaging.requirements import Requirement
                req = Requirement(dep_str.strip())
                package_name = req.name
                specifiers = req.specifier
            except ImportError:
                # Fallback to packaging library
                try:
                    from packaging.requirements import Requirement
                    req = Requirement(dep_str.strip())
                    package_name = req.name
                    specifiers = req.specifier
                except ImportError:
                    # Last resort: manual parsing
                    match = re.match(r'^([a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]|[a-zA-Z0-9])', dep_str.strip())
                    package_name = match.group(1) if match else dep_str.split()[0]
                    specifiers = None
            
            # Translate to Gentoo name and add version specifiers
            gentoo_name = default_translator.pypi_to_gentoo(package_name)
            if specifiers:
                gentoo_dep = self._format_gentoo_dependency(gentoo_name, specifiers)
            else:
                gentoo_dep = f"dev-python/{gentoo_name}"
            
            # TODO: Add version constraints parsing
            formatted_deps.append(gentoo_dep)
        
        return formatted_deps
        
    def extract_extras_as_use_flags(self, optional_dependencies: List[str]) -> Tuple[List[str], Dict[str, List[str]]]:
        """
        Extract PyPI extras and convert them to Gentoo USE flags.
        
        Args:
            optional_dependencies: List of optional requirement strings with extras
            
        Returns:
            Tuple of (IUSE flags, OPTIONAL_DEPEND dict mapping USE flag to dependencies)
            
        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> optional_deps = [
            ...     'pytest>=6.0; extra == "test"',
            ...     'sphinx>=4.0; extra == "docs"',
            ...     'requests-mock; extra == "test"'
            ... ]
            >>> iuse, optional_depend = extractor.extract_extras_as_use_flags(optional_deps)
            >>> 'test' in iuse
            True
            >>> 'docs' in iuse
            True
            >>> 'test' in optional_depend
            True
            >>> len(optional_depend['test']) >= 2  # pytest and requests-mock
            True
        """
        from portage_pip_fuse.name_translator import default_translator
        
        iuse_flags = set()
        optional_depend = {}
        
        for dep_str in optional_dependencies:
            if '; extra ==' not in dep_str:
                continue
                
            # Split requirement from extra condition
            base_req, condition = dep_str.split('; extra ==', 1)
            base_req = base_req.strip()
            
            # Extract extra name, removing quotes
            extra_name = condition.strip().strip('\"').strip("'")
            
            # Convert extra name to valid USE flag (lowercase, replace special chars)
            use_flag = re.sub(r'[^a-z0-9_]', '_', extra_name.lower().replace('-', '_'))
            
            # Parse dependency using pip's requirement parsing
            try:
                from pip._vendor.packaging.requirements import Requirement
                req = Requirement(base_req.strip())
                package_name = req.name
                specifiers = req.specifier
            except ImportError:
                # Fallback to packaging library
                try:
                    from packaging.requirements import Requirement
                    req = Requirement(base_req.strip())
                    package_name = req.name
                    specifiers = req.specifier
                except ImportError:
                    # Last resort: manual parsing
                    match = re.match(r'^([a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]|[a-zA-Z0-9])', base_req.strip())
                    package_name = match.group(1) if match else base_req.split()[0]
                    specifiers = None
            
            # Translate to Gentoo name and add version specifiers
            gentoo_name = default_translator.pypi_to_gentoo(package_name)
            if specifiers:
                gentoo_dep = self._format_gentoo_dependency(gentoo_name, specifiers)
            else:
                gentoo_dep = f"dev-python/{gentoo_name}"
            
            # Add to collections
            iuse_flags.add(use_flag)
            if use_flag not in optional_depend:
                optional_depend[use_flag] = []
            optional_depend[use_flag].append(gentoo_dep)
        
        return sorted(list(iuse_flags)), optional_depend
    
    def _format_gentoo_dependency(self, gentoo_name: str, specifiers) -> str:
        """
        Format a Gentoo dependency string with version specifiers.
        
        Args:
            gentoo_name: The Gentoo package name
            specifiers: Packaging specifiers from Requirement object
            
        Returns:
            Formatted Gentoo dependency string
            
        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> # Test by parsing actual requirement strings
            >>> try:
            ...     from pip._vendor.packaging.requirements import Requirement
            ... except ImportError:
            ...     from packaging.requirements import Requirement
            >>> # Test ~= compatible release operator (PEP 440)
            >>> req = Requirement('requests~=1.4')
            >>> result = extractor._format_gentoo_dependency('requests', req.specifier)
            >>> '>=dev-python/requests-1.4' in result
            True
            >>> '<dev-python/requests-2' in result
            True
            >>> # Test ~= with patch version
            >>> req = Requirement('requests~=1.4.2')
            >>> result = extractor._format_gentoo_dependency('requests', req.specifier)
            >>> '>=dev-python/requests-1.4.2' in result
            True
            >>> '<dev-python/requests-1.5' in result
            True
            >>> # Test simple >= operator
            >>> req = Requirement('requests>=2.0.0')
            >>> extractor._format_gentoo_dependency('requests', req.specifier)
            '>=dev-python/requests-2.0.0'
        """
        if not specifiers:
            return f"dev-python/{gentoo_name}"
        
        # Convert PyPI version specifiers to Gentoo format
        dep_parts = []
        for spec in specifiers:
            operator = spec.operator
            version = spec.version
            
            # Translate operators to Gentoo format
            if operator == '==':
                dep_parts.append(f"=dev-python/{gentoo_name}-{version}")
            elif operator == '>=':
                dep_parts.append(f">=dev-python/{gentoo_name}-{version}")
            elif operator == '>':
                dep_parts.append(f">dev-python/{gentoo_name}-{version}")
            elif operator == '<=':
                dep_parts.append(f"<=dev-python/{gentoo_name}-{version}")
            elif operator == '<':
                dep_parts.append(f"<dev-python/{gentoo_name}-{version}")
            elif operator == '!=':
                dep_parts.append(f"!dev-python/{gentoo_name}-{version}")
            elif operator == '~=':
                # Compatible release per PEP 440: ~=1.4 means >=1.4, ==1.*
                # ~=1.4.5 means >=1.4.5, ==1.4.*
                # We need to create the upper bound by incrementing the last segment
                version_parts = version.split('.')
                if len(version_parts) >= 2:
                    # Create upper bound by incrementing the second-to-last segment
                    upper_parts = version_parts[:-1]  # Remove last segment
                    try:
                        # Increment the last remaining segment
                        upper_parts[-1] = str(int(upper_parts[-1]) + 1)
                        upper_version = '.'.join(upper_parts)
                        dep_parts.append(f">=dev-python/{gentoo_name}-{version}")
                        dep_parts.append(f"<dev-python/{gentoo_name}-{upper_version}")
                    except ValueError:
                        # If conversion fails, fall back to just >=
                        dep_parts.append(f">=dev-python/{gentoo_name}-{version}")
                else:
                    # Single segment - not valid per PEP 440, but handle gracefully
                    dep_parts.append(f">=dev-python/{gentoo_name}-{version}")
        
        # For multiple specifiers, we need to use Gentoo's syntax
        if len(dep_parts) == 1:
            return dep_parts[0]
        else:
            # Multiple constraints - use space separation (Gentoo handles this)
            return ' '.join(dep_parts)
    
    def prepare_ebuild_data(self, package_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Prepare complete data dictionary for ebuild generation.
        
        Args:
            package_info: Complete package information
            
        Returns:
            Data dictionary ready for ebuild template substitution
            
        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> package_info = {
            ...     'metadata': {
            ...         'name': 'example-package',
            ...         'version': '1.0.0',
            ...         'summary': 'An example',
            ...         'homepage': 'https://example.com',
            ...         'license': 'MIT'
            ...     },
            ...     'python_versions': ['3.8', '3.9', '3.10'],
            ...     'runtime_dependencies': ['requests>=2.0'],
            ...     'source_distribution': {
            ...         'url': 'https://pypi.org/example-1.0.tar.gz'
            ...     }
            ... }
            >>> ebuild_data = extractor.prepare_ebuild_data(package_info)
            >>> ebuild_data['PN']
            'example-package'
            >>> ebuild_data['PV']
            '1.0.0'
            >>> 'python3_8' in ebuild_data['PYTHON_COMPAT']
            True
            >>> ebuild_data['PYPI_PN']
            'example-package'
            >>> ebuild_data['PYPI_PV']
            '1.0.0'
            >>> ebuild_data['LICENSE']
            'MIT'
        """
        if package_info is None:
            logger.error("Cannot prepare ebuild data: package_info is None")
            return {}
        
        metadata = package_info.get('metadata', {})
        
        ebuild_data = {
            # Basic package information
            'PN': metadata.get('name', ''),
            'PV': metadata.get('version', ''),
            'DESCRIPTION': metadata.get('summary', ''),
            'HOMEPAGE': metadata.get('homepage', ''),
            'LICENSE': self.translate_license(metadata.get('license', '')),
            
            # PyPI eclass variables
            'PYPI_PN': metadata.get('name', ''),
            'PYPI_PV': metadata.get('version', ''),
            
            # Python compatibility
            'PYTHON_COMPAT': self.format_python_compat(
                package_info.get('python_versions', [])
            ),
            
            # Dependencies
            'DEPEND': self.format_dependencies(
                package_info.get('runtime_dependencies', [])
            ),
            'RDEPEND': self.format_dependencies(
                package_info.get('runtime_dependencies', [])
            ),
            
            # Source information
            'SRC_URI': (package_info.get('source_distribution') or {}).get('url', ''),
            
            # Additional metadata
            'KEYWORDS': 'amd64 x86',  # Stable keywords - PyPI packages are generally stable releases
            'SLOT': '0',
        }
        
        # Handle PyPI extras as USE flags
        optional_deps = package_info.get('optional_dependencies', [])
        if optional_deps:
            iuse_flags, optional_depend = self.extract_extras_as_use_flags(optional_deps)
            ebuild_data['IUSE'] = iuse_flags
            ebuild_data['OPTIONAL_DEPEND'] = optional_depend
        else:
            ebuild_data['IUSE'] = []
        
        return ebuild_data


# Convenience functions
def get_package_info(package_name: str, version: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Convenience function to get complete package information.
    
    Args:
        package_name: PyPI package name
        version: Specific version, or None for latest
        
    Returns:
        Complete package information or None
        
    Examples:
        >>> info = get_package_info("setuptools")
        >>> info is None or isinstance(info, dict)
        True
    """
    extractor = PyPIMetadataExtractor()
    return extractor.get_complete_package_info(package_name, version)


def generate_manifest_dist(package_name: str, version: Optional[str] = None) -> Optional[str]:
    """
    Generate Manifest DIST entry for a PyPI package.
    
    Args:
        package_name: PyPI package name  
        version: Specific version, or None for latest
        
    Returns:
        Manifest DIST entry string or None
        
    Examples:
        >>> dist_entry = generate_manifest_dist("setuptools")
        >>> dist_entry is None or dist_entry.startswith('DIST')
        True
    """
    info = get_package_info(package_name, version)
    if info and 'manifest_entry' in info:
        return info['manifest_entry']
    return None


if __name__ == "__main__":
    # Example usage with numpy
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    if len(sys.argv) > 1:
        package_name = sys.argv[1]
        version = sys.argv[2] if len(sys.argv) > 2 else None
    else:
        package_name = "numpy"
        version = None
    
    print(f"Gathering information for {package_name}" + 
          (f" version {version}" if version else " (latest)"))
    
    extractor = PyPIMetadataExtractor()
    info = extractor.get_complete_package_info(package_name, version)
    
    if not info:
        print(f"Failed to get information for {package_name}")
        sys.exit(1)
    
    print(f"\n=== Package Metadata ===")
    metadata = info['metadata']
    print(f"Name: {metadata['name']}")
    print(f"Version: {metadata['version']}")  
    print(f"Summary: {metadata['summary']}")
    print(f"Homepage: {metadata['homepage']}")
    print(f"License: {metadata['license']}")
    
    print(f"\n=== Python Versions ===")
    for version in info['python_versions']:
        print(f"  Python {version}")
    
    print(f"\n=== Dependencies ({len(info['runtime_dependencies'])}) ===")
    for dep in info['runtime_dependencies'][:5]:  # Show first 5
        print(f"  {dep}")
    if len(info['runtime_dependencies']) > 5:
        print(f"  ... and {len(info['runtime_dependencies']) - 5} more")
    
    print(f"\n=== Source Distribution ===")
    if info['source_distribution']:
        sdist = info['source_distribution']
        print(f"Filename: {sdist['filename']}")
        print(f"Size: {sdist['size']:,} bytes")
        print(f"URL: {sdist['url']}")
    
    print(f"\n=== Manifest Entry ===")
    if 'manifest_entry' in info:
        print(info['manifest_entry'])
    
    print(f"\n=== Ebuild Data ===")
    ebuild_extractor = EbuildDataExtractor()
    ebuild_data = ebuild_extractor.prepare_ebuild_data(info)
    for key, value in ebuild_data.items():
        if isinstance(value, list):
            print(f"{key}=({' '.join(value)})")
        else:
            print(f"{key}={value}")