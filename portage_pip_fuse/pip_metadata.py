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

# Try to import packaging for version specifier parsing
try:
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
    HAS_PACKAGING = True
except ImportError:
    HAS_PACKAGING = False

from portage_pip_fuse.constants import find_cache_dir, HTTP_TIMEOUT

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
        
        # Set up cache directory using find_cache_dir() for consistent location
        self.cache_dir = find_cache_dir(cache_dir)
        
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
            
            response = requests.get(url, timeout=HTTP_TIMEOUT)
            
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

    def get_wheel_distribution(self, downloads: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """
        Find a pure-Python wheel from download list.

        Only returns wheels tagged 'py3-none-any' or 'py2.py3-none-any' which
        are pure Python wheels with no platform dependencies. These are usable
        on Gentoo since they don't require pre-built binary extensions.

        Args:
            downloads: List of download info dictionaries

        Returns:
            Wheel distribution info or None if no suitable wheel found

        Examples:
            >>> extractor = PyPIMetadataExtractor()
            >>> downloads = [
            ...     {'packagetype': 'bdist_wheel', 'filename': 'example-1.0-py3-none-any.whl'},
            ...     {'packagetype': 'sdist', 'filename': 'example-1.0.tar.gz'}
            ... ]
            >>> wheel = extractor.get_wheel_distribution(downloads)
            >>> wheel['filename']
            'example-1.0-py3-none-any.whl'
            >>> # Test preference for py3-none-any over py2.py3-none-any
            >>> downloads = [
            ...     {'packagetype': 'bdist_wheel', 'filename': 'example-1.0-py2.py3-none-any.whl'},
            ...     {'packagetype': 'bdist_wheel', 'filename': 'example-1.0-py3-none-any.whl'}
            ... ]
            >>> wheel = extractor.get_wheel_distribution(downloads)
            >>> wheel['filename']
            'example-1.0-py3-none-any.whl'
            >>> # Platform-specific wheels should not be returned
            >>> downloads = [
            ...     {'packagetype': 'bdist_wheel', 'filename': 'example-1.0-cp311-cp311-manylinux_2_17_x86_64.whl'}
            ... ]
            >>> extractor.get_wheel_distribution(downloads) is None
            True
        """
        # Prefer py3-none-any (pure Python 3 only) over py2.py3-none-any
        for download in downloads:
            if download.get('packagetype') == 'bdist_wheel':
                filename = download.get('filename', '')
                if filename.endswith('-py3-none-any.whl'):
                    return download

        # Fall back to py2.py3-none-any
        for download in downloads:
            if download.get('packagetype') == 'bdist_wheel':
                filename = download.get('filename', '')
                if filename.endswith('-py2.py3-none-any.whl'):
                    return download

        return None

    def _normalize_sdist_filename(self, filename: str) -> str:
        """
        Normalize sdist filename according to PEP 625 rules.

        The pypi.eclass normalizes project names in sdist filenames:
        - Replace any run of [-._]+ with a single underscore
        - Convert to lowercase

        For example: MarkupSafe-2.1.2.tar.gz -> markupsafe-2.1.2.tar.gz

        Args:
            filename: Original PyPI filename

        Returns:
            Normalized filename matching what pypi.eclass downloads

        Examples:
            >>> extractor = PyPIMetadataExtractor()
            >>> extractor._normalize_sdist_filename('MarkupSafe-2.1.2.tar.gz')
            'markupsafe-2.1.2.tar.gz'
            >>> extractor._normalize_sdist_filename('Jinja2-3.1.2.tar.gz')
            'jinja2-3.1.2.tar.gz'
            >>> extractor._normalize_sdist_filename('zope.interface-5.4.0.tar.gz')
            'zope_interface-5.4.0.tar.gz'
            >>> extractor._normalize_sdist_filename('my--pkg-1.0.tar.gz')
            'my_pkg-1.0.tar.gz'
            >>> extractor._normalize_sdist_filename('numpy-1.21.0.tar.gz')
            'numpy-1.21.0.tar.gz'
        """
        # Match pattern: {name}-{version}.{suffix}
        # Version starts after a hyphen followed by a digit
        match = re.search(r'-(\d)', filename)
        if not match:
            return filename  # Can't parse, return as-is

        name_end = match.start()
        name_part = filename[:name_end]
        rest = filename[name_end:]  # includes the hyphen

        # Normalize the name part according to PEP 625
        # Replace runs of [-._] with underscore, then lowercase
        normalized_name = re.sub(r'[-._]+', '_', name_part).lower()

        return normalized_name + rest

    def generate_manifest_entry(self, download_info: Dict[str, Any],
                               wanted_hashes: Optional[List[str]] = None) -> str:
        """
        Generate a Gentoo Manifest DIST entry from download information.

        Uses hash algorithms that PyPI provides: md5, sha256.

        Note on BLAKE2B: Modern Gentoo uses BLAKE2B (512-bit output, 128 hex chars),
        but PyPI only provides blake2b_256 (256-bit output, 64 hex chars). These are
        NOT compatible - different output sizes from the same algorithm. We cannot
        use PyPI's blake2b_256 as Gentoo's BLAKE2B. SHA256 is sufficient for
        portage verification.

        The filename is normalized according to PEP 625 to match what pypi.eclass
        downloads (e.g., MarkupSafe-2.1.2.tar.gz -> markupsafe-2.1.2.tar.gz).

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
            >>> 'SHA256' in entry
            True
            >>> # Test with mixed-case filename
            >>> download_info['filename'] = 'MarkupSafe-2.1.2.tar.gz'
            >>> entry = extractor.generate_manifest_entry(download_info)
            >>> entry.startswith('DIST markupsafe-2.1.2.tar.gz')
            True
        """
        filename = download_info.get('filename', '')
        # Use original filename - pypi.eclass will use PYPI_NO_NORMALIZE
        # to match. PyPI doesn't normalize filenames for older uploads.
        size = download_info.get('size', 0)
        digests = download_info.get('digests', {})

        # Start with DIST entry
        entry_parts = ['DIST', filename, str(size)]

        # PyPI provides md5, sha256, and blake2b_256.
        # We can only use md5 and sha256 because:
        # - Gentoo BLAKE2B = 512-bit output (128 hex chars)
        # - PyPI blake2b_256 = 256-bit output (64 hex chars)
        # These are incompatible - different output sizes.
        pypi_hash_order = [
            ('SHA256', 'sha256'),
            ('MD5', 'md5'),
        ]

        if wanted_hashes:
            # Use requested hashes if specified
            hash_mapping = {
                'SHA256': 'sha256',
                'MD5': 'md5',
                # BLAKE2B intentionally not mapped - PyPI's blake2b_256 is incompatible
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
            'license_expression': info.get('license_expression', ''),
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
                    # Match specific versions like 3.8, 3.9, etc. (must have a dot)
                    # Skip generic versions like '3' - we need specific minor versions
                    if '.' in version_part and version_part.replace('.', '').isdigit():
                        versions.append(version_part)

        # Sort versions numerically
        versions.sort(key=lambda x: tuple(map(int, x.split('.'))))
        return versions

    def parse_requires_python(self, requires_python: Optional[str]) -> List[str]:
        """
        Parse requires_python specifier and expand to matching Python versions.

        This handles version specifiers like '>=3.10', '>=3.8,<4', '~=3.9' and
        expands them to all Python versions that match the specifier.

        Args:
            requires_python: Version specifier string (e.g., '>=3.10.0')

        Returns:
            List of Python version strings that satisfy the specifier

        Examples:
            >>> extractor = PyPIMetadataExtractor()
            >>> versions = extractor.parse_requires_python('>=3.10')
            >>> '3.10' in versions
            True
            >>> '3.11' in versions
            True
            >>> '3.9' in versions
            False
            >>> versions = extractor.parse_requires_python('>=3.8,<3.11')
            >>> '3.8' in versions
            True
            >>> '3.10' in versions
            True
            >>> '3.11' in versions
            False
        """
        if not requires_python:
            return []

        # Get Python versions dynamically from _PYTHON_ALL_IMPLS
        # This ensures we always check against current Gentoo-supported versions
        valid_impls = EbuildDataExtractor._get_valid_python_impls()
        all_python_versions = []
        for impl in sorted(valid_impls):
            if impl.startswith('python3_'):
                minor = impl[8:]  # Remove 'python3_'
                if minor.isdigit():  # Skip free-threading variants like '13t'
                    all_python_versions.append(f'3.{minor}')
        # Fallback if we couldn't get impls
        if not all_python_versions:
            all_python_versions = ['3.11', '3.12', '3.13', '3.14']

        if HAS_PACKAGING:
            try:
                spec = SpecifierSet(requires_python)
                matching = []
                for version in all_python_versions:
                    if Version(version) in spec:
                        matching.append(version)
                return matching
            except Exception as e:
                logger.debug(f"Failed to parse requires_python '{requires_python}': {e}")
                # Fall through to simple parser

        # Simple fallback parser for common patterns
        requires_python = requires_python.strip()

        # Handle >=X.Y or >=X.Y.Z
        ge_match = re.match(r'>=\s*(\d+)\.(\d+)', requires_python)
        if ge_match:
            min_major = int(ge_match.group(1))
            min_minor = int(ge_match.group(2))

            # Check for upper bound (<X.Y or <X)
            lt_match = re.search(r'<\s*(\d+)(?:\.(\d+))?', requires_python)
            max_major = 99
            max_minor = 99
            if lt_match:
                max_major = int(lt_match.group(1))
                max_minor = int(lt_match.group(2)) if lt_match.group(2) else 0

            matching = []
            for version in all_python_versions:
                major, minor = map(int, version.split('.'))
                # Check lower bound
                if (major, minor) < (min_major, min_minor):
                    continue
                # Check upper bound (< means strictly less than)
                if (major, minor) >= (max_major, max_minor):
                    continue
                matching.append(version)
            return matching

        # Handle just "3" (any Python 3)
        if requires_python == '3':
            return all_python_versions

        logger.debug(f"Could not parse requires_python: {requires_python}")
        return []

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

        # Try sdist first, fall back to pure-Python wheel
        sdist = self.get_source_distribution(downloads)
        wheel_dist = None
        use_wheel = False

        if sdist:
            dist_info = sdist
        else:
            # Fall back to pure-Python wheel
            wheel_dist = self.get_wheel_distribution(downloads)
            if wheel_dist:
                dist_info = wheel_dist
                use_wheel = True
                logger.debug(f"Using wheel for {package_name}: {wheel_dist.get('filename')}")
            else:
                # No usable distribution - package will not be visible
                dist_info = None

        # Check for git repository URL when no sdist available
        git_repo_url = None
        if not sdist:
            try:
                from portage_pip_fuse.git_provider import extract_git_url
                project_urls = metadata.get('project_urls', {})
                git_repo_url = extract_git_url(project_urls)
                if git_repo_url:
                    logger.debug(f"Found git repository for {package_name}: {git_repo_url}")
            except ImportError:
                pass  # git_provider not available

        # Parse Python versions from classifiers first
        classifier_versions = self.extract_python_versions(metadata.get('classifiers', []))

        # Parse requires_python specifier if available
        # Note: get_package_metadata stores this as 'python_requires'
        requires_python = metadata.get('python_requires', '')
        specifier_versions = self.parse_requires_python(requires_python) if requires_python else []

        # Determine Python versions:
        # - requires_python is authoritative when present (e.g., >=3.9 means 3.9+)
        # - classifiers are often outdated and shouldn't cap requires_python
        # - Only use classifiers to cap if requires_python has explicit upper bound
        specific_classifiers = [v for v in classifier_versions if v != '3']

        if specifier_versions:
            # Trust requires_python - it's the authoritative source
            # Classifiers often lag behind (packages don't update for each Python release)
            has_upper_bound = '<' in requires_python if requires_python else False
            if has_upper_bound and specific_classifiers:
                # Only cap if requires_python explicitly has an upper bound
                # AND classifiers provide a lower max (rare case)
                max_classifier = max(specific_classifiers, key=lambda v: tuple(map(int, v.split('.'))))
                max_specifier = max(specifier_versions, key=lambda v: tuple(map(int, v.split('.'))))
                if tuple(map(int, max_classifier.split('.'))) < tuple(map(int, max_specifier.split('.'))):
                    python_versions = [v for v in specifier_versions
                                       if tuple(map(int, v.split('.'))) <= tuple(map(int, max_classifier.split('.')))]
                    logger.debug(f"Capping requires_python '{requires_python}' at classifier max {max_classifier} -> {python_versions}")
                else:
                    python_versions = specifier_versions
                    logger.debug(f"Using requires_python '{requires_python}' -> {python_versions}")
            else:
                python_versions = specifier_versions
                logger.debug(f"Using requires_python '{requires_python}' -> {python_versions}")
        elif specific_classifiers:
            python_versions = specific_classifiers
            logger.debug(f"Using classifiers -> {python_versions}")
        else:
            python_versions = []

        runtime_deps, optional_deps = self.parse_dependencies(metadata.get('dependencies', []))

        complete_info = {
            'metadata': metadata,
            'downloads': downloads,
            'source_distribution': sdist,
            'wheel_distribution': wheel_dist,
            'use_wheel': use_wheel,
            'git_repo_url': git_repo_url,
            'python_versions': python_versions,
            'runtime_dependencies': runtime_deps,
            'optional_dependencies': optional_deps,
        }

        # Generate Manifest entry for whichever distribution we have
        if dist_info:
            manifest_entry = self.generate_manifest_entry(dist_info)
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
    
    # Class-level cache for _PYTHON_ALL_IMPLS (shared across all instances)
    _cached_python_impls = None
    _cache_timestamp = 0
    _cache_ttl = 3600  # Cache for 1 hour
    
    def __init__(self, cache_dir: Optional[str] = None, name_translation_store=None):
        """Initialize the ebuild data extractor.

        Args:
            cache_dir: Directory for caching PyPI metadata
            name_translation_store: Optional NameTranslationPatchStore for custom
                PyPI-to-Gentoo package name mappings (e.g., torch -> sci-ml/pytorch)
        """
        self.pypi_extractor = PyPIMetadataExtractor(cache_dir=cache_dir)
        self.name_translation_store = name_translation_store

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

    def _get_gentoo_atom(self, pypi_name: str, gentoo_name: str) -> str:
        """
        Get the full Gentoo atom (category/package) for a PyPI package.

        Checks for custom name translation mappings first, then falls back
        to the default dev-python category.

        Args:
            pypi_name: Original PyPI package name (for lookup in name_translation_store)
            gentoo_name: Translated Gentoo package name (used as fallback)

        Returns:
            Full Gentoo atom like 'sci-ml/pytorch' or 'dev-python/requests'

        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> extractor._get_gentoo_atom('requests', 'requests')
            'dev-python/requests'
        """
        # Check for custom mapping
        if self.name_translation_store is not None:
            custom_atom = self.name_translation_store.get_mapping(pypi_name)
            if custom_atom:
                return custom_atom

        # Default to dev-python category
        return f"dev-python/{gentoo_name}"

    @classmethod
    def _get_valid_python_impls(cls) -> Set[str]:
        """
        Get valid Python implementations from _PYTHON_ALL_IMPLS.
        
        This is a class method with caching to avoid repeatedly reading the eclass.
        The cache is shared across all instances and refreshed every hour.
        """
        import time
        current_time = time.time()
        
        # Check if cache is still valid
        if (cls._cached_python_impls is not None and 
            current_time - cls._cache_timestamp < cls._cache_ttl):
            return cls._cached_python_impls
        
        # Cache miss or expired - read from eclass
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
                impls = set(result.stdout.strip().split())
                # Update cache
                cls._cached_python_impls = impls
                cls._cache_timestamp = current_time
                logger.debug(f"Refreshed _PYTHON_ALL_IMPLS cache: {sorted(impls)}")
                return impls
                
        except Exception as e:
            logger.warning(f"Could not read _PYTHON_ALL_IMPLS: {e}")
        
        # Fallback to hardcoded current values
        fallback = {
            'pypy3_11',
            'python3_11', 'python3_12', 'python3_13', 'python3_14',
            'python3_13t', 'python3_14t'
        }
        # Cache the fallback too
        cls._cached_python_impls = fallback
        cls._cache_timestamp = current_time
        return fallback
    
    # Class-level cache for format_python_compat results (shared across instances)
    _compat_cache = {}
    
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
            >>> versions = ['3.11', '3.12', '3.13']
            >>> compat = extractor.format_python_compat(versions)
            >>> 'python3_11' in compat
            True
            >>> 'python3_12' in compat
            True
            >>> 'python3_14' in compat  # Should not auto-extend
            False
        """
        # Check cache first
        cache_key = tuple(sorted(python_versions)) if python_versions else ()
        if cache_key in self._compat_cache:
            return self._compat_cache[cache_key]
        
        if not python_versions:
            # When compatibility is unknown, assume all current Python versions work
            # PYTHON_COMPAT declares what the *package* supports, not what the *user* wants
            valid_impls = self._get_valid_python_impls()
            fallback_versions = []
            for impl in sorted(valid_impls):
                if impl.startswith('python3_'):
                    minor = impl[8:]  # Remove 'python3_'
                    if minor.endswith('t'):  # Skip free-threading variants
                        continue
                    fallback_versions.append(f'3.{minor}')
            python_versions = fallback_versions if fallback_versions else ['3.11', '3.12', '3.13', '3.14']
            logger.debug(f"No Python versions specified, using all valid: {python_versions}")
        
        # Get valid implementations from eclass
        valid_impls = self._get_valid_python_impls()
        if not valid_impls:
            # No valid implementations available
            result = []
            self._compat_cache[cache_key] = result
            return result
            
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
        # If we have ONLY generic '3', use all valid implementations
        if has_generic and not specific_versions:
            # Only generic "3" means "any Python 3" - use all valid implementations
            # PYTHON_COMPAT declares what the *package* supports, not what the *user* wants
            for impl in valid_impls:
                if (impl.startswith('python3_') and
                    not impl.endswith('t') and  # Skip free-threading variants
                    impl not in compat_versions):
                    compat_versions.append(impl)
            logger.debug(f"Generic Python 3, using all valid impls: {compat_versions}")
        # If we have both generic '3' and specific versions, ignore the generic '3'
        
        # Remove duplicates and sort
        result = sorted(list(set(compat_versions)))

        # If no valid versions found, fall back to all valid implementations
        # PyPI is a Python archive - packages without explicit requirements
        # should still work with current Python versions
        if not result:
            # When compatibility is unknown, assume all current Python versions work
            # PyPI packages without requires_python typically support all modern versions
            # PYTHON_COMPAT declares what the *package* supports, not what the *user* wants
            def standard_impls(impls):
                return sorted([i for i in impls if i.startswith('python3_') and not i.endswith('t')])
            result = standard_impls(valid_impls)
            logger.debug(f"No valid Python versions from metadata, using all valid impls: {result}")

        # Cache the result
        self._compat_cache[cache_key] = result
        return result
    
    def _translate_spdx_expression(self, spdx_expr: str) -> str:
        """
        Translate SPDX license expression to Gentoo format.
        
        Handles basic SPDX expressions including OR and AND operators.
        
        Args:
            spdx_expr: SPDX license expression (e.g., "Apache-2.0 OR BSD-2-Clause")
            
        Returns:
            Gentoo-compatible license string or None if cannot translate
            
        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> extractor._translate_spdx_expression('MIT')
            'MIT'
            >>> extractor._translate_spdx_expression('Apache-2.0 OR BSD-2-Clause')
            '|| ( Apache-2.0 BSD-2 )'
            >>> extractor._translate_spdx_expression('MIT AND Apache-2.0')
            'MIT Apache-2.0'
        """
        if not spdx_expr:
            return None
            
        # SPDX to Gentoo license mapping
        spdx_to_gentoo = {
            'MIT': 'MIT',
            'Apache-2.0': 'Apache-2.0',
            'BSD-2-Clause': 'BSD-2',
            'BSD-3-Clause': 'BSD',
            'GPL-2.0': 'GPL-2',
            'GPL-2.0-only': 'GPL-2',
            'GPL-2.0-or-later': 'GPL-2+',
            'GPL-2.0+': 'GPL-2+',
            'GPL-3.0': 'GPL-3',
            'GPL-3.0-only': 'GPL-3',
            'GPL-3.0-or-later': 'GPL-3+',
            'GPL-3.0+': 'GPL-3+',
            'LGPL-2.1': 'LGPL-2.1',
            'LGPL-2.1-only': 'LGPL-2.1',
            'LGPL-2.1-or-later': 'LGPL-2.1+',
            'LGPL-2.1+': 'LGPL-2.1+',
            'LGPL-3.0': 'LGPL-3',
            'LGPL-3.0-only': 'LGPL-3',
            'LGPL-3.0-or-later': 'LGPL-3+',
            'LGPL-3.0+': 'LGPL-3+',
            'ISC': 'ISC',
            'MPL-2.0': 'MPL-2.0',
            'CC0-1.0': 'CC0-1.0',
            'Unlicense': 'Unlicense',
            'Python-2.0': 'PSF-2',
            'PSF-2.0': 'PSF-2',
        }
        
        # Handle OR expressions (e.g., "Apache-2.0 OR BSD-2-Clause")
        if ' OR ' in spdx_expr:
            parts = [part.strip() for part in spdx_expr.split(' OR ')]
            gentoo_parts = []
            for part in parts:
                gentoo_license = spdx_to_gentoo.get(part)
                if gentoo_license:
                    gentoo_parts.append(gentoo_license)
                else:
                    # If we can't translate any part, fall back to None
                    return None
            if gentoo_parts:
                return f"|| ( {' '.join(gentoo_parts)} )"
        
        # Handle AND expressions (e.g., "MIT AND Apache-2.0")
        elif ' AND ' in spdx_expr:
            parts = [part.strip() for part in spdx_expr.split(' AND ')]
            gentoo_parts = []
            for part in parts:
                gentoo_license = spdx_to_gentoo.get(part)
                if gentoo_license:
                    gentoo_parts.append(gentoo_license)
                else:
                    # If we can't translate any part, fall back to None
                    return None
            if gentoo_parts:
                return ' '.join(gentoo_parts)
        
        # Single license
        else:
            spdx_clean = spdx_expr.strip()
            if spdx_clean in spdx_to_gentoo:
                return spdx_to_gentoo[spdx_clean]
        
        return None
    
    def translate_license(self, pypi_license: str, license_expression: str = '') -> str:
        """
        Translate PyPI license string to Gentoo license format.
        
        Supports both old-style 'license' field and new PEP 639 'license_expression' field.
        If license_expression is provided and valid, it takes precedence.
        
        Args:
            pypi_license: License string from PyPI metadata 'license' field
            license_expression: SPDX expression from 'license_expression' field (PEP 639)
            
        Returns:
            Gentoo-compatible license string
            
        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> extractor.translate_license('MIT')
            'MIT'
            >>> extractor.translate_license('Apache-2.0')
            'Apache-2.0'
            >>> extractor.translate_license('', 'Apache-2.0')
            'Apache-2.0'
            >>> extractor.translate_license('', 'Apache-2.0 OR BSD-2-Clause')
            '|| ( Apache-2.0 BSD-2 )'
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
        # Try license_expression first (PEP 639 - new style)
        if license_expression:
            gentoo_license = self._translate_spdx_expression(license_expression)
            if gentoo_license:
                return gentoo_license
        
        # Fall back to traditional license field
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

    @classmethod
    def _get_supported_python_versions(cls) -> List[str]:
        """
        Get supported Python versions as version strings (e.g., "3.11", "3.12").

        Returns:
            List of Python version strings supported by the system

        Examples:
            >>> versions = EbuildDataExtractor._get_supported_python_versions()
            >>> all(v.startswith('3.') for v in versions)
            True
        """
        valid_impls = cls._get_valid_python_impls()
        versions = []
        for impl in sorted(valid_impls):
            if impl.startswith('python3_'):
                # Convert python3_11 -> 3.11, skip free-threading variants
                suffix = impl[8:]
                if suffix.endswith('t'):
                    continue
                try:
                    versions.append(f'3.{suffix}')
                except ValueError:
                    continue
        return versions

    @staticmethod
    def _evaluate_marker_for_python(marker, python_version: str) -> bool:
        """
        Evaluate a PEP 508 environment marker for a specific Python version.

        Args:
            marker: The marker object from packaging.requirements.Requirement
            python_version: Python version string like "3.11"

        Returns:
            True if the marker evaluates to True for this Python version

        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> try:
            ...     from pip._vendor.packaging.requirements import Requirement
            ... except ImportError:
            ...     from packaging.requirements import Requirement
            >>> req = Requirement('numpy>=2; python_version >= "3.9"')
            >>> extractor._evaluate_marker_for_python(req.marker, '3.11')
            True
            >>> extractor._evaluate_marker_for_python(req.marker, '3.8')
            False
        """
        if marker is None:
            return True

        # Create environment dict for marker evaluation
        # We only need python_version for our use case
        env = {
            'python_version': python_version,
            'python_full_version': f'{python_version}.0',
            'implementation_name': 'cpython',
            'platform_system': 'Linux',
            'sys_platform': 'linux',
            'os_name': 'posix',
        }

        try:
            return marker.evaluate(env)
        except Exception as e:
            logger.warning(f"Failed to evaluate marker {marker}: {e}")
            return True  # Be permissive on errors

    def format_dependencies(self, dependencies: List[str]) -> List[str]:
        """
        Format Python dependencies for ebuild DEPEND/RDEPEND.

        Handles PEP 508 environment markers by evaluating them against supported
        Python versions and generating conditional Gentoo dependencies when needed.

        Args:
            dependencies: List of requirement strings (may include environment markers)

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

        # Import Requirement class
        try:
            from pip._vendor.packaging.requirements import Requirement
        except ImportError:
            try:
                from packaging.requirements import Requirement
            except ImportError:
                Requirement = None

        # Get supported Python versions
        supported_versions = self._get_supported_python_versions()
        if not supported_versions:
            supported_versions = ['3.11', '3.12', '3.13', '3.14']

        # Group dependencies by package name to handle multiple markers for same package
        # Structure: {package_name: [(specifiers, marker, gentoo_name), ...]}
        package_deps: Dict[str, List[Tuple[Any, Any, str]]] = {}

        for dep_str in dependencies:
            if Requirement is None:
                # Fallback: manual parsing without marker support
                match = re.match(r'^([a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]|[a-zA-Z0-9])', dep_str.strip())
                package_name = match.group(1) if match else dep_str.split()[0]
                gentoo_name = default_translator.pypi_to_gentoo(package_name)
                if package_name not in package_deps:
                    package_deps[package_name] = []
                package_deps[package_name].append((None, None, gentoo_name))
                continue

            try:
                req = Requirement(dep_str.strip())
                package_name = req.name
                specifiers = req.specifier
                marker = req.marker
                gentoo_name = default_translator.pypi_to_gentoo(package_name)

                if package_name not in package_deps:
                    package_deps[package_name] = []
                package_deps[package_name].append((specifiers, marker, gentoo_name))
            except Exception as e:
                logger.warning(f"Failed to parse dependency '{dep_str}': {e}")
                continue

        # Process each package and generate appropriate Gentoo dependencies
        formatted_deps = []

        for package_name, dep_entries in package_deps.items():
            gentoo_name = dep_entries[0][2]  # All entries have the same gentoo_name
            # Get full Gentoo atom (may use custom mapping like sci-ml/pytorch for torch)
            gentoo_atom = self._get_gentoo_atom(package_name, gentoo_name)

            # Build a map: python_version -> gentoo_dep_string
            version_to_dep: Dict[str, str] = {}

            for specifiers, marker, _ in dep_entries:
                if specifiers:
                    gentoo_dep = self._format_gentoo_dependency(gentoo_atom, specifiers)
                else:
                    gentoo_dep = gentoo_atom

                # Determine which Python versions this applies to
                for py_ver in supported_versions:
                    if self._evaluate_marker_for_python(marker, py_ver):
                        # This dependency applies to this Python version
                        # If there's already a dep for this version, we have conflicting markers
                        # Keep the first one (or we could try to merge, but that's complex)
                        if py_ver not in version_to_dep:
                            version_to_dep[py_ver] = gentoo_dep

            if not version_to_dep:
                # No Python versions matched - skip this dependency entirely
                logger.debug(f"Dependency {package_name} has no matching Python versions, skipping")
                continue

            # Check if all versions have the same dependency
            unique_deps = set(version_to_dep.values())

            if len(unique_deps) == 1:
                # All versions have the same dependency - simple case
                formatted_deps.append(self._add_python_usedep(list(unique_deps)[0]))
            else:
                # Different versions have different dependencies - use conditionals
                # Group versions by their dependency to minimize output
                dep_to_versions: Dict[str, List[str]] = {}
                for py_ver, dep in version_to_dep.items():
                    if dep not in dep_to_versions:
                        dep_to_versions[dep] = []
                    dep_to_versions[dep].append(py_ver)

                for dep, versions in dep_to_versions.items():
                    # Generate individual conditionals for each Python version
                    # e.g., "3.11" -> "python_targets_python3_11? ( dep[${PYTHON_USEDEP}] )"
                    dep_with_usedep = self._add_python_usedep(dep)
                    for v in sorted(versions):
                        use_flag = f"python_targets_python{v.replace('.', '_')}"
                        formatted_deps.append(f"{use_flag}? ( {dep_with_usedep} )")

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
            
            # Translate to Gentoo name and get full atom (may use custom mapping)
            gentoo_name = default_translator.pypi_to_gentoo(package_name)
            gentoo_atom = self._get_gentoo_atom(package_name, gentoo_name)
            if specifiers:
                gentoo_dep = self._format_gentoo_dependency(gentoo_atom, specifiers)
            else:
                gentoo_dep = gentoo_atom

            # Add PYTHON_USEDEP to ensure deps are built for same Python targets
            gentoo_dep = self._add_python_usedep(gentoo_dep)

            # Add to collections
            iuse_flags.add(use_flag)
            if use_flag not in optional_depend:
                optional_depend[use_flag] = []
            optional_depend[use_flag].append(gentoo_dep)
        
        return sorted(list(iuse_flags)), optional_depend

    def _add_python_usedep(self, dep: str) -> str:
        """
        Add ${PYTHON_USEDEP} to a Gentoo dependency string.

        Handles simple atoms, || ( ) groups, and space-separated compound
        atoms (e.g., '>=pkg-1.0 <pkg-2.0') by adding the USEDEP to each
        individual atom.

        Args:
            dep: Gentoo dependency string

        Returns:
            Dependency string with ${PYTHON_USEDEP} added to each atom

        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> extractor._add_python_usedep('>=dev-python/requests-2.0')
            '>=dev-python/requests-2.0[${PYTHON_USEDEP}]'
            >>> extractor._add_python_usedep('|| ( =dev-python/foo-1.0 =dev-python/foo-1.0.0 )')
            '|| ( =dev-python/foo-1.0[${PYTHON_USEDEP}] =dev-python/foo-1.0.0[${PYTHON_USEDEP}] )'
            >>> extractor._add_python_usedep('dev-python/simple')
            'dev-python/simple[${PYTHON_USEDEP}]'
            >>> extractor._add_python_usedep('>=dev-python/foo-1.0 <dev-python/foo-2.0')
            '>=dev-python/foo-1.0[${PYTHON_USEDEP}] <dev-python/foo-2.0[${PYTHON_USEDEP}]'
        """
        usedep = '[${PYTHON_USEDEP}]'

        # Handle || ( ) groups - add USEDEP to each atom inside
        if dep.startswith('|| ('):
            # Extract atoms from inside || ( ... )
            inner = dep[4:-1].strip()  # Remove "|| (" and ")"
            atoms = inner.split()
            atoms_with_usedep = [f"{atom}{usedep}" for atom in atoms]
            return f"|| ( {' '.join(atoms_with_usedep)} )"

        # Handle space-separated compound atoms (e.g., '>=pkg-1.0 <pkg-2.0')
        # Check if this looks like multiple atoms (contains space and multiple category/package refs)
        if ' ' in dep and not dep.startswith('|| '):
            atoms = dep.split()
            # Verify each part looks like an atom (contains /)
            if all('/' in atom for atom in atoms):
                atoms_with_usedep = [f"{atom}{usedep}" for atom in atoms]
                return ' '.join(atoms_with_usedep)

        # Simple atom - just append USEDEP
        return f"{dep}{usedep}"

    def _translate_pypi_version(self, pypi_version: str) -> str:
        """
        Translate PyPI version string to Gentoo format.

        Converts PEP 440 pre-release and post-release markers:
        - a/alpha -> _alpha (e.g., 2.0a0 -> 2.0_alpha0)
        - b/beta -> _beta (e.g., 1.0b1 -> 1.0_beta1)
        - rc/c -> _rc (e.g., 3.0rc1 -> 3.0_rc1)
        - .post -> _p (e.g., 1.0.post1 -> 1.0_p1)
        - .dev -> _pre (e.g., 1.0.dev1 -> 1.0_pre1)

        Args:
            pypi_version: Version string in PyPI/PEP 440 format

        Returns:
            Version string in Gentoo format

        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> extractor._translate_pypi_version('2.0a0')
            '2.0_alpha0'
            >>> extractor._translate_pypi_version('1.0b1')
            '1.0_beta1'
            >>> extractor._translate_pypi_version('3.0rc1')
            '3.0_rc1'
            >>> extractor._translate_pypi_version('1.0c1')
            '1.0_rc1'
            >>> extractor._translate_pypi_version('1.0.post1')
            '1.0_p1'
            >>> extractor._translate_pypi_version('1.2.3')
            '1.2.3'
        """
        import re
        version = pypi_version

        # Handle pre-release markers (must check longer patterns first)
        # alpha/a followed by a number
        # Use negative lookbehind to avoid matching 'a' in already-translated '_alpha'
        version = re.sub(r'\.?alpha(\d+)', r'_alpha\1', version)
        version = re.sub(r'(?<![a-z])\.?a(\d+)', r'_alpha\1', version)

        # beta/b followed by a number
        # Use negative lookbehind to avoid matching 'b' in already-translated '_beta'
        version = re.sub(r'\.?beta(\d+)', r'_beta\1', version)
        version = re.sub(r'(?<![a-z])\.?b(\d+)', r'_beta\1', version)

        # rc/c followed by a number (release candidate)
        # Must check 'rc' first before 'c' to avoid partial match
        version = re.sub(r'\.?rc(\d+)', r'_rc\1', version)
        # Only match standalone 'c' not preceded by 'r' (use negative lookbehind)
        version = re.sub(r'(?<!r)\.?c(\d+)', r'_rc\1', version)

        # post release
        version = re.sub(r'\.post(\d+)', r'_p\1', version)

        # dev release
        version = re.sub(r'\.dev(\d+)', r'_pre\1', version)

        return version

    def _normalize_version_shortest(self, version: str) -> str:
        """
        Normalize version to shortest form by stripping trailing .0 segments.

        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> extractor._normalize_version_shortest('1.33.0')
            '1.33'
            >>> extractor._normalize_version_shortest('2.0.0')
            '2.0'
            >>> extractor._normalize_version_shortest('1.33')
            '1.33'
        """
        # Don't normalize versions with suffixes
        if '_' in version:
            return version
        while version.endswith('.0') and version.count('.') > 1:
            version = version[:-2]
        return version

    def _normalize_version_longest(self, version: str) -> str:
        """
        Normalize version to longest form by adding trailing .0 segment.

        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> extractor._normalize_version_longest('1.33')
            '1.33.0'
            >>> extractor._normalize_version_longest('1.33.0')
            '1.33.0'
        """
        # Don't normalize versions with suffixes
        if '_' in version:
            return version
        if not version.endswith('.0') or version.count('.') < 2:
            return version + '.0'
        return version

    def _get_pep440_equivalent_version(self, version: str) -> Optional[str]:
        """
        Get PEP 440 equivalent version with/without trailing .0.

        PEP 440 considers 1.33 and 1.33.0 equivalent, but Gentoo doesn't.
        This returns the alternate form so we can match either.

        Args:
            version: Version string (already translated to Gentoo format)

        Returns:
            Alternate version form, or None if not applicable

        Examples:
            >>> extractor = EbuildDataExtractor()
            >>> extractor._get_pep440_equivalent_version('1.33')
            '1.33.0'
            >>> extractor._get_pep440_equivalent_version('1.33.0')
            '1.33'
            >>> extractor._get_pep440_equivalent_version('2.0.0')
            '2.0'
            >>> extractor._get_pep440_equivalent_version('1.0_alpha1')
        """
        # Don't apply to versions with suffixes (alpha, beta, rc, p, pre)
        if '_' in version:
            return None

        if version.endswith('.0') and version.count('.') > 1:
            # Has trailing .0 - return without it (1.33.0 -> 1.33)
            return version[:-2]
        else:
            # No trailing .0 - return with it (1.33 -> 1.33.0)
            return version + '.0'

    def _format_gentoo_dependency(self, gentoo_atom: str, specifiers) -> str:
        """
        Format a Gentoo dependency string with version specifiers.

        Args:
            gentoo_atom: The full Gentoo atom (e.g., 'dev-python/requests' or 'sci-ml/pytorch')
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
            >>> result = extractor._format_gentoo_dependency('dev-python/requests', req.specifier)
            >>> '>=dev-python/requests-1.4' in result
            True
            >>> '<dev-python/requests-2' in result
            True
            >>> # Test ~= with patch version
            >>> req = Requirement('requests~=1.4.2')
            >>> result = extractor._format_gentoo_dependency('dev-python/requests', req.specifier)
            >>> '>=dev-python/requests-1.4.2' in result
            True
            >>> '<dev-python/requests-1.5' in result
            True
            >>> # Test simple >= operator
            >>> req = Requirement('requests>=2.0.0')
            >>> extractor._format_gentoo_dependency('dev-python/requests', req.specifier)
            '>=dev-python/requests-2.0.0'
            >>> # Test with custom category
            >>> req = Requirement('torch>=2.0')
            >>> extractor._format_gentoo_dependency('sci-ml/pytorch', req.specifier)
            '>=sci-ml/pytorch-2.0'
        """
        if not specifiers:
            return gentoo_atom

        # Convert PyPI version specifiers to Gentoo format
        dep_parts = []
        for spec in specifiers:
            operator = spec.operator
            version = self._translate_pypi_version(spec.version)

            # Translate operators to Gentoo format
            if operator == '==':
                # Handle wildcard versions: PyPI ==23.* -> Gentoo =pkg-23*
                if version.endswith('.*'):
                    version = version[:-2] + '*'  # Remove .* and add *
                    dep_parts.append(f"={gentoo_atom}-{version}")
                else:
                    # PEP 440 considers 1.33 == 1.33.0, but Gentoo doesn't
                    # Generate || ( ~pkg-1.33 ~pkg-1.33.0 ) to match either
                    # Use ~ to also match revision bumps (e.g., 1.33-r1)
                    alt_version = self._get_pep440_equivalent_version(version)
                    if alt_version:
                        dep_parts.append(f"|| ( ~{gentoo_atom}-{version} ~{gentoo_atom}-{alt_version} )")
                    else:
                        dep_parts.append(f"~{gentoo_atom}-{version}")
            elif operator == '>=':
                # Normalize to shortest form so >=1.33.0 matches version 1.33
                # (In Gentoo 1.33 < 1.33.0, but in PEP 440 they're equal)
                norm_version = self._normalize_version_shortest(version)
                dep_parts.append(f">={gentoo_atom}-{norm_version}")
            elif operator == '>':
                # Keep original - >1.33.0 correctly excludes both 1.33 and 1.33.0 in Gentoo
                dep_parts.append(f">{gentoo_atom}-{version}")
            elif operator == '<=':
                # Use longest form so <=1.33 also includes 1.33.0
                norm_version = self._normalize_version_longest(version)
                dep_parts.append(f"<={gentoo_atom}-{norm_version}")
            elif operator == '<':
                # Normalize to shortest form so <1.33.0 excludes 1.33 too
                # (In PEP 440, 1.33 == 1.33.0, so <1.33.0 should exclude 1.33)
                norm_version = self._normalize_version_shortest(version)
                dep_parts.append(f"<{gentoo_atom}-{norm_version}")
            elif operator == '!=':
                # Handle wildcard versions: PyPI !=9.2.* -> Gentoo !=pkg-9.2*
                # Note: Gentoo uses != for versioned blocks, ! is only for unversioned
                if version.endswith('.*'):
                    version = version[:-2] + '*'  # Remove .* and add *
                dep_parts.append(f"!={gentoo_atom}-{version}")
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
                        dep_parts.append(f">={gentoo_atom}-{version}")
                        dep_parts.append(f"<{gentoo_atom}-{upper_version}")
                    except ValueError:
                        # If conversion fails, fall back to just >=
                        dep_parts.append(f">={gentoo_atom}-{version}")
                else:
                    # Single segment - not valid per PEP 440, but handle gracefully
                    dep_parts.append(f">={gentoo_atom}-{version}")

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
            ...     'python_versions': ['3.11', '3.12', '3.13'],
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
            >>> 'python3_11' in ebuild_data['PYTHON_COMPAT']
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
        use_wheel = package_info.get('use_wheel', False)
        version = metadata.get('version', '')

        # Extract original PyPI project name from distribution filename
        # PyPI normalizes the 'name' field but keeps original case in filenames
        # e.g., name="pillow" but filename="Pillow-9.4.0.tar.gz"
        pypi_pn = metadata.get('name', '')  # fallback to normalized name

        if use_wheel:
            # Extract from wheel filename: {project}-{version}-{pytag}-{abi}-{platform}.whl
            wheel_dist = package_info.get('wheel_distribution') or {}
            wheel_filename = wheel_dist.get('filename', '')
            if wheel_filename and version:
                # Wheel filenames have the format: name-version-pytag-abi-platform.whl
                # The name part uses underscores instead of hyphens/dots
                parts = wheel_filename.split('-')
                if len(parts) >= 5:  # name-version-pytag-abi-platform.whl
                    # Name might contain underscores, so we need to find where version starts
                    # Version should match what we have
                    for i in range(1, len(parts)):
                        if parts[i] == version.replace('-', '_'):
                            pypi_pn = '-'.join(parts[:i]).replace('_', '-')
                            break
        else:
            # Extract from sdist filename: {project}-{version}.tar.gz
            sdist = package_info.get('source_distribution') or {}
            sdist_filename = sdist.get('filename', '')
            if sdist_filename and version:
                # Remove version and extension to get original project name
                suffix = f'-{version}.tar.gz'
                if sdist_filename.endswith(suffix):
                    pypi_pn = sdist_filename[:-len(suffix)]
                else:
                    # Try .zip
                    suffix = f'-{version}.zip'
                    if sdist_filename.endswith(suffix):
                        pypi_pn = sdist_filename[:-len(suffix)]

        ebuild_data = {
            # Basic package information
            'PN': metadata.get('name', ''),
            'PV': metadata.get('version', ''),
            'DESCRIPTION': metadata.get('summary', ''),
            'HOMEPAGE': metadata.get('homepage', ''),
            'LICENSE': self.translate_license(
                metadata.get('license', ''),
                metadata.get('license_expression', '')
            ),

            # PyPI eclass variables - use original case from filename
            'PYPI_PN': pypi_pn,
            'PYPI_PV': version,
            
            # Python compatibility
            'PYTHON_COMPAT': self.format_python_compat(
                package_info.get('python_versions', [])
            ),

            # Dependencies - pure Python packages only need runtime deps
            'DEPEND': [],  # No build-time deps for pure Python
            'RDEPEND': self.format_dependencies(
                package_info.get('runtime_dependencies', [])
            ),

            # Source information - use wheel URL if no sdist
            'SRC_URI': '',  # Will be set below

            # Wheel support
            'use_wheel': use_wheel,

            # Additional metadata
            'KEYWORDS': 'amd64 x86',  # Stable keywords - PyPI packages are generally stable releases
            'SLOT': '0',
        }

        # Set SRC_URI based on distribution type
        # Priority: sdist > git > wheel
        sdist = package_info.get('source_distribution')
        git_repo_url = package_info.get('git_repo_url')

        if sdist and sdist.get('url'):
            # Prefer sdist
            ebuild_data['SRC_URI'] = sdist.get('url', '')
            ebuild_data['use_git'] = False
        elif git_repo_url and not sdist:
            # Use git source when no sdist available
            try:
                from portage_pip_fuse.git_provider import normalize_git_url
                ebuild_data['use_git'] = True
                ebuild_data['git_repo_uri'] = normalize_git_url(git_repo_url)
                ebuild_data['git_commit'] = 'v${PV}'  # Most common tag pattern
                ebuild_data['SRC_URI'] = ''  # No SRC_URI for git-based builds
            except ImportError:
                # Fallback to wheel if git_provider not available
                if use_wheel:
                    wheel_dist = package_info.get('wheel_distribution') or {}
                    ebuild_data['SRC_URI'] = wheel_dist.get('url', '')
                    ebuild_data['wheel_filename'] = wheel_dist.get('filename', '')
        elif use_wheel:
            wheel_dist = package_info.get('wheel_distribution') or {}
            ebuild_data['SRC_URI'] = wheel_dist.get('url', '')
            ebuild_data['wheel_filename'] = wheel_dist.get('filename', '')

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