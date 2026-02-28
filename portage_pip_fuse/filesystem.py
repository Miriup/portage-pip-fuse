"""
FUSE filesystem implementation for portage-pip adapter.

This module provides a complete FUSE filesystem that dynamically generates
Gentoo overlay content from PyPI packages. It integrates all components:
- Name translation between PyPI and Gentoo formats
- Version translation from PyPI to Gentoo
- Dynamic ebuild generation with dependencies
- Manifest file generation with checksums
- PyPI extras handling as Gentoo USE flags

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import errno
import logging
import os
import re
import stat
import time
from typing import Optional, Dict, List, Set, Tuple

from fuse import FUSE, FuseOSError, Operations

from .constants import REPO_NAME, DEFAULT_PATCH_FILE
from .dependency_patch import DependencyPatchStore
from .python_compat_patch import PythonCompatPatchStore
from .interrupt import InterruptChecker, check_interrupt
from .prefetcher import create_prefetched_translator
from .pip_metadata import PyPIMetadataExtractor, EbuildDataExtractor
from .hybrid_metadata import HybridMetadataExtractor
from .prefetcher import PyPIPrefetcher
from .package_filter import (
    FilterBase, FilterAll, FilterCurated, FilterRecent, 
    FilterNewest, FilterDependencyTree, FilterChain, FilterRegistry
)
from .version_filter import (
    VersionFilterBase,
    VersionFilterRegistry,
    VersionFilterChain
)

# Import gs-pypi version parsing
try:
    import sys
    import os.path
    gs_pypi_path = os.path.join(os.path.dirname(__file__), '..', '..', 'gs-pypi')
    if os.path.exists(gs_pypi_path):
        sys.path.insert(0, gs_pypi_path)
        from gs_pypi.pypi_db import PypiVersion
    else:
        PypiVersion = None
except ImportError:
    PypiVersion = None

logger = logging.getLogger(__name__)


class PortagePipFS(Operations):
    """
    FUSE filesystem that provides a virtual interface between pip and portage.
    
    This filesystem presents PyPI packages as if they were portage ebuilds,
    allowing transparent access to Python packages through Gentoo's package
    management system.
    
    Features:
    - Dynamic ebuild generation from PyPI metadata
    - Bidirectional name translation (PyPI <-> Gentoo)
    - Version translation from PyPI to Gentoo format
    - Manifest file generation with checksums from PyPI
    - PyPI extras handling as Gentoo USE flags
    - Thin overlay layout with on-demand content generation
    """
    
    def __init__(self, root: str = "/", cache_ttl: int = 3600, cache_dir: Optional[str] = None,
                 filter_config: Optional[Dict] = None, patch_file: Optional[str] = None,
                 no_patches: bool = False):
        """
        Initialize the FUSE filesystem.

        Args:
            root: Root directory for the filesystem operations
            cache_ttl: Cache time-to-live in seconds (default: 1 hour)
            cache_dir: Directory for persistent cache storage
            filter_config: Package filter configuration dictionary
            patch_file: Path to dependency patch file (default: ~/.cache/portage-pip-fuse/patches.json)
            no_patches: If True, disable the dependency patching system entirely
        """
        self.root = root
        self.cache_ttl = cache_ttl
        self.no_patches = no_patches
        
        # Content cache: path -> (content, timestamp)
        self._content_cache: Dict[str, Tuple[bytes, float]] = {}

        # Package metadata cache: pypi_name -> (metadata, timestamp)
        self._metadata_cache: Dict[str, Tuple[dict, float]] = {}

        # Package JSON cache: pypi_name -> (json_data, timestamp)
        # This caches the raw get_package_json result to avoid redundant calls
        self._package_json_cache: Dict[str, Tuple[Optional[dict], float]] = {}

        # Category listing cache: category -> (package_list, timestamp)
        # Caches the expensive dev-python/ listing to avoid re-computing on each readdir
        self._category_cache: Dict[str, Tuple[List[str], float]] = {}
        
        # Name translation components
        self.name_translator = create_prefetched_translator()
        
        # Choose metadata extractor based on configuration
        use_sqlite = filter_config.get('use_sqlite', True) if filter_config else True
        if use_sqlite:
            self.pypi_extractor = HybridMetadataExtractor(cache_ttl=cache_ttl, cache_dir=cache_dir)
        else:
            self.pypi_extractor = PyPIMetadataExtractor(cache_ttl=cache_ttl, cache_dir=cache_dir)
            
        self.ebuild_extractor = EbuildDataExtractor(cache_dir=cache_dir)
        
        # Initialize dependency patch store
        if not no_patches:
            if patch_file:
                self.patch_store = DependencyPatchStore(patch_file)
            else:
                self.patch_store = DependencyPatchStore(str(DEFAULT_PATCH_FILE))
            logger.info(f"Dependency patching enabled, using {self.patch_store.storage_path}")
        else:
            self.patch_store = None
            logger.info("Dependency patching disabled")

        # Initialize PYTHON_COMPAT patch store (uses same file as dependency patches)
        if not no_patches:
            if patch_file:
                self.compat_patch_store = PythonCompatPatchStore(patch_file)
            else:
                self.compat_patch_store = PythonCompatPatchStore(str(DEFAULT_PATCH_FILE))
            logger.info(f"PYTHON_COMPAT patching enabled, using {self.compat_patch_store.storage_path}")
        else:
            self.compat_patch_store = None

        # Static overlay structure
        self.static_dirs = {
            "/",
            "/dev-python",
            "/profiles",
            "/metadata",
            "/eclass",
            # .sys virtual filesystem for dependency patching
            "/.sys",
            "/.sys/dependencies",
            "/.sys/dependencies/dev-python",
            "/.sys/dependencies-patch",
            "/.sys/dependencies-patch/dev-python",
            # .sys virtual filesystem for PYTHON_COMPAT patching
            "/.sys/python-compat",
            "/.sys/python-compat/dev-python",
            "/.sys/python-compat-patch",
            "/.sys/python-compat-patch/dev-python"
        }
        
        # Static files
        self.static_files = {
            "/profiles/repo_name": (REPO_NAME + "\n").encode('utf-8'),
            "/metadata/layout.conf": self._generate_layout_conf().encode('utf-8')
        }
        
        # Set up package filter based on configuration
        self.package_filter = self._create_filter(filter_config or {})
        
        # Set up version filters from config
        self.version_filter_chain = self._create_version_filter(filter_config or {})
        
        # Pre-resolve dependency trees during initialization
        # This avoids slow first directory listings
        if hasattr(self.package_filter, 'initialize'):
            logger.info("Pre-resolving package filter...")
            self.package_filter.initialize()
        
        # Timestamp lookup setting
        self.no_timestamps = (filter_config or {}).get('no_timestamps', True)

        # Maximum versions to show per package (0 = unlimited)
        # Limiting versions significantly speeds up readdir for packages with many releases
        self.max_versions = (filter_config or {}).get('max_versions', 0)
        
        logger.info(f"PortagePipFS initialized with filter: {self.package_filter.get_description()}")
        if self.version_filter_chain:
            logger.info(f"Version filters: {self.version_filter_chain.get_description()}")
        if self.no_timestamps:
            logger.info("Timestamp lookup disabled for faster performance")
        
    def _create_version_filter(self, filter_config: Dict) -> Optional[VersionFilterChain]:
        """Create version filter chain based on configuration."""
        # Check disabled_filters from CLI (respects --no-filter)
        disabled_filters = set(filter_config.get('disabled_filters', []))

        # Default version filters, excluding any that were explicitly disabled
        default_version_filters = ['source-dist', 'python-compat']
        version_filters = [f for f in default_version_filters if f not in disabled_filters]

        if not version_filters:
            return None
            
        filters = []
        for filter_name in version_filters:
            if filter_name == 'source-dist':
                filter_class = VersionFilterRegistry.get_filter_class('source-dist')
                if filter_class:
                    filters.append(filter_class())
            elif filter_name == 'python-compat':
                filter_class = VersionFilterRegistry.get_filter_class('python-compat')
                if filter_class:
                    filters.append(filter_class())
            elif filter_name == 'latest':
                filter_class = VersionFilterRegistry.get_filter_class('latest')
                if filter_class:
                    max_versions = filter_config.get('max_versions', 5)
                    filters.append(filter_class(max_versions=max_versions))
            else:
                logger.warning(f"Unknown version filter: {filter_name}")
        
        if filters:
            return VersionFilterChain(filters)
        return None
    
    def _create_filter(self, filter_config: Dict) -> FilterBase:
        """Create package filter based on configuration."""
        active_filters = filter_config.get('active_filters', [])
        
        if not active_filters:
            # No filters - return all packages
            return FilterAll()
        
        # Create filter instances
        filters = []
        for filter_name in active_filters:
            filter_class = FilterRegistry.get_filter_class(filter_name)
            if not filter_class:
                logger.warning(f"Unknown filter: {filter_name}")
                continue
            
            # Create filter instance with appropriate parameters
            if filter_name == 'recent':
                days = filter_config.get('days', 30)
                filters.append(filter_class(days=days))
            elif filter_name == 'newest':
                count = filter_config.get('count', 100)
                filters.append(filter_class(count=count))
            elif filter_name == 'deps':
                deps_for = filter_config.get('deps_for', [])
                use_flags = filter_config.get('use_flags', [])
                filters.append(filter_class(
                    root_packages=deps_for,
                    use_flags=use_flags,
                    cache_dir=self.pypi_extractor.cache_dir
                ))
            else:
                # Default constructor
                filters.append(filter_class())
        
        if len(filters) == 1:
            return filters[0]
        else:
            return FilterChain(filters, operator='AND')
        
    def _generate_layout_conf(self) -> str:
        """Generate layout.conf for the overlay."""
        return f"""repo-name = {REPO_NAME}
masters = gentoo
thin-manifests = true
profile-formats = portage-2
cache-formats = md5-dict
"""
    
    def _parse_path(self, path: str) -> Dict[str, str]:
        """Parse filesystem path and return components."""
        path = path.strip('/')
        if not path:
            return {'type': 'root'}

        parts = path.split('/')

        # Handle .sys/ virtual filesystem for dependency patching
        if parts[0] == '.sys':
            return self._parse_sys_path(parts)

        if parts[0] == 'profiles':
            if len(parts) == 1:
                return {'type': 'profiles'}
            elif len(parts) == 2 and parts[1] == 'repo_name':
                return {'type': 'profiles_file', 'filename': 'repo_name'}
            else:
                # Invalid profiles path - return not found
                return {'type': 'invalid'}
        elif parts[0] == 'metadata':
            if len(parts) == 1:
                return {'type': 'metadata'}
            elif len(parts) == 2 and parts[1] == 'layout.conf':
                return {'type': 'metadata_file', 'filename': 'layout.conf'}
            else:
                # Invalid metadata path - return not found
                return {'type': 'invalid'}
        elif parts[0] == 'eclass':
            return {'type': 'eclass', 'filename': parts[-1] if len(parts) > 1 else None}
        elif parts[0] == 'dev-python' and len(parts) == 1:
            return {'type': 'category', 'category': 'dev-python'}
        elif parts[0] == 'dev-python' and len(parts) == 2:
            return {'type': 'package', 'category': 'dev-python', 'package': parts[1]}
        elif parts[0] == 'dev-python' and len(parts) == 3:
            category, package, filename = parts
            if filename == 'metadata.xml':
                return {'type': 'package_metadata', 'category': category, 'package': package, 'filename': filename}
            elif filename == 'Manifest':
                return {'type': 'manifest', 'category': category, 'package': package, 'filename': filename}
            elif filename.endswith('.ebuild'):
                # Extract version from ebuild filename
                name_version = filename[:-7]  # Remove '.ebuild'
                if name_version.startswith(package + '-'):
                    version = name_version[len(package) + 1:]
                    return {'type': 'ebuild', 'category': category, 'package': package, 'version': version, 'filename': filename}
                    
        return {'type': 'unknown'}

    def _encode_dep_filename(self, dep: str) -> str:
        """
        Encode a dependency atom for use as a filename.

        Since dependency atoms contain '/' (e.g., dev-python/requests),
        we replace '/' with '::' to make valid filenames that are still readable.

        Example: >=dev-python/urllib3-1.21[${PYTHON_USEDEP}]
              -> >=dev-python::urllib3-1.21[${PYTHON_USEDEP}]

        Args:
            dep: Dependency atom string

        Returns:
            String with '/' replaced by '::' for use as filename
        """
        return dep.replace('/', '::')

    def _decode_dep_filename(self, filename: str) -> str:
        """
        Decode a filename back to a dependency atom.

        Args:
            filename: Filename with '::' instead of '/'

        Returns:
            Original dependency atom string with '/' restored
        """
        return filename.replace('::', '/')

    def _parse_sys_path(self, parts: List[str]) -> Dict[str, str]:
        """
        Parse .sys/ virtual filesystem paths for dependency patching.

        Directory structure:
            .sys/
                dependencies/
                    dev-python/
                        {package}/
                            {version}/                    # e.g., requests/2.31.0/
                                >=dev-python/urllib3-1.21[${PYTHON_USEDEP}]  # one file per dep
                            _all/                         # patches apply to all versions
                dependencies-patch/
                    dev-python/
                        {package}/
                            {version}.patch               # e.g., 2.31.0.patch
                            _all.patch
        """
        if len(parts) == 1:
            # /.sys
            return {'type': 'sys_root'}

        if parts[1] == 'dependencies':
            if len(parts) == 2:
                # /.sys/dependencies
                return {'type': 'sys_deps'}
            elif len(parts) == 3:
                # /.sys/dependencies/dev-python
                return {'type': 'sys_deps_category', 'category': parts[2]}
            elif len(parts) == 4:
                # /.sys/dependencies/dev-python/requests
                return {'type': 'sys_deps_package', 'category': parts[2], 'package': parts[3]}
            elif len(parts) == 5:
                # /.sys/dependencies/dev-python/requests/2.31.0
                return {'type': 'sys_deps_version', 'category': parts[2], 'package': parts[3], 'version': parts[4]}
            elif len(parts) == 6:
                # /.sys/dependencies/dev-python/requests/2.31.0/>=dev-python::urllib3-1.21...
                # The dep filename has '/' replaced with '::', decode it
                return {
                    'type': 'sys_deps_dep',
                    'category': parts[2],
                    'package': parts[3],
                    'version': parts[4],
                    'dep': self._decode_dep_filename(parts[5])
                }

        elif parts[1] == 'dependencies-patch':
            if len(parts) == 2:
                # /.sys/dependencies-patch
                return {'type': 'sys_patch'}
            elif len(parts) == 3:
                # /.sys/dependencies-patch/dev-python
                return {'type': 'sys_patch_category', 'category': parts[2]}
            elif len(parts) == 4:
                # /.sys/dependencies-patch/dev-python/requests
                return {'type': 'sys_patch_package', 'category': parts[2], 'package': parts[3]}
            elif len(parts) == 5:
                # /.sys/dependencies-patch/dev-python/requests/2.31.0.patch
                filename = parts[4]
                if filename.endswith('.patch'):
                    version = filename[:-6]  # Remove .patch
                    return {
                        'type': 'sys_patch_file',
                        'category': parts[2],
                        'package': parts[3],
                        'version': version,
                        'filename': filename
                    }

        elif parts[1] == 'python-compat':
            if len(parts) == 2:
                # /.sys/python-compat
                return {'type': 'sys_compat'}
            elif len(parts) == 3:
                # /.sys/python-compat/dev-python
                return {'type': 'sys_compat_category', 'category': parts[2]}
            elif len(parts) == 4:
                # /.sys/python-compat/dev-python/pillow
                return {'type': 'sys_compat_package', 'category': parts[2], 'package': parts[3]}
            elif len(parts) == 5:
                # /.sys/python-compat/dev-python/pillow/9.4.0
                return {'type': 'sys_compat_version', 'category': parts[2], 'package': parts[3], 'version': parts[4]}
            elif len(parts) == 6:
                # /.sys/python-compat/dev-python/pillow/9.4.0/python3_13
                return {
                    'type': 'sys_compat_impl',
                    'category': parts[2],
                    'package': parts[3],
                    'version': parts[4],
                    'impl': parts[5]
                }

        elif parts[1] == 'python-compat-patch':
            if len(parts) == 2:
                # /.sys/python-compat-patch
                return {'type': 'sys_compat_patch'}
            elif len(parts) == 3:
                # /.sys/python-compat-patch/dev-python
                return {'type': 'sys_compat_patch_category', 'category': parts[2]}
            elif len(parts) == 4:
                # /.sys/python-compat-patch/dev-python/pillow
                return {'type': 'sys_compat_patch_package', 'category': parts[2], 'package': parts[3]}
            elif len(parts) == 5:
                # /.sys/python-compat-patch/dev-python/pillow/9.4.0.patch
                filename = parts[4]
                if filename.endswith('.patch'):
                    version = filename[:-6]  # Remove .patch
                    return {
                        'type': 'sys_compat_patch_file',
                        'category': parts[2],
                        'package': parts[3],
                        'version': version,
                        'filename': filename
                    }

        return {'type': 'invalid'}

    def _get_cached_content(self, path: str) -> Optional[bytes]:
        """Get cached content if valid."""
        if path in self._content_cache:
            content, timestamp = self._content_cache[path]
            if time.time() - timestamp < self.cache_ttl:
                return content
            else:
                del self._content_cache[path]
        return None
        
    def _cache_content(self, path: str, content: bytes):
        """Cache content with timestamp."""
        self._content_cache[path] = (content, time.time())
        
    def _get_cached_metadata(self, pypi_name: str) -> Optional[dict]:
        """Get cached PyPI metadata if valid."""
        if pypi_name in self._metadata_cache:
            metadata, timestamp = self._metadata_cache[pypi_name]
            if time.time() - timestamp < self.cache_ttl:
                return metadata
            else:
                del self._metadata_cache[pypi_name]
        return None
        
    def _cache_metadata(self, pypi_name: str, metadata: dict):
        """Cache PyPI metadata with timestamp."""
        self._metadata_cache[pypi_name] = (metadata, time.time())
        
    def _gentoo_to_pypi(self, gentoo_name: str) -> Optional[str]:
        """Convert Gentoo package name to PyPI name."""
        try:
            # First try the translator for known Gentoo packages
            pypi_name = self.name_translator.gentoo_to_pypi(gentoo_name)
            if pypi_name:
                return pypi_name
        except Exception as e:
            logger.debug(f"Name translation failed for {gentoo_name}: {e}")
        
        # For packages not in Gentoo repos, the name might already be a PyPI name
        # (we use PyPI names directly when translator doesn't know them)
        # Just return it as-is - PyPI will validate if it exists
        return gentoo_name
            
    def _get_package_metadata(self, pypi_name: str) -> Optional[dict]:
        """Get complete package metadata from PyPI."""
        # Check cache first
        cached = self._get_cached_metadata(pypi_name)
        if cached:
            return cached

        try:
            metadata = self.pypi_extractor.get_complete_package_info(pypi_name)
            if metadata:
                self._cache_metadata(pypi_name, metadata)
            return metadata
        except Exception as e:
            logger.error(f"Failed to fetch metadata for {pypi_name}: {e}")
            return None

    def _get_cached_package_json(self, pypi_name: str) -> Optional[dict]:
        """
        Get package JSON with filesystem-level caching.

        This wraps pypi_extractor.get_package_json() with an additional cache
        layer to avoid redundant calls within the same session. Multiple methods
        (versions, exists, upload_time, ebuild, manifest) all need the same JSON
        data, so caching here reduces calls from ~8 per package to 1.

        Args:
            pypi_name: PyPI package name

        Returns:
            Package JSON dict or None if not found
        """
        # Check filesystem-level cache first
        if pypi_name in self._package_json_cache:
            json_data, timestamp = self._package_json_cache[pypi_name]
            if time.time() - timestamp < self.cache_ttl:
                return json_data
            else:
                del self._package_json_cache[pypi_name]

        # Fetch from extractor (which has its own cache)
        json_data = self.pypi_extractor.get_package_json(pypi_name)

        # Cache the result (including None for negative caching)
        self._package_json_cache[pypi_name] = (json_data, time.time())

        return json_data

    def _package_exists(self, pypi_name: str) -> bool:
        """
        Lightweight check if a PyPI package exists.

        This is much faster than _get_package_versions() as it only checks
        if the package JSON is available, without processing versions.
        """
        cache_key = f"exists_{pypi_name}"
        if cache_key in self._metadata_cache:
            result, timestamp = self._metadata_cache[cache_key]
            if time.time() - timestamp < self.cache_ttl:
                return result

        try:
            json_data = self._get_cached_package_json(pypi_name)
            exists = json_data is not None and 'releases' in json_data
            self._metadata_cache[cache_key] = (exists, time.time())
            return exists
        except Exception:
            self._metadata_cache[cache_key] = (False, time.time())
            return False

    def _version_exists(self, pypi_name: str, gentoo_version: str) -> bool:
        """
        Check if a specific version exists for a package.

        Converts Gentoo version back to PyPI format and checks releases.
        """
        try:
            json_data = self._get_cached_package_json(pypi_name)
            if not json_data or 'releases' not in json_data:
                return False

            # Convert Gentoo version back to PyPI version
            pypi_ver = gentoo_version.replace('_alpha', 'a').replace('_beta', 'b').replace('_rc', 'rc')
            pypi_ver = pypi_ver.replace('_p', '.post').replace('.9999.', '.dev')

            # Check if this version exists in releases
            if pypi_ver in json_data['releases']:
                return True

            # Also check if any release translates to this Gentoo version
            for release_ver in json_data['releases']:
                if self._translate_version(release_ver) == gentoo_version:
                    return True

            return False
        except Exception:
            return False

    def _would_have_valid_python_compat(self, pypi_name: str, version: str) -> bool:
        """
        Check if this version would have valid PYTHON_COMPAT entries.
        
        This prevents listing versions that would generate empty PYTHON_COMPAT.
        """
        # Check cache first
        cache_key = f"python_compat_{pypi_name}_{version}"
        if cache_key in self._metadata_cache:
            return self._metadata_cache[cache_key]
            
        try:
            # Get version-specific package info
            package_info = self.pypi_extractor.get_complete_package_info(pypi_name, version)
            if not package_info:
                self._metadata_cache[cache_key] = False
                return False
            
            python_versions = package_info.get('python_versions', [])
            if not python_versions:
                # No Python version info - might be okay, let it through
                self._metadata_cache[cache_key] = True
                return True
            
            # Check if ANY version is valid for Gentoo
            python_compat = self.ebuild_extractor.format_python_compat(python_versions)
            
            # If PYTHON_COMPAT would be empty, don't show this version
            result = len(python_compat) > 0
            self._metadata_cache[cache_key] = result
            return result
            
        except Exception as e:
            logger.debug(f"Error checking Python compat for {pypi_name}-{version}: {e}")
            # On error, be permissive but don't cache
            return True
    
    def _translate_version(self, pypi_version: str) -> Optional[str]:
        """Translate PyPI version to Gentoo format."""
        import re

        if PypiVersion is None:
            # Fallback using regex patterns (same as pip_metadata.py)
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
        else:
            try:
                parsed = PypiVersion.parse_version(pypi_version)
                version = str(parsed) if parsed else None
            except Exception:
                version = None

        # Validate the translated version against Gentoo's format
        # Valid: digits, dots, and suffixes (_alpha, _beta, _pre, _rc, _p) with optional numbers
        # Invalid: hyphens (except -r for revision), bare letters, special chars
        if version:
            # Gentoo version regex based on PMS specification
            # Format: numeric(.numeric)* with optional suffixes and revision
            gentoo_version_re = re.compile(
                r'^'
                r'\d+(\.\d+)*'  # Base version: 1.2.3
                r'([a-z])?'  # Optional single letter suffix (rare but valid)
                r'(_alpha\d*|_beta\d*|_pre\d*|_rc\d*|_p\d*)*'  # Gentoo suffixes
                r'(-r\d+)?'  # Optional revision
                r'$'
            )
            if not gentoo_version_re.match(version):
                logger.debug(f"Invalid Gentoo version format: {version} (from {pypi_version})")
                return None

        return version
            
    def _get_package_versions(self, pypi_name: str) -> List[str]:
        """Get available Gentoo versions for a PyPI package."""
        # Check if we have cached versions for this package
        cache_key = f"versions_{pypi_name}"
        if cache_key in self._metadata_cache:
            versions, timestamp = self._metadata_cache[cache_key]
            if time.time() - timestamp < self.cache_ttl:
                return versions
            else:
                del self._metadata_cache[cache_key]
        
        try:
            # Get raw PyPI JSON data which contains releases
            json_data = self._get_cached_package_json(pypi_name)
            if not json_data or 'releases' not in json_data:
                # Cache empty result to avoid repeated failed lookups
                self._metadata_cache[cache_key] = ([], time.time())
                return []
            
            # Apply version filters if configured
            releases = json_data['releases']
            if self.version_filter_chain:
                # Build version metadata dict for filtering
                versions_metadata = {}
                for version, release_info in releases.items():
                    # Get version-specific requires_python from release files
                    # (the global info.requires_python is only for the latest version)
                    version_requires_python = None
                    for file_info in release_info:
                        if file_info.get('requires_python'):
                            version_requires_python = file_info['requires_python']
                            break

                    # Build version-specific info dict
                    version_info = {
                        'requires_python': version_requires_python,
                        # Note: classifiers are not available per-version in bulk JSON,
                        # so we don't include them - requires_python is authoritative
                    }

                    versions_metadata[version] = {
                        'urls': release_info,  # Release info contains list of files
                        'info': version_info,  # Version-specific info
                        'pypi_name': pypi_name,
                        'version': version
                    }
                
                # Apply filters
                filtered_versions = self.version_filter_chain.filter_versions(pypi_name, versions_metadata)
                releases = {v: json_data['releases'][v] for v in filtered_versions}
            
            gentoo_versions = []
            for pypi_ver in releases:
                # Check for interrupts between version iterations
                check_interrupt()

                # Skip versions with no files (yanked or empty releases)
                if not releases[pypi_ver]:
                    continue

                gentoo_ver = self._translate_version(pypi_ver)
                if gentoo_ver:
                    gentoo_versions.append(gentoo_ver)
                    
            sorted_versions = sorted(gentoo_versions, reverse=True)  # Newest first

            # Apply version limit if configured (for faster readdir)
            if self.max_versions > 0 and len(sorted_versions) > self.max_versions:
                sorted_versions = sorted_versions[:self.max_versions]

            # Cache the versions list
            self._metadata_cache[cache_key] = (sorted_versions, time.time())

            return sorted_versions
            
        except Exception as e:
            logger.debug(f"Error getting versions for {pypi_name}: {e}")
            # Cache empty result to avoid repeated failed lookups
            self._metadata_cache[cache_key] = ([], time.time())
            return []
    
    def _get_package_upload_time(self, pypi_name: str, pypi_version: Optional[str] = None) -> float:
        """Get the upload timestamp for a PyPI package or specific version.
        
        Args:
            pypi_name: PyPI package name
            pypi_version: Optional specific version (uses latest if None)
            
        Returns:
            Unix timestamp of upload time, or current time if not found
        """
        try:
            # Get package JSON data with release info
            json_data = self._get_cached_package_json(pypi_name)
            if not json_data:
                return time.time()
            
            # If no specific version, use the latest
            if not pypi_version:
                pypi_version = json_data.get('info', {}).get('version')
                if not pypi_version:
                    return time.time()
            
            # Get the release data for this version
            releases = json_data.get('releases', {})
            if pypi_version not in releases:
                return time.time()
            
            # Get upload time from the first file in this release
            version_files = releases[pypi_version]
            if version_files and len(version_files) > 0:
                # Parse the upload_time from the first file
                upload_time_str = version_files[0].get('upload_time')
                if upload_time_str:
                    # PyPI provides time in ISO format: "2023-10-15T12:34:56"
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(upload_time_str.replace('Z', '+00:00'))
                        return dt.timestamp()
                    except:
                        # Try alternative parsing
                        try:
                            dt = datetime.strptime(upload_time_str, "%Y-%m-%dT%H:%M:%S")
                            return dt.timestamp()
                        except:
                            pass
            
            return time.time()
            
        except Exception as e:
            logger.debug(f"Error getting upload time for {pypi_name}: {e}")
            return time.time()
        
    def access(self, path, mode):
        """Check file access permissions."""
        # Allow read access to all files in our filesystem
        if path in self.static_files:
            return 0
            
        parsed = self._parse_path(path)
        if parsed['type'] != 'unknown':
            return 0
            
        raise FuseOSError(errno.EACCES)
        
    def getxattr(self, path, name, position=0):
        """Get extended file attributes."""
        # We don't support extended attributes, but return empty instead of error
        # This prevents the traceback spam in logs
        # Use ENODATA if available, otherwise ENOTSUP
        error_code = getattr(errno, 'ENODATA', errno.ENOTSUP)
        raise FuseOSError(error_code)
        
    def listxattr(self, path):
        """List extended file attributes."""
        # Return empty list - we don't have any extended attributes
        return []
        
    def getattr(self, path, fh=None):
        """Get file attributes."""
        parsed = self._parse_path(path)
        
        # Default to current time
        current_time = time.time()
        
        # Default attributes
        attrs = {
            'st_uid': os.getuid(),
            'st_gid': os.getgid(),
            'st_atime': current_time,
            'st_mtime': current_time,
            'st_ctime': current_time,
        }
        
        # Check for static files first (before directory checks)
        if path in self.static_files:
            # Static file
            content = self.static_files[path]
            attrs.update({
                'st_mode': stat.S_IFREG | 0o644,
                'st_nlink': 1,
                'st_size': len(content),
            })
        elif parsed['type'] == 'root':
            # Root directory
            attrs.update({
                'st_mode': stat.S_IFDIR | 0o755,
                'st_nlink': 2,
                'st_size': 4096,
            })
        elif parsed['type'] == 'profiles_file':
            # Static profiles files
            if parsed['filename'] == 'repo_name':
                content = b"portage-pip-fuse\n"
                attrs.update({
                    'st_mode': stat.S_IFREG | 0o644,
                    'st_nlink': 1,
                    'st_size': len(content),
                })
            else:
                raise FuseOSError(errno.ENOENT)
        elif parsed['type'] == 'metadata_file':
            # Static metadata files
            if parsed['filename'] == 'layout.conf':
                content = self._generate_layout_conf().encode('utf-8')
                attrs.update({
                    'st_mode': stat.S_IFREG | 0o644,
                    'st_nlink': 1,
                    'st_size': len(content),
                })
            else:
                raise FuseOSError(errno.ENOENT)
        elif parsed['type'] in ['profiles', 'metadata', 'eclass', 'category']:
            # Static directories - use current time
            attrs.update({
                'st_mode': stat.S_IFDIR | 0o755,
                'st_nlink': 2,
                'st_size': 4096,
            })
        elif parsed['type'] == 'package':
            # Package directory - use latest package upload time (unless disabled)
            gentoo_name = parsed['package']
            pypi_name = self._gentoo_to_pypi(gentoo_name)
            if pypi_name and not self.no_timestamps:
                upload_time = self._get_package_upload_time(pypi_name)
                attrs.update({
                    'st_mode': stat.S_IFDIR | 0o755,
                    'st_nlink': 2,
                    'st_size': 4096,
                    'st_mtime': upload_time,
                    'st_ctime': upload_time,
                })
            else:
                attrs.update({
                    'st_mode': stat.S_IFDIR | 0o755,
                    'st_nlink': 2,
                    'st_size': 4096,
                })
        elif parsed['type'] in ['ebuild', 'package_metadata', 'manifest']:
            # Dynamic file - check if it should exist by verifying PyPI package
            gentoo_name = parsed['package']
            pypi_name = self._gentoo_to_pypi(gentoo_name)
            if not pypi_name:
                logger.debug(f"Cannot translate package name: {gentoo_name}")
                raise FuseOSError(errno.ENOENT)

            try:
                # Use lightweight existence check instead of full version listing
                if not self._package_exists(pypi_name):
                    logger.debug(f"Package not found on PyPI: {pypi_name}")
                    raise FuseOSError(errno.ENOENT)

                # For ebuild files, verify the specific version exists
                if parsed['type'] == 'ebuild':
                    if not self._version_exists(pypi_name, parsed['version']):
                        logger.debug(f"Version {parsed['version']} not found for {pypi_name}")
                        raise FuseOSError(errno.ENOENT)

            except FuseOSError:
                # Re-raise FUSE errors
                raise
            except Exception as e:
                # If we can't verify, deny access to prevent broken files
                logger.debug(f"Cannot verify package {pypi_name}: {e}")
                raise FuseOSError(errno.ENOENT)
                        
            # File exists, set attributes with accurate size
            attrs.update({
                'st_mode': stat.S_IFREG | 0o644,
                'st_nlink': 1,
                'st_size': 2048,  # Default estimate, will be updated below
            })
            
            # For ebuild files, try to get the specific version's upload time (unless disabled)
            if parsed['type'] == 'ebuild' and pypi_name and not self.no_timestamps:
                # Need to convert Gentoo version back to PyPI version
                # This is a simplified reverse translation
                gentoo_ver = parsed['version']
                pypi_ver = gentoo_ver.replace('_alpha', 'a').replace('_beta', 'b').replace('_rc', 'rc')
                pypi_ver = pypi_ver.replace('_p', '.post').replace('.9999.', '.dev')
                
                upload_time = self._get_package_upload_time(pypi_name, pypi_ver)
                attrs['st_mtime'] = upload_time
                attrs['st_ctime'] = upload_time
            elif pypi_name and not self.no_timestamps:
                # For other files (metadata.xml, Manifest), use latest package time
                upload_time = self._get_package_upload_time(pypi_name)
                attrs['st_mtime'] = upload_time
                attrs['st_ctime'] = upload_time
            
            # Get actual size by generating content and caching it
            # This ensures vim and other tools see the correct file size
            # IMPORTANT: We must cache the content here so read() returns the same
            # content that we used to compute the size. Otherwise, if content
            # generation is non-deterministic (e.g., PYTHON_COMPAT changes),
            # the file will appear truncated.
            try:
                # First check if content is already cached
                cached_content = self._get_cached_content(path)
                if cached_content is not None:
                    attrs['st_size'] = len(cached_content)
                elif parsed['type'] == 'ebuild':
                    # Extract category from parsed path
                    category = parsed.get('category', 'dev-python')
                    content = self._generate_ebuild(category, parsed['package'], parsed['version'])
                    if content is None:
                        # Ebuild can't be generated (e.g., empty PYTHON_COMPAT)
                        raise FuseOSError(errno.ENOENT)
                    content_bytes = content.encode('utf-8')
                    attrs['st_size'] = len(content_bytes)
                    # Cache so read() returns the same content
                    self._cache_content(path, content_bytes)
                elif parsed['type'] == 'package_metadata':
                    content = self._generate_metadata_xml(pypi_name)
                    content_bytes = content.encode('utf-8')
                    attrs['st_size'] = len(content_bytes)
                    self._cache_content(path, content_bytes)
                elif parsed['type'] == 'manifest':
                    content = self._generate_manifest(parsed['category'], parsed['package'])
                    content_bytes = content.encode('utf-8')
                    attrs['st_size'] = len(content_bytes)
                    self._cache_content(path, content_bytes)
            except FuseOSError:
                # Re-raise FUSE errors (including our ENOENT for invalid ebuilds)
                raise
            except Exception as e:
                # If content generation fails, use the default estimate
                # This is better than failing completely
                logger.debug(f"Could not determine accurate size for {path}: {e}")
                # Keep the default 2048 estimate
        # Handle .sys/ virtual filesystem paths
        elif parsed['type'] in ('sys_root', 'sys_deps', 'sys_deps_category',
                                'sys_patch', 'sys_patch_category'):
            # Static .sys directories
            attrs.update({
                'st_mode': stat.S_IFDIR | 0o755,
                'st_nlink': 2,
                'st_size': 4096,
            })
        elif parsed['type'] in ('sys_deps_package', 'sys_deps_version', 'sys_patch_package'):
            # Dynamic .sys directories - verify package exists
            if self.patch_store is None:
                raise FuseOSError(errno.ENOENT)
            gentoo_name = parsed['package']
            pypi_name = self._gentoo_to_pypi(gentoo_name)
            if not pypi_name or not self._package_exists(pypi_name):
                raise FuseOSError(errno.ENOENT)
            attrs.update({
                'st_mode': stat.S_IFDIR | 0o755,
                'st_nlink': 2,
                'st_size': 4096,
            })
        elif parsed['type'] == 'sys_deps_dep':
            # Dependency file in .sys/dependencies/.../version/
            if self.patch_store is None:
                raise FuseOSError(errno.ENOENT)
            # These are virtual files representing dependencies
            dep_name = parsed['dep']
            attrs.update({
                'st_mode': stat.S_IFREG | 0o644,
                'st_nlink': 1,
                'st_size': len(dep_name.encode('utf-8')),
            })
        elif parsed['type'] == 'sys_patch_file':
            # Patch file in .sys/dependencies-patch/
            if self.patch_store is None:
                raise FuseOSError(errno.ENOENT)
            category = parsed['category']
            package = parsed['package']
            version = parsed['version']
            content = self.patch_store.generate_patch_file(category, package, version)
            attrs.update({
                'st_mode': stat.S_IFREG | 0o644,
                'st_nlink': 1,
                'st_size': len(content.encode('utf-8')),
            })
        # Handle .sys/python-compat/ virtual filesystem paths
        elif parsed['type'] in ('sys_compat', 'sys_compat_category',
                                'sys_compat_patch', 'sys_compat_patch_category'):
            # Static .sys/python-compat directories
            attrs.update({
                'st_mode': stat.S_IFDIR | 0o755,
                'st_nlink': 2,
                'st_size': 4096,
            })
        elif parsed['type'] in ('sys_compat_package', 'sys_compat_version', 'sys_compat_patch_package'):
            # Dynamic .sys/python-compat directories - verify package exists
            if self.compat_patch_store is None:
                raise FuseOSError(errno.ENOENT)
            gentoo_name = parsed['package']
            pypi_name = self._gentoo_to_pypi(gentoo_name)
            if not pypi_name or not self._package_exists(pypi_name):
                raise FuseOSError(errno.ENOENT)
            attrs.update({
                'st_mode': stat.S_IFDIR | 0o755,
                'st_nlink': 2,
                'st_size': 4096,
            })
        elif parsed['type'] == 'sys_compat_impl':
            # Implementation file in .sys/python-compat/.../version/
            if self.compat_patch_store is None:
                raise FuseOSError(errno.ENOENT)
            impl_name = parsed['impl']
            attrs.update({
                'st_mode': stat.S_IFREG | 0o644,
                'st_nlink': 1,
                'st_size': len(impl_name.encode('utf-8')),
            })
        elif parsed['type'] == 'sys_compat_patch_file':
            # Patch file in .sys/python-compat-patch/
            if self.compat_patch_store is None:
                raise FuseOSError(errno.ENOENT)
            category = parsed['category']
            package = parsed['package']
            version = parsed['version']
            content = self.compat_patch_store.generate_patch_file(category, package, version)
            attrs.update({
                'st_mode': stat.S_IFREG | 0o644,
                'st_nlink': 1,
                'st_size': len(content.encode('utf-8')),
            })
        elif parsed['type'] == 'invalid':
            # Invalid path - return ENOENT
            raise FuseOSError(errno.ENOENT)
        else:
            # Unknown path type - this is normal for filesystem exploration
            logger.debug(f"Path not found: {path} (type: {parsed['type']})")
            raise FuseOSError(errno.ENOENT)

        return attrs
            
    def readdir(self, path, fh):
        """Read directory contents."""
        # Clear interrupt flag at start of operation
        InterruptChecker.clear()

        parsed = self._parse_path(path)
        entries = ['.', '..']

        try:
            if parsed['type'] == 'root':
                # Root directory - show main overlay structure
                entries.extend(['dev-python', 'profiles', 'metadata', 'eclass'])
                # Add .sys if patching is enabled
                if self.patch_store is not None:
                    entries.append('.sys')

            elif parsed['type'] == 'profiles':
                entries.append('repo_name')

            elif parsed['type'] == 'metadata':
                entries.append('layout.conf')

            elif parsed['type'] == 'eclass':
                # Empty for now - could add eclasses later
                pass

            elif parsed['type'] == 'category' and parsed['category'] == 'dev-python':
                # Check cache first
                cache_key = 'dev-python'
                use_cache = False
                if cache_key in self._category_cache:
                    cached_packages, timestamp = self._category_cache[cache_key]
                    if time.time() - timestamp < self.cache_ttl:
                        logger.debug(f"Using cached package list ({len(cached_packages)} packages)")
                        entries.extend(cached_packages)
                        use_cache = True
                    else:
                        del self._category_cache[cache_key]

                if not use_cache:
                    # Use the configured filter to get packages
                    try:
                        start_time = time.time()

                        logger.info(f"Listing packages using filter: {self.package_filter.get_description()}")

                        # Get PyPI packages from the filter
                        pypi_packages = self.package_filter.get_packages()

                        # Convert PyPI names to Gentoo names
                        gentoo_packages = []
                        for pypi_name in pypi_packages:
                            check_interrupt()

                            gentoo_name = self.name_translator.pypi_to_gentoo(pypi_name)
                            if gentoo_name:
                                gentoo_packages.append(gentoo_name)
                            else:
                                # Use PyPI name directly (normalized)
                                gentoo_packages.append(pypi_name.lower().replace('_', '-').replace('.', '-'))

                        sorted_packages = sorted(gentoo_packages)

                        # Cache the result
                        self._category_cache[cache_key] = (sorted_packages, time.time())

                        entries.extend(sorted_packages)

                        elapsed = time.time() - start_time
                        logger.info(f"Listed {len(gentoo_packages)} packages in {elapsed:.2f} seconds")

                    except InterruptedError:
                        logger.info("Package listing interrupted")
                    except Exception as e:
                        logger.error(f"Error listing packages: {e}")
                        logger.warning("Package listing failed, returning empty directory")

            elif parsed['type'] == 'package':
                # List versions and files for a package
                gentoo_name = parsed['package']
                pypi_name = self._gentoo_to_pypi(gentoo_name)

                if pypi_name:
                    try:
                        versions = self._get_package_versions(pypi_name)
                        if versions:  # Only list files if package exists on PyPI
                            # Add ebuild files for each version
                            for version in versions:
                                entries.append(f"{gentoo_name}-{version}.ebuild")

                            # Add metadata files
                            entries.extend(['metadata.xml', 'Manifest'])
                        else:
                            logger.debug(f"No versions found for {pypi_name}, not listing files")
                    except InterruptedError:
                        logger.info(f"Version listing interrupted for {pypi_name}")
                        # Return minimal result on interrupt
                    except Exception as e:
                        logger.error(f"Error listing files for {gentoo_name}: {e}")

            # Handle .sys/ virtual filesystem directories
            elif parsed['type'] == 'sys_root':
                # /.sys - show all virtual filesystem sections
                if self.patch_store is not None:
                    entries.extend(['dependencies', 'dependencies-patch'])
                if self.compat_patch_store is not None:
                    entries.extend(['python-compat', 'python-compat-patch'])

            elif parsed['type'] == 'sys_deps':
                # /.sys/dependencies - show categories
                if self.patch_store is not None:
                    entries.append('dev-python')

            elif parsed['type'] == 'sys_deps_category':
                # /.sys/dependencies/dev-python - show packages with patches or cached packages
                if self.patch_store is not None:
                    # Show packages that have patches
                    for cat, pkg, ver in self.patch_store.list_patched_packages():
                        if cat == parsed['category'] and pkg not in entries:
                            entries.append(pkg)
                    # Also show all cached packages for convenience
                    cache_key = 'dev-python'
                    if cache_key in self._category_cache:
                        cached_packages, _ = self._category_cache[cache_key]
                        for pkg in cached_packages:
                            if pkg not in entries:
                                entries.append(pkg)

            elif parsed['type'] == 'sys_deps_package':
                # /.sys/dependencies/dev-python/requests - show versions
                # IMPORTANT: Use cached versions only to prevent blocking
                if self.patch_store is not None:
                    gentoo_name = parsed['package']
                    pypi_name = self._gentoo_to_pypi(gentoo_name)
                    if pypi_name:
                        # Check if versions are cached
                        cache_key = f"versions_{pypi_name}"
                        if cache_key in self._metadata_cache:
                            versions, _ = self._metadata_cache[cache_key]
                            entries.extend(versions)
                        # else: versions not cached, return empty (user can cd directly)
                        entries.append('_all')  # Always show _all for global patches

            elif parsed['type'] == 'sys_deps_version':
                # /.sys/dependencies/dev-python/requests/2.31.0 - show dependencies
                # IMPORTANT: Use cached data only to prevent blocking
                if self.patch_store is not None:
                    gentoo_name = parsed['package']
                    version = parsed['version']
                    category = parsed['category']
                    # Show only patched deps (from local patch store) - no network calls
                    # Original deps would require fetching package info which can block
                    patches = self.patch_store.get_patches(category, gentoo_name, version)
                    for patch in patches:
                        if patch.action == 'add' and patch.dependency:
                            entries.append(self._encode_dep_filename(patch.dependency))
                    # Note: Full dep listing requires 'cat' on a specific dep file

            elif parsed['type'] == 'sys_patch':
                # /.sys/dependencies-patch - show categories
                if self.patch_store is not None:
                    entries.append('dev-python')

            elif parsed['type'] == 'sys_patch_category':
                # /.sys/dependencies-patch/dev-python - show packages with patches
                if self.patch_store is not None:
                    for cat, pkg, ver in self.patch_store.list_patched_packages():
                        if cat == parsed['category'] and pkg not in entries:
                            entries.append(pkg)

            elif parsed['type'] == 'sys_patch_package':
                # /.sys/dependencies-patch/dev-python/requests - show version.patch files
                if self.patch_store is not None:
                    category = parsed['category']
                    package = parsed['package']
                    versions = self.patch_store.get_package_versions_with_patches(category, package)
                    for ver in versions:
                        entries.append(f"{ver}.patch")

            # Handle .sys/python-compat/ directories
            elif parsed['type'] == 'sys_compat':
                # /.sys/python-compat - show categories
                if self.compat_patch_store is not None:
                    entries.append('dev-python')

            elif parsed['type'] == 'sys_compat_category':
                # /.sys/python-compat/dev-python - show packages with patches or cached packages
                if self.compat_patch_store is not None:
                    # Show packages that have patches
                    for cat, pkg, ver in self.compat_patch_store.list_patched_packages():
                        if cat == parsed['category'] and pkg not in entries:
                            entries.append(pkg)
                    # Also show all cached packages for convenience
                    cache_key = 'dev-python'
                    if cache_key in self._category_cache:
                        cached_packages, _ = self._category_cache[cache_key]
                        for pkg in cached_packages:
                            if pkg not in entries:
                                entries.append(pkg)

            elif parsed['type'] == 'sys_compat_package':
                # /.sys/python-compat/dev-python/pillow - show versions
                if self.compat_patch_store is not None:
                    gentoo_name = parsed['package']
                    pypi_name = self._gentoo_to_pypi(gentoo_name)
                    if pypi_name:
                        versions = self._get_package_versions(pypi_name)
                        entries.extend(versions)
                        entries.append('_all')  # Always show _all for global patches

            elif parsed['type'] == 'sys_compat_version':
                # /.sys/python-compat/dev-python/pillow/9.4.0 - show Python implementations
                if self.compat_patch_store is not None:
                    gentoo_name = parsed['package']
                    version = parsed['version']
                    category = parsed['category']
                    pypi_name = self._gentoo_to_pypi(gentoo_name)
                    if pypi_name:
                        # Get current PYTHON_COMPAT (original + patches applied)
                        impls = self._get_package_python_compat_for_sys(category, gentoo_name, pypi_name, version)
                        entries.extend(impls)

            elif parsed['type'] == 'sys_compat_patch':
                # /.sys/python-compat-patch - show categories
                if self.compat_patch_store is not None:
                    entries.append('dev-python')

            elif parsed['type'] == 'sys_compat_patch_category':
                # /.sys/python-compat-patch/dev-python - show packages with patches
                if self.compat_patch_store is not None:
                    for cat, pkg, ver in self.compat_patch_store.list_patched_packages():
                        if cat == parsed['category'] and pkg not in entries:
                            entries.append(pkg)

            elif parsed['type'] == 'sys_compat_patch_package':
                # /.sys/python-compat-patch/dev-python/pillow - show version.patch files
                if self.compat_patch_store is not None:
                    category = parsed['category']
                    package = parsed['package']
                    versions = self.compat_patch_store.get_package_versions_with_patches(category, package)
                    for ver in versions:
                        entries.append(f"{ver}.patch")

        except InterruptedError:
            logger.info(f"readdir interrupted for {path}")
            # Return minimal result on interrupt

        return entries
        
    def read(self, path, length, offset, fh):
        """Read file contents."""
        # Clear interrupt flag at start of operation
        InterruptChecker.clear()

        # Check static files first
        if path in self.static_files:
            content = self.static_files[path]
            return content[offset:offset + length]

        # Check static files
        parsed = self._parse_path(path)
        if parsed['type'] == 'profiles_file' and parsed['filename'] == 'repo_name':
            content = b"portage-pip-fuse\n"
            return content[offset:offset + length]
        elif parsed['type'] == 'metadata_file' and parsed['filename'] == 'layout.conf':
            content = self._generate_layout_conf().encode('utf-8')
            return content[offset:offset + length]

        # Handle .sys/ file reads
        elif parsed['type'] == 'sys_deps_dep':
            # Read a dependency file - just return the dep name
            content = parsed['dep'].encode('utf-8')
            return content[offset:offset + length]

        elif parsed['type'] == 'sys_patch_file':
            # Read a patch file
            if self.patch_store is None:
                raise FuseOSError(errno.ENOENT)
            category = parsed['category']
            package = parsed['package']
            version = parsed['version']
            content = self.patch_store.generate_patch_file(category, package, version).encode('utf-8')
            return content[offset:offset + length]

        # Handle .sys/python-compat/ file reads
        elif parsed['type'] == 'sys_compat_impl':
            # Read an implementation file - just return the impl name
            content = parsed['impl'].encode('utf-8')
            return content[offset:offset + length]

        elif parsed['type'] == 'sys_compat_patch_file':
            # Read a PYTHON_COMPAT patch file
            if self.compat_patch_store is None:
                raise FuseOSError(errno.ENOENT)
            category = parsed['category']
            package = parsed['package']
            version = parsed['version']
            content = self.compat_patch_store.generate_patch_file(category, package, version).encode('utf-8')
            return content[offset:offset + length]

        try:
            # Try cache
            content = self._get_cached_content(path)
            if content is None:
                # Generate dynamic content
                content = self._generate_content(path)
                if content is not None:
                    if isinstance(content, str):
                        content = content.encode('utf-8')
                    self._cache_content(path, content)

            if content is None:
                raise FuseOSError(errno.ENOENT)

            return content[offset:offset + length]

        except InterruptedError:
            logger.info(f"read interrupted for {path}")
            raise FuseOSError(errno.EINTR)
        
    def open(self, path, flags):
        """Open a file."""
        # Allow opening static files and dynamic files
        if path in self.static_files:
            return 0
            
        # Check if it's a valid dynamic file
        parsed = self._parse_path(path)
        if parsed['type'] in ['profiles_file', 'metadata_file']:
            # Allow opening static files
            return 0
        elif parsed['type'] in ['ebuild', 'package_metadata', 'manifest']:
            # Additional verification for ebuilds
            if parsed['type'] == 'ebuild':
                gentoo_name = parsed['package']
                pypi_name = self._gentoo_to_pypi(gentoo_name)
                if not pypi_name:
                    logger.debug(f"Cannot translate package name: {gentoo_name}")
                    raise FuseOSError(errno.ENOENT)
            return 0
        elif parsed['type'] in ['sys_deps_dep', 'sys_patch_file',
                                  'sys_compat_impl', 'sys_compat_patch_file']:
            # Allow opening .sys files
            return 0

        logger.debug(f"Cannot open path: {path} (type: {parsed['type']})")
        raise FuseOSError(errno.ENOENT)
        
    def _generate_content(self, path: str) -> Optional[str]:
        """Generate dynamic content for a given path."""
        parsed = self._parse_path(path)
        
        try:
            if parsed['type'] == 'ebuild':
                return self._generate_ebuild(
                    parsed['category'], parsed['package'], parsed['version']
                )
            elif parsed['type'] == 'package_metadata':
                return self._generate_package_metadata(
                    parsed['category'], parsed['package']
                )
            elif parsed['type'] == 'manifest':
                return self._generate_manifest(
                    parsed['category'], parsed['package']
                )
        except Exception as e:
            logger.error(f"Error generating content for {path}: {e}")
            
        return None

    def _escape_double_quotes(self, value: str) -> str:
        """
        Escape a string for use in double-quoted shell/bash context.

        In double-quoted strings, these characters have special meaning and
        must be escaped with a backslash:
        - \\ (backslash) - escape character itself
        - $ - variable expansion
        - ` - command substitution
        - " - ends the string
        - ! - history expansion (bash interactive, but escape for safety)

        Uses standard shell escaping with backslashes, which is the same
        approach used by shlex but for double-quote context.

        Args:
            value: The string to escape

        Returns:
            Escaped string safe for double-quoted shell context

        Examples:
            >>> fs._escape_double_quotes('simple text')
            'simple text'
            >>> fs._escape_double_quotes('has `backticks`')
            'has \\\\`backticks\\\\`'
            >>> fs._escape_double_quotes('costs $5')
            'costs \\\\$5'
        """
        if not value:
            return value
        # Order matters: escape backslashes first, then other special chars
        value = value.replace('\\', '\\\\')
        value = value.replace('$', '\\$')
        value = value.replace('`', '\\`')
        value = value.replace('"', '\\"')
        value = value.replace('!', '\\!')
        return value

    def _generate_ebuild(self, category: str, package: str, version: str) -> Optional[str]:
        """Generate ebuild content."""
        # Convert Gentoo package name to PyPI name
        pypi_name = self._gentoo_to_pypi(package)
        if not pypi_name:
            return None
            
        # Get package metadata
        metadata = self._get_package_metadata(pypi_name)
        if not metadata:
            return None
            
        # Find the PyPI version that corresponds to this Gentoo version
        # We need to use raw PyPI JSON data since the metadata structure doesn't have all_versions
        pypi_version = None
        try:
            json_data = self._get_cached_package_json(pypi_name)
            if json_data and 'releases' in json_data:
                for pypi_ver in json_data['releases']:
                    if self._translate_version(pypi_ver) == version:
                        pypi_version = pypi_ver
                        break
        except Exception as e:
            logger.debug(f"Error finding PyPI version for {version}: {e}")
            return None
                
        if not pypi_version:
            return None
            
        try:
            # Get specific version info
            version_metadata = self.pypi_extractor.get_complete_package_info(pypi_name, pypi_version)
            if not version_metadata:
                return None
                
            # Prepare ebuild data
            ebuild_data = self.ebuild_extractor.prepare_ebuild_data(version_metadata)
            if not ebuild_data:
                logger.error(f"Failed to prepare ebuild data for {package}-{version}")
                return None

            # Apply PYTHON_COMPAT patches if enabled
            python_compat = ebuild_data.get('PYTHON_COMPAT', [])
            if self.compat_patch_store is not None:
                python_compat = self.compat_patch_store.apply_patches(
                    category, package, version, python_compat
                )
                ebuild_data['PYTHON_COMPAT'] = python_compat

            # Check if PYTHON_COMPAT would be empty - if so, hide this ebuild
            if not python_compat:
                logger.debug(f"Hiding {package}-{version}: no valid PYTHON_COMPAT")
                return None

            # Apply dependency patches if enabled
            if self.patch_store is not None and ebuild_data.get('RDEPEND'):
                original_rdepend = ebuild_data['RDEPEND']
                patched_rdepend = self.patch_store.apply_patches(
                    category, package, version, original_rdepend
                )
                ebuild_data['RDEPEND'] = patched_rdepend

                # Also patch OPTIONAL_DEPEND if present
                if ebuild_data.get('OPTIONAL_DEPEND'):
                    for use_flag, deps in ebuild_data['OPTIONAL_DEPEND'].items():
                        patched_deps = self.patch_store.apply_patches(
                            category, package, version, deps
                        )
                        ebuild_data['OPTIONAL_DEPEND'][use_flag] = patched_deps

            # Generate ebuild from template
            return self._format_ebuild(ebuild_data)
            
        except Exception as e:
            logger.error(f"Error generating ebuild for {package}-{version}: {e}")
            return None
    
    def _format_ebuild(self, data: dict) -> str:
        """Format ebuild data into ebuild file content."""
        ebuild_lines = [
            f"# Copyright 2026 Gentoo Authors",
            f"# Distributed under the terms of the GNU General Public License v2",
            f"",
            f"EAPI=8",
            f"",
            f"DISTUTILS_USE_PEP517=setuptools",
            f"PYTHON_COMPAT=( {' '.join(data.get('PYTHON_COMPAT', ['python3_11', 'python3_12', 'python3_13']))} )",
            f"# PYPI_* variables must be set before inherit",
            f"PYPI_NO_NORMALIZE=1",
            f"PYPI_PN=\"{data.get('PYPI_PN', data.get('PN', ''))}\"",
            f"PYPI_PV=\"{data.get('PYPI_PV', data.get('PV', ''))}\"",
            f"",
            f"inherit distutils-r1 pypi",
            f"",
            f"DESCRIPTION=\"{self._escape_double_quotes(data.get('DESCRIPTION', 'Python package from PyPI'))}\"",
            f"HOMEPAGE=\"{data.get('HOMEPAGE', 'https://pypi.org/project/' + data.get('PN', ''))}\"",
            f"",
            f"LICENSE=\"{data.get('LICENSE', 'unknown')}\"",
            f"SLOT=\"{data.get('SLOT', '0')}\"",
            f"KEYWORDS=\"{data.get('KEYWORDS', '~amd64 ~x86')}\"",
        ]
        
        # Add IUSE for PyPI extras as USE flags
        if data.get('IUSE'):
            ebuild_lines.append(f"")
            ebuild_lines.append(f"IUSE=\"{' '.join(data['IUSE'])}\"")
        
        # Add dependencies if present
        if data.get('DEPEND'):
            ebuild_lines.append(f"")
            ebuild_lines.append(f"DEPEND=\"")
            for dep in data['DEPEND']:
                ebuild_lines.append(f"\t{dep}")
            ebuild_lines.append(f"\"")
            
        if data.get('RDEPEND'):
            ebuild_lines.append(f"")
            ebuild_lines.append(f"RDEPEND=\"")
            for dep in data['RDEPEND']:
                ebuild_lines.append(f"\t{dep}")
            ebuild_lines.append(f"\"")
            
        # Add optional dependencies with USE flags (grouped properly per Gentoo style)
        if data.get('OPTIONAL_DEPEND'):
            ebuild_lines.append(f"")
            ebuild_lines.append(f"RDEPEND+=\"")
            for use_flag, deps in data['OPTIONAL_DEPEND'].items():
                if deps:  # Only add if there are dependencies
                    deps_str = ' '.join(deps)
                    ebuild_lines.append(f"\t{use_flag}? ( {deps_str} )")
            ebuild_lines.append(f"\"")

        # Add python_compile_pre to clean bundled autoconf caches between Python impls
        # This fixes packages like gevent that bundle c-ares/libev with config.cache
        ebuild_lines.extend([
            f"",
            f"python_compile_pre() {{",
            f"\t# Clean autoconf caches from bundled deps to avoid conflicts between Python impls",
            f"\tfind \"${{S}}\" -name config.cache -delete 2>/dev/null || true",
            f"}}",
        ])

        return '\n'.join(ebuild_lines) + '\n'
        
    def _generate_package_metadata(self, category: str, package: str) -> Optional[str]:
        """Generate metadata.xml content."""
        pypi_name = self._gentoo_to_pypi(package)
        if not pypi_name:
            return None
            
        metadata = self._get_package_metadata(pypi_name)
        if not metadata:
            return None
            
        info = metadata.get('metadata', {})
        description = info.get('summary', 'Python package from PyPI')
        homepage = info.get('homepage') or f"https://pypi.org/project/{pypi_name}"
        
        xml_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<!DOCTYPE pkgmetadata SYSTEM "https://www.gentoo.org/dtd/metadata.dtd">',
            '<pkgmetadata>',
            '\t<maintainer type="project">',
            '\t\t<email>python@gentoo.org</email>',
            '\t\t<name>Python</name>',
            '\t</maintainer>',
            f'\t<longdescription>{description}</longdescription>',
            '\t<upstream>',
            f'\t\t<remote-id type="pypi">{pypi_name}</remote-id>',
            '\t</upstream>',
            '</pkgmetadata>'
        ]
        
        return '\n'.join(xml_lines) + '\n'
        
    def _generate_manifest(self, category: str, package: str) -> Optional[str]:
        """Generate Manifest content."""
        pypi_name = self._gentoo_to_pypi(package)
        if not pypi_name:
            return None

        # Get the versions from raw PyPI JSON (get_complete_package_info doesn't have all_versions)
        try:
            json_data = self._get_cached_package_json(pypi_name)
            if not json_data or 'releases' not in json_data:
                return None
            all_versions = list(json_data['releases'].keys())
        except Exception as e:
            logger.error(f"Failed to get versions for {pypi_name}: {e}")
            return None

        manifest_lines = []

        # Generate DIST entries for each version
        for pypi_version in all_versions:
            # Check for interrupts between version iterations
            check_interrupt()

            gentoo_version = self._translate_version(pypi_version)
            if not gentoo_version:
                continue

            try:
                version_info = self.pypi_extractor.get_complete_package_info(pypi_name, pypi_version)
                if version_info and 'manifest_entry' in version_info:
                    manifest_lines.append(version_info['manifest_entry'])
            except InterruptedError:
                raise  # Re-raise interrupts
            except Exception as e:
                logger.warning(f"Failed to get manifest entry for {pypi_name} {pypi_version}: {e}")
                
        return '\n'.join(manifest_lines) + ('\n' if manifest_lines else '')
    
    def destroy(self, path):
        """Called when filesystem is being unmounted - print performance stats."""
        logger.info("Filesystem unmounting - performance summary:")
        if hasattr(self.pypi_extractor, 'print_performance_stats'):
            self.pypi_extractor.print_performance_stats()

        # Save patches if there are unsaved changes
        if self.patch_store is not None and self.patch_store.is_dirty:
            logger.info("Saving dependency patches...")
            if self.patch_store.save():
                logger.info(f"Patches saved to {self.patch_store.storage_path}")
            else:
                logger.error("Failed to save dependency patches!")

        if self.compat_patch_store is not None and self.compat_patch_store.is_dirty:
            logger.info("Saving PYTHON_COMPAT patches...")
            if self.compat_patch_store.save():
                logger.info(f"PYTHON_COMPAT patches saved to {self.compat_patch_store.storage_path}")
            else:
                logger.error("Failed to save PYTHON_COMPAT patches!")

        # Close the extractor properly
        if hasattr(self.pypi_extractor, 'close'):
            self.pypi_extractor.close()

    def _get_package_deps_for_sys(self, category: str, gentoo_name: str,
                                   pypi_name: str, version: str) -> List[str]:
        """
        Get dependencies for display in .sys/dependencies filesystem.

        Returns original dependencies with patches applied.
        """
        if version == '_all':
            # For _all, return empty list (patches apply to all versions)
            return []

        try:
            # Find the PyPI version
            json_data = self._get_cached_package_json(pypi_name)
            if not json_data or 'releases' not in json_data:
                return []

            pypi_version = None
            for pypi_ver in json_data['releases']:
                if self._translate_version(pypi_ver) == version:
                    pypi_version = pypi_ver
                    break

            if not pypi_version:
                return []

            # Get package metadata
            version_metadata = self.pypi_extractor.get_complete_package_info(pypi_name, pypi_version)
            if not version_metadata:
                return []

            # Prepare ebuild data to get formatted dependencies
            ebuild_data = self.ebuild_extractor.prepare_ebuild_data(version_metadata)
            if not ebuild_data:
                return []

            # Get RDEPEND
            deps = ebuild_data.get('RDEPEND', [])

            # Apply patches
            if self.patch_store is not None:
                deps = self.patch_store.apply_patches(category, gentoo_name, version, deps)

            return deps

        except Exception as e:
            logger.debug(f"Error getting deps for {gentoo_name}-{version}: {e}")
            return []

    def _get_package_python_compat_for_sys(self, category: str, gentoo_name: str,
                                            pypi_name: str, version: str) -> List[str]:
        """
        Get PYTHON_COMPAT for display in .sys/python-compat filesystem.

        Returns original PYTHON_COMPAT with patches applied.
        """
        if version == '_all':
            # For _all, return empty list (patches apply to all versions)
            return []

        try:
            # Find the PyPI version
            json_data = self._get_cached_package_json(pypi_name)
            if not json_data or 'releases' not in json_data:
                return []

            pypi_version = None
            for pypi_ver in json_data['releases']:
                if self._translate_version(pypi_ver) == version:
                    pypi_version = pypi_ver
                    break

            if not pypi_version:
                return []

            # Get package metadata
            version_metadata = self.pypi_extractor.get_complete_package_info(pypi_name, pypi_version)
            if not version_metadata:
                return []

            # Prepare ebuild data to get formatted PYTHON_COMPAT
            ebuild_data = self.ebuild_extractor.prepare_ebuild_data(version_metadata)
            if not ebuild_data:
                return []

            # Get PYTHON_COMPAT
            python_compat = ebuild_data.get('PYTHON_COMPAT', [])

            # Apply patches
            if self.compat_patch_store is not None:
                python_compat = self.compat_patch_store.apply_patches(
                    category, gentoo_name, version, python_compat
                )

            return python_compat

        except Exception as e:
            logger.debug(f"Error getting PYTHON_COMPAT for {gentoo_name}-{version}: {e}")
            return []

    # FUSE write operations for .sys/ filesystem

    def create(self, path, mode, fi=None):
        """Create a new file (used for adding dependencies/impls via touch)."""
        parsed = self._parse_path(path)

        if parsed['type'] == 'sys_deps_dep':
            # touch /.sys/dependencies/dev-python/pkg/ver/>=dep-1.0[...]
            if self.patch_store is None:
                raise FuseOSError(errno.EROFS)

            category = parsed['category']
            package = parsed['package']
            version = parsed['version']
            new_dep = parsed['dep']

            self.patch_store.add_dependency(category, package, version, new_dep)
            logger.info(f"Added dependency via touch: {new_dep} to {category}/{package}/{version}")
            return 0

        if parsed['type'] == 'sys_compat_impl':
            # touch /.sys/python-compat/dev-python/pkg/ver/python3_13
            if self.compat_patch_store is None:
                raise FuseOSError(errno.EROFS)

            category = parsed['category']
            package = parsed['package']
            version = parsed['version']
            impl = parsed['impl']

            self.compat_patch_store.add_impl(category, package, version, impl)
            logger.info(f"Added impl via touch: {impl} to {category}/{package}/{version}")
            return 0

        raise FuseOSError(errno.EROFS)

    def unlink(self, path):
        """Remove a file (used for removing dependencies/impls via rm)."""
        parsed = self._parse_path(path)

        if parsed['type'] == 'sys_deps_dep':
            # rm /.sys/dependencies/dev-python/pkg/ver/=dep-1.0[...]
            if self.patch_store is None:
                raise FuseOSError(errno.EROFS)

            category = parsed['category']
            package = parsed['package']
            version = parsed['version']
            old_dep = parsed['dep']

            self.patch_store.remove_dependency(category, package, version, old_dep)
            logger.info(f"Removed dependency via rm: {old_dep} from {category}/{package}/{version}")
            return

        if parsed['type'] == 'sys_compat_impl':
            # rm /.sys/python-compat/dev-python/pkg/ver/python3_14
            if self.compat_patch_store is None:
                raise FuseOSError(errno.EROFS)

            category = parsed['category']
            package = parsed['package']
            version = parsed['version']
            impl = parsed['impl']

            self.compat_patch_store.remove_impl(category, package, version, impl)
            logger.info(f"Removed impl via rm: {impl} from {category}/{package}/{version}")
            return

        raise FuseOSError(errno.EROFS)

    def rename(self, old_path, new_path):
        """Rename a file (used for modifying dependencies via mv)."""
        old_parsed = self._parse_path(old_path)
        new_parsed = self._parse_path(new_path)

        if (old_parsed['type'] == 'sys_deps_dep' and new_parsed['type'] == 'sys_deps_dep'):
            # mv /.sys/deps/.../old_dep /.sys/deps/.../new_dep
            if self.patch_store is None:
                raise FuseOSError(errno.EROFS)

            # Verify same package/version
            if (old_parsed['category'] != new_parsed['category'] or
                old_parsed['package'] != new_parsed['package'] or
                old_parsed['version'] != new_parsed['version']):
                raise FuseOSError(errno.EXDEV)  # Cross-device link not permitted

            category = old_parsed['category']
            package = old_parsed['package']
            version = old_parsed['version']
            old_dep = old_parsed['dep']
            new_dep = new_parsed['dep']

            self.patch_store.modify_dependency(category, package, version, old_dep, new_dep)
            logger.info(f"Modified dependency via mv: {old_dep} -> {new_dep}")
            return

        raise FuseOSError(errno.EROFS)

    def write(self, path, data, offset, fh):
        """Write to a file (used for importing patch files)."""
        parsed = self._parse_path(path)

        if parsed['type'] == 'sys_patch_file':
            # echo "..." > /.sys/dependencies-patch/dev-python/pkg/ver.patch
            if self.patch_store is None:
                raise FuseOSError(errno.EROFS)

            category = parsed['category']
            package = parsed['package']
            version = parsed['version']

            # Decode and parse patch content
            content = data.decode('utf-8', errors='replace')

            # Clear existing patches and import new ones
            self.patch_store.clear_patches(category, package, version)
            count = self.patch_store.parse_patch_file(content, category, package, version)
            logger.info(f"Imported {count} dependency patches via write to {path}")

            return len(data)

        if parsed['type'] == 'sys_compat_patch_file':
            # echo "..." > /.sys/python-compat-patch/dev-python/pkg/ver.patch
            if self.compat_patch_store is None:
                raise FuseOSError(errno.EROFS)

            category = parsed['category']
            package = parsed['package']
            version = parsed['version']

            # Decode and parse patch content
            content = data.decode('utf-8', errors='replace')

            # Clear existing patches and import new ones
            self.compat_patch_store.clear_patches(category, package, version)
            count = self.compat_patch_store.parse_patch_file(content, category, package, version)
            logger.info(f"Imported {count} PYTHON_COMPAT patches via write to {path}")

            return len(data)

        raise FuseOSError(errno.EROFS)

    def truncate(self, path, length, fh=None):
        """Truncate a file (needed for write support)."""
        parsed = self._parse_path(path)

        if parsed['type'] == 'sys_patch_file':
            # Support truncate for dependency patch files
            if self.patch_store is None:
                raise FuseOSError(errno.EROFS)

            if length == 0:
                # truncate to 0 = clear all patches
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                self.patch_store.clear_patches(category, package, version)
            return

        if parsed['type'] == 'sys_compat_patch_file':
            # Support truncate for PYTHON_COMPAT patch files
            if self.compat_patch_store is None:
                raise FuseOSError(errno.EROFS)

            if length == 0:
                # truncate to 0 = clear all patches
                category = parsed['category']
                package = parsed['package']
                version = parsed['version']
                self.compat_patch_store.clear_patches(category, package, version)
            return

        raise FuseOSError(errno.EROFS)


def mount_filesystem(mountpoint: str, foreground: bool = False, debug: bool = False,
                    cache_ttl: int = 3600, cache_dir: Optional[str] = None,
                    filter_config: Optional[Dict] = None,
                    patch_file: Optional[str] = None, no_patches: bool = False):
    """
    Mount the portage-pip FUSE filesystem.

    Args:
        mountpoint: Path where the filesystem should be mounted
        foreground: Run in foreground instead of daemonizing
        debug: Enable debug output
        cache_ttl: Cache time-to-live in seconds (default: 1 hour)
        cache_dir: Cache directory for PyPI metadata (default: system temp)
        filter_config: Package filter configuration dictionary
        patch_file: Path to dependency patch file
        no_patches: If True, disable the dependency patching system
    """
    # Only configure logging if it hasn't been configured yet (no handlers exist)
    if not logging.getLogger().handlers:
        if debug:
            logging.basicConfig(level=logging.DEBUG)
        else:
            logging.basicConfig(level=logging.INFO)

    logger.info(f"Mounting portage-pip FUSE filesystem at {mountpoint}")
    fs = PortagePipFS(
        cache_ttl=cache_ttl,
        cache_dir=cache_dir,
        filter_config=filter_config,
        patch_file=patch_file,
        no_patches=no_patches
    )
    # Note: nothreads=False allows better signal handling for Ctrl+C
    FUSE(fs, mountpoint, nothreads=False, foreground=foreground, debug=debug, allow_other=True)
