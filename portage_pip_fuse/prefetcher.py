"""
Prefetcher module for loading PyPI name mappings from Gentoo repositories.

This module scans Gentoo repositories to find dev-python packages that inherit
from the pypi eclass, extracts their PyPI package names, and preloads these
mappings into the name translator for accurate bidirectional translation.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional
import logging

# Try to import portage - this is optional for basic functionality
try:
    import portage
    from portage.repository.config import RepoConfigLoader
    HAS_PORTAGE = True
except ImportError:
    HAS_PORTAGE = False

from portage_pip_fuse.name_translator import CachedNameTranslator
from portage_pip_fuse.constants import REPO_NAME, REPO_LOCATION

logger = logging.getLogger(__name__)


class RepositoryScanner:
    """
    Scanner for Gentoo repositories to find PyPI package mappings.
    
    This class interfaces with portage to discover repositories and
    scan dev-python packages for PyPI name mappings.
    """
    
    def __init__(self, repos_conf: Optional[str] = None):
        """
        Initialize the repository scanner.
        
        Args:
            repos_conf: Path to repos.conf directory. If None, uses default.
            
        Examples:
            >>> scanner = RepositoryScanner()
            >>> isinstance(scanner, RepositoryScanner)
            True
        """
        self.repos_conf = repos_conf or "/etc/portage/repos.conf"
        self.repositories: Dict[str, str] = {}
        self._pypi_inherit_pattern = re.compile(r'^\s*inherit\s+.*\bpypi\b', re.MULTILINE)
        self._pypi_pn_pattern = re.compile(r'^\s*PYPI_PN\s*=\s*["\']?([^"\'\n]+)["\']?', re.MULTILINE)
        self._python_compat_pattern = re.compile(r'^\s*PYTHON_COMPAT\s*=\s*\(([^)]+)\)', re.MULTILINE)
        
    def discover_repositories(self) -> Dict[str, str]:
        """
        Discover available Gentoo repositories and their locations.
        
        Returns:
            Dictionary mapping repository name to filesystem path
            
        Examples:
            >>> scanner = RepositoryScanner()
            >>> repos = scanner.discover_repositories()
            >>> isinstance(repos, dict)
            True
            >>> 'gentoo' in repos or len(repos) == 0  # May or may not have repos
            True
        """
        repositories = {}
        
        # Try portage API first if available
        if HAS_PORTAGE:
            try:
                settings = portage.config(clone=portage.settings)
                repo_config = settings.repositories
                for repo in repo_config:
                    if repo.location:
                        # Skip our own FUSE filesystem to avoid recursion/issues
                        if repo.name == REPO_NAME:
                            logger.debug(f"Skipping FUSE repository '{repo.name}' at {repo.location}")
                            continue
                        repositories[repo.name] = repo.location
                        logger.debug(f"Found repository '{repo.name}' at {repo.location}")
            except Exception as e:
                logger.warning(f"Failed to use portage API: {e}")
        
        # Fallback to direct filesystem scanning
        if not repositories:
            # Check common repository locations
            common_paths = [
                "/var/db/repos",  # Check parent directory for multiple repos
                "/var/db/repos/gentoo",
                "/var/lib/overlays",
            ]
            
            for path in common_paths:
                if os.path.exists(path):
                    if os.path.isdir(os.path.join(path, "dev-python")):
                        # This looks like a repository
                        repo_name = os.path.basename(path)
                        # Skip our FUSE filesystem
                        if repo_name == REPO_NAME:
                            logger.debug(f"Skipping FUSE repository at {path}")
                            continue
                        repositories[repo_name] = path
                        logger.debug(f"Found repository at {path}")
                    elif os.path.isdir(path):
                        # Check subdirectories
                        for subdir in os.listdir(path):
                            # Skip our FUSE filesystem (might be mounted at REPO_LOCATION or identify as REPO_NAME)
                            if subdir == os.path.basename(REPO_LOCATION) or subdir == REPO_NAME:
                                logger.debug(f"Skipping FUSE repository '{subdir}'")
                                continue
                            subpath = os.path.join(path, subdir)
                            if os.path.isdir(os.path.join(subpath, "dev-python")):
                                repositories[subdir] = subpath
                                logger.debug(f"Found repository '{subdir}' at {subpath}")
        
        self.repositories = repositories
        return repositories
    
    def scan_dev_python_packages(self, repo_path: str) -> List[Tuple[str, str]]:
        """
        Scan dev-python category in a repository for package names and paths.
        
        Args:
            repo_path: Path to the repository root
            
        Returns:
            List of tuples (package_name, package_path)
            
        Examples:
            >>> scanner = RepositoryScanner()
            >>> # This will return empty list if no repo exists
            >>> packages = scanner.scan_dev_python_packages("/nonexistent")
            >>> isinstance(packages, list)
            True
        """
        packages = []
        dev_python_path = os.path.join(repo_path, "dev-python")
        
        if not os.path.exists(dev_python_path):
            logger.warning(f"No dev-python category found in {repo_path}")
            return packages
        
        try:
            for entry in os.listdir(dev_python_path):
                package_path = os.path.join(dev_python_path, entry)
                if os.path.isdir(package_path):
                    # Skip metadata and CVS directories
                    if entry in ["metadata", "CVS", ".git"]:
                        continue
                    packages.append((entry, package_path))
        except OSError as e:
            logger.error(f"Error scanning {dev_python_path}: {e}")
        
        return packages
    
    def check_pypi_inheritance(self, package_path: str) -> bool:
        """
        Check if any ebuild in the package directory inherits pypi eclass.
        
        Args:
            package_path: Path to the package directory
            
        Returns:
            True if package inherits pypi eclass
            
        Examples:
            >>> scanner = RepositoryScanner()
            >>> # Will return False for non-existent path
            >>> scanner.check_pypi_inheritance("/nonexistent")
            False
        """
        try:
            for entry in os.listdir(package_path):
                if entry.endswith(".ebuild"):
                    ebuild_path = os.path.join(package_path, entry)
                    with open(ebuild_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        if self._pypi_inherit_pattern.search(content):
                            return True
        except (OSError, IOError) as e:
            logger.debug(f"Error checking {package_path}: {e}")
        
        return False
    
    def extract_pypi_name(self, package_path: str, gentoo_name: str) -> Optional[str]:
        """
        Extract the PyPI package name from ebuilds in the package directory.
        
        Looks for PYPI_PN variable in ebuilds. If not found, returns None
        (which means the package uses the default mapping).
        
        Args:
            package_path: Path to the package directory
            gentoo_name: The Gentoo package name
            
        Returns:
            PyPI package name if PYPI_PN is set, None otherwise
            
        Examples:
            >>> scanner = RepositoryScanner()
            >>> # Returns None for non-existent paths
            >>> scanner.extract_pypi_name("/nonexistent", "test-package")
        """
        pypi_pn = None
        
        try:
            # Check all ebuilds, preferring the newest version
            ebuilds = [f for f in os.listdir(package_path) if f.endswith(".ebuild")]
            ebuilds.sort(reverse=True)  # Newest version first
            
            for ebuild_file in ebuilds:
                ebuild_path = os.path.join(package_path, ebuild_file)
                with open(ebuild_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    
                    # Check if it inherits pypi
                    if not self._pypi_inherit_pattern.search(content):
                        continue
                    
                    # Look for PYPI_PN
                    match = self._pypi_pn_pattern.search(content)
                    if match:
                        pypi_pn = match.group(1).strip()
                        # Handle variable substitutions like ${PN/-/.}
                        pypi_pn = self._expand_variables(pypi_pn, gentoo_name)
                        break
                    
                    # If no PYPI_PN is set, the package uses default mapping
                    # which is the Gentoo name itself
                    return None
                    
        except (OSError, IOError) as e:
            logger.debug(f"Error extracting PyPI name from {package_path}: {e}")
        
        return pypi_pn
    
    def _expand_variables(self, value: str, package_name: str) -> str:
        """
        Expand common bash variables in PYPI_PN values.
        
        Args:
            value: The value that may contain variables
            package_name: The package name (PN value)
            
        Returns:
            Expanded value
            
        Examples:
            >>> scanner = RepositoryScanner()
            >>> scanner._expand_variables("${PN}", "django")
            'django'
            >>> scanner._expand_variables("${PN/-/.}", "google-cloud")
            'google.cloud'
            >>> scanner._expand_variables("${PN/-/_}", "my-package")
            'my_package'
            >>> scanner._expand_variables("${PN^^}", "django")
            'DJANGO'
            >>> scanner._expand_variables("prefix-${PN}", "test")
            'prefix-test'
        """
        result = value
        
        # Handle ${PN} and its transformations
        if "${PN" in result:
            # Simple substitution
            result = result.replace("${PN}", package_name)
            
            # Handle ${PN/-/.} (replace - with .)
            result = re.sub(r'\$\{PN/\-/\.\}', package_name.replace('-', '.'), result)
            
            # Handle ${PN/./-} (replace . with -)
            result = re.sub(r'\$\{PN/\./\-\}', package_name.replace('.', '-'), result)
            
            # Handle ${PN/-/_} (replace - with _)
            result = re.sub(r'\$\{PN/\-/_\}', package_name.replace('-', '_'), result)
            
            # Handle ${PN/_/-} (replace _ with -)
            result = re.sub(r'\$\{PN/_/\-\}', package_name.replace('_', '-'), result)
            
            # Handle ${PN^^} (uppercase)
            result = re.sub(r'\$\{PN\^\^\}', package_name.upper(), result)
            
            # Handle ${PN^} (capitalize first letter)
            result = re.sub(r'\$\{PN\^\}', package_name.capitalize(), result)
        
        return result


class PyPIPrefetcher:
    """
    Prefetcher that loads PyPI name mappings from Gentoo repositories.
    
    This class coordinates scanning repositories and loading mappings
    into a name translator for accurate name translation.
    
    Examples:
        >>> prefetcher = PyPIPrefetcher()
        >>> isinstance(prefetcher.translator, CachedNameTranslator)
        True
    """
    
    def __init__(self, translator: Optional[CachedNameTranslator] = None):
        """
        Initialize the prefetcher.
        
        Args:
            translator: Name translator to load mappings into.
                       If None, creates a new CachedNameTranslator.
        """
        self.translator = translator or CachedNameTranslator()
        self.scanner = RepositoryScanner()
        self.masters: Set[str] = set()
        self.mappings: Dict[str, str] = {}
        
    def load_from_repositories(self, 
                              repo_names: Optional[List[str]] = None,
                              include_non_pypi: bool = False) -> Dict[str, str]:
        """
        Load PyPI name mappings from specified repositories.
        
        Args:
            repo_names: List of repository names to scan.
                       If None, scans all discovered repositories.
            include_non_pypi: If True, includes packages that don't inherit pypi
            
        Returns:
            Dictionary of PyPI name to Gentoo name mappings
            
        Examples:
            >>> prefetcher = PyPIPrefetcher()
            >>> mappings = prefetcher.load_from_repositories()
            >>> isinstance(mappings, dict)
            True
        """
        # Discover repositories
        repositories = self.scanner.discover_repositories()
        
        if not repositories:
            logger.warning("No repositories found")
            return {}
        
        # Filter repositories if specific ones requested
        if repo_names:
            repositories = {
                name: path for name, path in repositories.items() 
                if name in repo_names
            }
        
        # Scan each repository
        for repo_name, repo_path in repositories.items():
            logger.info(f"Scanning repository '{repo_name}' at {repo_path}")
            self._scan_repository(repo_name, repo_path, include_non_pypi)
        
        # Load mappings into translator
        if self.mappings:
            self.translator.preload_mappings(self.mappings)
            logger.info(f"Loaded {len(self.mappings)} PyPI name mappings")
        
        return self.mappings
    
    def _scan_repository(self, repo_name: str, repo_path: str, 
                        include_non_pypi: bool = False) -> None:
        """
        Scan a single repository for PyPI packages.
        
        Args:
            repo_name: Name of the repository
            repo_path: Path to the repository
            include_non_pypi: If True, includes non-pypi packages
        """
        packages = self.scanner.scan_dev_python_packages(repo_path)
        pypi_packages = 0
        
        for gentoo_name, package_path in packages:
            # Check if package inherits pypi
            if self.scanner.check_pypi_inheritance(package_path):
                pypi_packages += 1
                
                # Extract PyPI name if custom
                pypi_name = self.scanner.extract_pypi_name(package_path, gentoo_name)
                
                if pypi_name:
                    # Custom PyPI name mapping
                    self.mappings[pypi_name] = gentoo_name
                    logger.debug(f"Found mapping: {pypi_name} -> {gentoo_name}")
                else:
                    # Default mapping (PyPI name equals Gentoo name after denormalization)
                    # We need to guess the original PyPI name
                    # Common patterns: django -> Django, google-cloud -> google.cloud
                    possible_names = self._guess_pypi_names(gentoo_name)
                    for name in possible_names:
                        self.mappings[name] = gentoo_name
                        
            elif include_non_pypi:
                # Include as-is mapping for non-pypi packages
                self.mappings[gentoo_name] = gentoo_name
        
        if pypi_packages > 0:
            self.masters.add(repo_name)
            logger.info(f"Repository '{repo_name}' has {pypi_packages} PyPI packages")
    
    def _guess_pypi_names(self, gentoo_name: str) -> List[str]:
        """
        Guess possible PyPI names from a Gentoo name.
        
        Since Gentoo names are normalized, we try to guess common
        original PyPI name patterns.
        
        Args:
            gentoo_name: The normalized Gentoo package name
            
        Returns:
            List of possible PyPI names
            
        Examples:
            >>> prefetcher = PyPIPrefetcher()
            >>> names = prefetcher._guess_pypi_names("django")
            >>> "Django" in names
            True
            >>> names = prefetcher._guess_pypi_names("google-cloud")
            >>> "google.cloud" in names
            True
            >>> "google_cloud" in names
            True
        """
        guesses = [gentoo_name]  # Always include the name as-is
        
        # Try capitalized version (common for many packages)
        guesses.append(gentoo_name.capitalize())
        
        # Try with dots instead of hyphens (namespace packages)
        if '-' in gentoo_name:
            guesses.append(gentoo_name.replace('-', '.'))
            guesses.append(gentoo_name.replace('-', '_'))
        
        # Try title case for each component
        parts = gentoo_name.split('-')
        if len(parts) > 1:
            # TitleCase (e.g., MyPackage)
            guesses.append(''.join(p.capitalize() for p in parts))
            # With dots (e.g., my.package)
            guesses.append('.'.join(parts))
            # With underscores (e.g., my_package)
            guesses.append('_'.join(parts))
        
        # Special cases for common patterns
        if gentoo_name.startswith("py"):
            # Try without py prefix but capitalized
            base = gentoo_name[2:]
            if base:
                guesses.append(base.capitalize())
                guesses.append("Py" + base.capitalize())
        
        return guesses
    
    def get_masters(self) -> Set[str]:
        """
        Get the set of repository names that contain PyPI packages.
        
        Returns:
            Set of repository names designated as masters
            
        Examples:
            >>> prefetcher = PyPIPrefetcher()
            >>> masters = prefetcher.get_masters()
            >>> isinstance(masters, set)
            True
        """
        return self.masters
    
    def get_translator(self) -> CachedNameTranslator:
        """
        Get the name translator with loaded mappings.
        
        Returns:
            The cached name translator instance
            
        Examples:
            >>> prefetcher = PyPIPrefetcher()
            >>> translator = prefetcher.get_translator()
            >>> isinstance(translator, CachedNameTranslator)
            True
        """
        return self.translator


def create_prefetched_translator(repo_names: Optional[List[str]] = None) -> CachedNameTranslator:
    """
    Convenience function to create a translator with prefetched mappings.
    
    Args:
        repo_names: Optional list of repository names to scan
        
    Returns:
        A CachedNameTranslator with loaded PyPI mappings
        
    Examples:
        >>> translator = create_prefetched_translator()
        >>> isinstance(translator, CachedNameTranslator)
        True
    """
    prefetcher = PyPIPrefetcher()
    prefetcher.load_from_repositories(repo_names)
    return prefetcher.get_translator()


if __name__ == "__main__":
    # Example usage and testing
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    print("Discovering Gentoo repositories...")
    scanner = RepositoryScanner()
    repos = scanner.discover_repositories()
    
    if not repos:
        print("No repositories found. Make sure you're on a Gentoo system.")
        sys.exit(1)
    
    print(f"\nFound {len(repos)} repositories:")
    for name, path in repos.items():
        print(f"  - {name}: {path}")
    
    print("\nLoading PyPI mappings...")
    prefetcher = PyPIPrefetcher()
    mappings = prefetcher.load_from_repositories()
    
    print(f"\nLoaded {len(mappings)} PyPI name mappings")
    print(f"Master repositories: {', '.join(prefetcher.get_masters())}")
    
    # Show some example mappings
    if mappings:
        print("\nExample mappings (first 10):")
        for pypi_name, gentoo_name in list(mappings.items())[:10]:
            print(f"  {pypi_name} -> {gentoo_name}")
