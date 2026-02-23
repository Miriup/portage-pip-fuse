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

from .prefetcher import create_prefetched_translator
from .pip_metadata import PyPIMetadataExtractor, EbuildDataExtractor
from .prefetcher import PyPIPrefetcher

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
    
    def __init__(self, root: str = "/", cache_ttl: int = 3600, cache_dir: Optional[str] = None):
        """
        Initialize the FUSE filesystem.
        
        Args:
            root: Root directory for the filesystem operations
            cache_ttl: Cache time-to-live in seconds (default: 1 hour)
        """
        self.root = root
        self.cache_ttl = cache_ttl
        
        # Content cache: path -> (content, timestamp)
        self._content_cache: Dict[str, Tuple[bytes, float]] = {}
        
        # Package metadata cache: pypi_name -> (metadata, timestamp)
        self._metadata_cache: Dict[str, Tuple[dict, float]] = {}
        
        # Name translation components
        self.name_translator = create_prefetched_translator()
        self.pypi_extractor = PyPIMetadataExtractor(cache_ttl=cache_ttl, cache_dir=cache_dir)
        self.ebuild_extractor = EbuildDataExtractor()
        
        # Static overlay structure
        self.static_dirs = {
            "/",
            "/dev-python",
            "/profiles", 
            "/metadata",
            "/eclass"
        }
        
        # Static files
        self.static_files = {
            "/profiles/repo_name": b"portage-pip-fuse\n",
            "/metadata/layout.conf": self._generate_layout_conf().encode('utf-8')
        }
        
        logger.info("PortagePipFS initialized with PyPI integration")
        
    def _generate_layout_conf(self) -> str:
        """Generate layout.conf for the overlay."""
        return """repo-name = portage-pip-fuse
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
        
        if parts[0] == 'profiles':
            return {'type': 'profiles', 'filename': parts[-1] if len(parts) > 1 else None}
        elif parts[0] == 'metadata':
            return {'type': 'metadata', 'filename': parts[-1] if len(parts) > 1 else None}
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
            return self.name_translator.gentoo_to_pypi(gentoo_name)
        except Exception as e:
            logger.warning(f"Name translation failed for {gentoo_name}: {e}")
            return None
            
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
    
    def _translate_version(self, pypi_version: str) -> Optional[str]:
        """Translate PyPI version to Gentoo format."""
        if PypiVersion is None:
            # Fallback simple translation
            version = pypi_version.replace('a', '_alpha').replace('b', '_beta').replace('rc', '_rc')
            if '.dev' in version:
                version = version.replace('.dev', '.9999.')
            if '.post' in version:
                version = version.replace('.post', '_p')
            return version
            
        try:
            parsed = PypiVersion.parse_version(pypi_version)
            return str(parsed) if parsed else None
        except Exception:
            return None
            
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
            json_data = self.pypi_extractor.get_package_json(pypi_name)
            if not json_data or 'releases' not in json_data:
                # Cache empty result to avoid repeated failed lookups
                self._metadata_cache[cache_key] = ([], time.time())
                return []
            
            gentoo_versions = []
            for pypi_ver in json_data['releases']:
                # Skip versions with no files (yanked or empty releases)
                if not json_data['releases'][pypi_ver]:
                    continue
                    
                gentoo_ver = self._translate_version(pypi_ver)
                if gentoo_ver:
                    gentoo_versions.append(gentoo_ver)
                    
            sorted_versions = sorted(gentoo_versions, reverse=True)  # Newest first
            
            # Cache the versions list
            self._metadata_cache[cache_key] = (sorted_versions, time.time())
            
            return sorted_versions
            
        except Exception as e:
            logger.debug(f"Error getting versions for {pypi_name}: {e}")
            # Cache empty result to avoid repeated failed lookups
            self._metadata_cache[cache_key] = ([], time.time())
            return []
        
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
        
        # Default attributes
        attrs = {
            'st_uid': os.getuid(),
            'st_gid': os.getgid(),
            'st_atime': time.time(),
            'st_mtime': time.time(),
            'st_ctime': time.time(),
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
        elif parsed['type'] in ['profiles', 'metadata', 'eclass', 'category', 'package']:
            # Directory
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
                # Check if the PyPI package exists (this will use cached versions)
                versions = self._get_package_versions(pypi_name)
                if not versions:
                    logger.debug(f"No versions found for PyPI package: {pypi_name}")
                    raise FuseOSError(errno.ENOENT)
                    
                # Additional check for ebuild files
                if parsed['type'] == 'ebuild':
                    if parsed['version'] not in versions:
                        logger.debug(f"Version {parsed['version']} not found for {pypi_name}")
                        raise FuseOSError(errno.ENOENT)
                        
            except FuseOSError:
                # Re-raise FUSE errors
                raise
            except Exception as e:
                # If we can't verify, deny access to prevent broken files
                logger.debug(f"Cannot verify package {pypi_name}: {e}")
                raise FuseOSError(errno.ENOENT)
                        
            # File exists, set attributes
            attrs.update({
                'st_mode': stat.S_IFREG | 0o644,
                'st_nlink': 1,
                'st_size': 2048,  # Default estimate
            })
            
            # Try to get actual size from cache
            cached = self._get_cached_content(path)
            if cached:
                attrs['st_size'] = len(cached)
        else:
            # Unknown path type - this is normal for filesystem exploration
            logger.debug(f"Path not found: {path} (type: {parsed['type']})")
            raise FuseOSError(errno.ENOENT)
            
        return attrs
            
    def readdir(self, path, fh):
        """Read directory contents."""
        parsed = self._parse_path(path)
        entries = ['.', '..']
        
        if parsed['type'] == 'root':
            # Root directory - show main overlay structure
            entries.extend(['dev-python', 'profiles', 'metadata', 'eclass'])
            
        elif parsed['type'] == 'profiles':
            entries.append('repo_name')
            
        elif parsed['type'] == 'metadata':
            entries.append('layout.conf')
            
        elif parsed['type'] == 'eclass':
            # Empty for now - could add eclasses later
            pass
            
        elif parsed['type'] == 'category' and parsed['category'] == 'dev-python':
            # List available PyPI packages
            # For now, return packages that are requested (they'll be created dynamically)
            # In a full implementation, this could scan a pre-populated list
            pass
            
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
                except Exception as e:
                    logger.error(f"Error listing files for {gentoo_name}: {e}")
        
        return entries
        
    def read(self, path, length, offset, fh):
        """Read file contents."""
        # Check static files first
        if path in self.static_files:
            content = self.static_files[path]
            return content[offset:offset + length]
        
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
        
    def open(self, path, flags):
        """Open a file."""
        # Allow opening static files and dynamic files
        if path in self.static_files:
            return 0
            
        # Check if it's a valid dynamic file
        parsed = self._parse_path(path)
        if parsed['type'] in ['ebuild', 'package_metadata', 'manifest']:
            # Additional verification for ebuilds
            if parsed['type'] == 'ebuild':
                gentoo_name = parsed['package']
                pypi_name = self._gentoo_to_pypi(gentoo_name)
                if not pypi_name:
                    logger.debug(f"Cannot translate package name: {gentoo_name}")
                    raise FuseOSError(errno.ENOENT)
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
            json_data = self.pypi_extractor.get_package_json(pypi_name)
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
            f"DISTUTILS_USE_PEP517=standalone",
            f"PYTHON_COMPAT=( {' '.join(data.get('PYTHON_COMPAT', ['python3_8', 'python3_9', 'python3_10', 'python3_11']))} )",
            f"",
            f"inherit distutils-r1 pypi",
            f"",
            f"DESCRIPTION=\"{data.get('DESCRIPTION', 'Python package from PyPI')}\"",
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
            
        # Add optional dependencies with USE flags
        if data.get('OPTIONAL_DEPEND'):
            ebuild_lines.append(f"")
            ebuild_lines.append(f"RDEPEND+=\"")
            for use_flag, deps in data['OPTIONAL_DEPEND'].items():
                for dep in deps:
                    ebuild_lines.append(f"\t{use_flag}? ( {dep} )")
            ebuild_lines.append(f"\"")
            
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
            
        metadata = self._get_package_metadata(pypi_name)
        if not metadata:
            return None
            
        manifest_lines = []
        
        # Get all versions and generate DIST entries
        for pypi_version in metadata.get('all_versions', []):
            gentoo_version = self._translate_version(pypi_version)
            if not gentoo_version:
                continue
                
            try:
                version_info = self.pypi_extractor.get_complete_package_info(pypi_name, pypi_version)
                if version_info and 'manifest_entry' in version_info:
                    manifest_lines.append(version_info['manifest_entry'])
            except Exception as e:
                logger.warning(f"Failed to get manifest entry for {pypi_name} {pypi_version}: {e}")
                
        return '\n'.join(manifest_lines) + ('\n' if manifest_lines else '')


def mount_filesystem(mountpoint: str, foreground: bool = False, debug: bool = False, cache_ttl: int = 3600, cache_dir: Optional[str] = None):
    """
    Mount the portage-pip FUSE filesystem.
    
    Args:
        mountpoint: Path where the filesystem should be mounted
        foreground: Run in foreground instead of daemonizing
        debug: Enable debug output
        cache_ttl: Cache time-to-live in seconds (default: 1 hour)
        cache_dir: Cache directory for PyPI metadata (default: system temp)
    """
    if debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
        
    logger.info(f"Mounting portage-pip FUSE filesystem at {mountpoint}")
    fs = PortagePipFS(cache_ttl=cache_ttl, cache_dir=cache_dir)
    FUSE(fs, mountpoint, nothreads=True, foreground=foreground, debug=debug)