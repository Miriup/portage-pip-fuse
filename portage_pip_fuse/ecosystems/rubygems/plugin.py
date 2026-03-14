"""
RubyGems ecosystem plugin implementation.

This module provides the EcosystemPlugin implementation for RubyGems,
enabling installation of Ruby gems through Portage using ruby-fakegem.eclass.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Set, TYPE_CHECKING

from portage_pip_fuse.plugin import (
    EcosystemPlugin,
    EbuildGeneratorBase,
    MetadataProviderBase,
    PluginRegistry,
)

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace
    from portage_pip_fuse.name_translator import NameTranslatorBase
    from portage_pip_fuse.source_provider import SourceProviderBase

logger = logging.getLogger(__name__)


class RubyGemsMetadataProvider(MetadataProviderBase):
    """
    Metadata provider for RubyGems packages.

    This fetches gem metadata from RubyGems.org API:
    - /api/v1/gems/{name}.json - Package info
    - /api/v1/versions/{name}.json - Version list
    """

    RUBYGEMS_API_BASE = "https://rubygems.org/api/v1"

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        cache_ttl: int = 3600
    ):
        """
        Initialize the RubyGems metadata provider.

        Args:
            cache_dir: Cache directory path
            cache_ttl: Cache time-to-live in seconds
        """
        import json
        import time
        from pathlib import Path
        from portage_pip_fuse.constants import find_cache_dir

        self.cache_ttl = cache_ttl
        self.cache_dir = Path(find_cache_dir(cache_dir)) / 'rubygems'
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # In-memory cache: cache_key -> (data, timestamp)
        self._memory_cache: Dict[str, tuple] = {}
        self._session = None

        logger.info(f"RubyGems metadata cache initialized at {self.cache_dir}")

    @property
    def session(self):
        """Lazy-initialize requests session."""
        if self._session is None:
            try:
                import requests
                self._session = requests.Session()
                self._session.headers.update({
                    'User-Agent': 'portage-gem-fuse/0.1.0',
                    'Accept': 'application/json',
                })
            except ImportError:
                raise RuntimeError("requests library required for RubyGems API")
        return self._session

    def _get_cache_key(self, name: str, version: Optional[str] = None) -> str:
        """Generate cache key."""
        if version:
            return f"{name.lower()}_{version}"
        return name.lower()

    def _get_cache_path(self, cache_key: str):
        """Get filesystem path for cache key."""
        from pathlib import Path
        subdir = cache_key[:2] if len(cache_key) >= 2 else '00'
        cache_subdir = self.cache_dir / subdir
        cache_subdir.mkdir(exist_ok=True)
        return cache_subdir / f"{cache_key}.json"

    def _get_cached(self, cache_key: str) -> Optional[Dict]:
        """Get data from cache."""
        import json
        import time

        # Check memory cache first
        if cache_key in self._memory_cache:
            data, timestamp = self._memory_cache[cache_key]
            if time.time() - timestamp < self.cache_ttl:
                return data
            del self._memory_cache[cache_key]

        # Check disk cache
        cache_path = self._get_cache_path(cache_key)
        if cache_path.exists():
            try:
                if time.time() - cache_path.stat().st_mtime < self.cache_ttl:
                    with cache_path.open('r') as f:
                        data = json.load(f)
                    self._memory_cache[cache_key] = (data, time.time())
                    return data
                cache_path.unlink(missing_ok=True)
            except (json.JSONDecodeError, OSError):
                cache_path.unlink(missing_ok=True)

        return None

    def _set_cached(self, cache_key: str, data: Dict):
        """Store data in cache."""
        import json
        import time

        self._memory_cache[cache_key] = (data, time.time())

        cache_path = self._get_cache_path(cache_key)
        try:
            temp_path = cache_path.with_suffix('.tmp')
            with temp_path.open('w') as f:
                json.dump(data, f)
            temp_path.rename(cache_path)
        except OSError as e:
            logger.warning(f"Failed to cache {cache_key}: {e}")

    def _fetch_api(self, endpoint: str) -> Optional[Dict]:
        """Fetch from RubyGems API."""
        from portage_pip_fuse.constants import HTTP_TIMEOUT

        url = f"{self.RUBYGEMS_API_BASE}{endpoint}"
        try:
            response = self.session.get(url, timeout=HTTP_TIMEOUT)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                return None
            else:
                logger.warning(f"RubyGems API error {response.status_code}: {url}")
                return None
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            return None

    def get_package_info(self, name: str) -> Optional[Dict[str, Any]]:
        """Get complete package information from RubyGems."""
        cache_key = self._get_cache_key(name)
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        # Fetch from API
        data = self._fetch_api(f"/gems/{name}.json")
        if data:
            # Enrich with versions list
            versions = self._fetch_api(f"/versions/{name}.json")
            if versions:
                data['versions'] = versions
            self._set_cached(cache_key, data)

        return data

    def get_package_versions(self, name: str) -> List[str]:
        """Get list of available versions for a package."""
        versions_data = self.get_versions_metadata(name)
        if not versions_data:
            return []

        # Extract version numbers
        versions = [v.get('number') for v in versions_data if isinstance(v, dict) and v.get('number')]
        return versions

    def get_versions_metadata(self, name: str) -> List[Dict[str, Any]]:
        """
        Get full versions metadata including SHA256 checksums.

        Returns:
            List of version dicts with 'number', 'sha', 'platform', etc.
        """
        cache_key = f"{name.lower()}_versions_full"
        cached = self._get_cached(cache_key)
        if cached:
            return cached.get('versions_full', [])

        versions_data = self._fetch_api(f"/versions/{name}.json")
        if not versions_data:
            return []

        # Sort by version number (newest first)
        versions_data.sort(key=lambda v: v.get('number', ''), reverse=True)

        self._set_cached(cache_key, {'versions_full': versions_data})
        return versions_data

    def get_version_info(self, name: str, version: str) -> Optional[Dict[str, Any]]:
        """Get detailed information for a specific version."""
        cache_key = self._get_cache_key(name, version)
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        # RubyGems API: /api/v2/rubygems/{name}/versions/{version}.json
        data = self._fetch_api(f"/../v2/rubygems/{name}/versions/{version}.json")
        if data:
            self._set_cached(cache_key, data)
            return data

        # Fall back to finding version in versions list
        versions_data = self._fetch_api(f"/versions/{name}.json")
        if versions_data:
            for v in versions_data:
                if v.get('number') == version:
                    self._set_cached(cache_key, v)
                    return v

        return None

    def list_packages(self) -> Set[str]:
        """
        List all available packages.

        Note: RubyGems has ~180k gems, so we don't attempt to list all.
        This returns packages we have cached.
        """
        packages = set()
        for subdir in self.cache_dir.iterdir():
            if subdir.is_dir():
                for cache_file in subdir.glob("*.json"):
                    name = cache_file.stem
                    if '_' not in name:  # Exclude version-specific caches
                        packages.add(name)
        return packages

    def list_all_packages(self) -> Set[str]:
        """
        List all available gem names from RubyGems.org.

        Fetches the full gem names list from https://index.rubygems.org/names
        which is a plain text file with one gem name per line.

        The result is cached to disk with a 24-hour TTL to avoid
        repeatedly downloading the ~3MB names file.

        Returns:
            Set of all gem names available on RubyGems.org (~190k gems)
        """
        import time

        # Cache file path for the names list
        names_cache_path = self.cache_dir / '_all_names.txt'
        names_cache_ttl = 86400  # 24 hours

        # Check disk cache first
        if names_cache_path.exists():
            try:
                cache_age = time.time() - names_cache_path.stat().st_mtime
                if cache_age < names_cache_ttl:
                    logger.debug(f"Using cached gem names list (age: {cache_age:.0f}s)")
                    with names_cache_path.open('r') as f:
                        return set(line.strip() for line in f if line.strip() and line.strip() != '---')
            except OSError as e:
                logger.warning(f"Failed to read gem names cache: {e}")

        # Fetch from RubyGems index
        logger.info("Fetching all gem names from RubyGems.org...")
        names_url = "https://index.rubygems.org/names"

        try:
            from portage_pip_fuse.constants import HTTP_TIMEOUT
            import urllib.request

            req = urllib.request.Request(names_url)
            req.add_header('User-Agent', 'portage-gem-fuse/0.1.0')

            # HTTP_TIMEOUT is a tuple (connect, read) for requests library
            # urllib uses a single timeout value, so use the read timeout
            timeout = HTTP_TIMEOUT[1] if isinstance(HTTP_TIMEOUT, tuple) else HTTP_TIMEOUT

            with urllib.request.urlopen(req, timeout=timeout) as response:
                content = response.read().decode('utf-8')

            # Parse the names list (skip the '---' header line)
            names = set()
            for line in content.splitlines():
                line = line.strip()
                if line and line != '---':
                    names.add(line)

            logger.info(f"Fetched {len(names)} gem names from RubyGems.org")

            # Cache to disk
            try:
                temp_path = names_cache_path.with_suffix('.tmp')
                with temp_path.open('w') as f:
                    f.write('\n'.join(sorted(names)))
                temp_path.rename(names_cache_path)
                logger.debug(f"Cached gem names list to {names_cache_path}")
            except OSError as e:
                logger.warning(f"Failed to cache gem names list: {e}")

            return names

        except Exception as e:
            logger.error(f"Failed to fetch gem names list: {e}")
            # Fall back to cached packages if available
            return self.list_packages()


class RubyGemsEbuildGenerator(EbuildGeneratorBase):
    """
    Ebuild generator for Ruby gems using ruby-fakegem.eclass.

    Generates ebuilds compatible with Gentoo's Ruby ecosystem, following
    the ruby-fakegem conventions.
    """

    # Ruby versions currently in Gentoo
    RUBY_VERSIONS = ['ruby32', 'ruby33']

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        name_translator: Optional['NameTranslatorBase'] = None,
        **kwargs
    ):
        """
        Initialize the ebuild generator.

        Args:
            cache_dir: Cache directory path
            name_translator: Name translator for dependencies
            **kwargs: Additional configuration
        """
        self.cache_dir = cache_dir
        self.name_translator = name_translator
        self.kwargs = kwargs

    def generate_ebuild(
        self,
        package_info: Dict[str, Any],
        version: str,
        gentoo_name: str
    ) -> str:
        """Generate ebuild content for a gem version."""
        # Extract info
        name = package_info.get('name', gentoo_name)
        description = package_info.get('info', '')[:200]
        homepage = package_info.get('homepage_uri') or package_info.get('project_uri', '')
        licenses = self._translate_license(package_info.get('licenses', []))

        # Get Ruby compatibility
        use_ruby = self._generate_use_ruby(package_info)

        # Check for native extensions
        extensions = package_info.get('extensions', [])
        has_extensions = bool(extensions)

        # Get dependencies
        rdepend = self._generate_dependencies(package_info, version, 'runtime')
        dev_deps = self._generate_dependencies(package_info, version, 'development')

        # Determine which USE flags are needed
        iuse_flags = []
        if dev_deps:
            iuse_flags.append('debug')

        # Build ebuild
        lines = [
            "# Copyright 2026 Gentoo Authors",
            "# Distributed under the terms of the GNU General Public License v2",
            "",
            "EAPI=8",
            "",
            f'USE_RUBY="{use_ruby}"',
        ]

        # Add test recipe
        lines.append('RUBY_FAKEGEM_RECIPE_TEST="none"')
        lines.append('RUBY_FAKEGEM_RECIPE_DOC="none"')

        # Add extensions if present
        if has_extensions:
            ext_list = ' '.join(extensions)
            lines.append(f'RUBY_FAKEGEM_EXTENSIONS=( {ext_list} )')

        lines.extend([
            "",
            "inherit ruby-fakegem",
            "",
            f'DESCRIPTION="{self._escape_string(description)}"',
            f'HOMEPAGE="{homepage}"',
            f'SRC_URI="https://rubygems.org/gems/${{PN}}-${{PV}}.gem"',
            "",
            f'LICENSE="{licenses}"',
            'SLOT="0"',
            'KEYWORDS="~amd64 ~arm64"',
        ])

        # Add IUSE if we have optional dependencies
        if iuse_flags:
            lines.append(f'IUSE="{" ".join(iuse_flags)}"')

        # Add runtime dependencies
        if rdepend:
            lines.extend([
                "",
                f'RDEPEND="{rdepend}"',
            ])

        # Add development dependencies guarded by debug?
        # These come from spec.add_development_dependency in the gemspec
        if dev_deps:
            bdepend_content = "debug? (\n\t\t" + dev_deps.replace('\n\t', '\n\t\t') + "\n\t)"
            lines.extend([
                "",
                f'BDEPEND="\n\t{bdepend_content}\n"',
            ])

        lines.append("")
        return '\n'.join(lines)

    def get_inherit_eclasses(self, package_info: Dict[str, Any]) -> List[str]:
        """Get list of eclasses to inherit."""
        eclasses = ['ruby-fakegem']

        # Add git-r3 if using git source
        if package_info.get('_use_git_source'):
            eclasses.append('git-r3')

        return eclasses

    def get_compat_variable(self) -> str:
        """Get the compatibility variable name."""
        return "USE_RUBY"

    def generate_compat_declaration(self, package_info: Dict[str, Any]) -> str:
        """Generate USE_RUBY declaration."""
        use_ruby = self._generate_use_ruby(package_info)
        return f'USE_RUBY="{use_ruby}"'

    def _generate_use_ruby(self, package_info: Dict[str, Any]) -> str:
        """
        Generate USE_RUBY value based on required_ruby_version.

        The required_ruby_version from RubyGems is a version specifier like:
        - ">= 2.7.0"
        - ">= 2.5", "< 4"
        """
        required = package_info.get('required_ruby_version', '')

        # Default to all supported versions if no requirement
        if not required or required == '>= 0':
            return ' '.join(self.RUBY_VERSIONS)

        # Parse version requirements
        compatible = []
        for ruby_ver in self.RUBY_VERSIONS:
            # Extract version number (e.g., "ruby32" -> "3.2")
            if ruby_ver.startswith('ruby'):
                ver_num = ruby_ver[4:]
                major = ver_num[0]
                minor = ver_num[1] if len(ver_num) > 1 else '0'
                ruby_version = f"{major}.{minor}"

                # Simple check - if ">= 2.7" and we're at 3.x, include it
                if self._version_satisfies(ruby_version, required):
                    compatible.append(ruby_ver)

        return ' '.join(compatible) if compatible else ' '.join(self.RUBY_VERSIONS)

    def _version_satisfies(self, ruby_version: str, requirement: str) -> bool:
        """Check if a Ruby version satisfies a requirement."""
        try:
            # Simple version comparison
            from packaging.specifiers import SpecifierSet
            from packaging.version import Version

            spec = SpecifierSet(requirement)
            return Version(ruby_version) in spec
        except Exception:
            # If we can't parse, assume compatible
            return True

    def generate_dependencies(
        self,
        package_info: Dict[str, Any],
        version: str,
        dep_type: str = 'runtime'
    ) -> str:
        """Generate dependency declarations."""
        return self._generate_dependencies(package_info, version, dep_type)

    def _generate_dependencies(
        self,
        package_info: Dict[str, Any],
        version: str,
        dep_type: str
    ) -> str:
        """
        Generate dependencies from gem metadata.

        RubyGems provides dependencies in the format:
        {
            "dependencies": {
                "runtime": [{"name": "...", "requirements": "..."}],
                "development": [...]
            }
        }
        """
        deps = package_info.get('dependencies', {})
        dep_list = deps.get(dep_type, [])

        if not dep_list:
            return ""

        # Convert to ruby_add_rdepend format
        atoms = []
        for dep in dep_list:
            name = dep.get('name', '')
            requirements = dep.get('requirements', '>= 0')

            if not name:
                continue

            # Translate name to Gentoo format
            gentoo_name = self._translate_gem_name(name)
            atom = self._format_gem_atom(gentoo_name, requirements)
            atoms.append(atom)

        if dep_type == 'runtime':
            # For runtime deps, use ruby_add_rdepend style
            return '\n\t'.join(atoms)
        else:
            # For development deps, they become BDEPEND
            return '\n\t'.join(atoms)

    def _translate_gem_name(self, gem_name: str) -> str:
        """Translate gem name to Gentoo package name."""
        if self.name_translator:
            return self.name_translator.rubygems_to_gentoo(gem_name)

        # Basic translation: lowercase, underscores to hyphens
        return gem_name.lower().replace('_', '-')

    def _format_gem_atom(self, gentoo_name: str, requirements: str) -> str:
        """
        Format a Gentoo dependency atom from gem requirements.

        Gem version constraints:
        - ~> 2.1 (pessimistic): >=2.1.0 <3.0
        - ~> 2.1.3 (pessimistic): >=2.1.3 <2.2
        - >= 1.0, < 2.0: compound constraint
        - = 1.0: exact version
        """
        if not requirements or requirements == '>= 0':
            return f"dev-ruby/{gentoo_name}"

        # Parse multiple constraints (e.g., ">= 1.0, < 2.0")
        constraints = [c.strip() for c in requirements.split(',')]
        atoms = []

        for constraint in constraints:
            atom = self._parse_single_constraint(gentoo_name, constraint)
            if atom:
                atoms.append(atom)

        if not atoms:
            return f"dev-ruby/{gentoo_name}"

        if len(atoms) == 1:
            return atoms[0]

        # Multiple constraints - return compound
        # Gentoo uses multiple atoms on separate lines
        return ' '.join(atoms)

    def _parse_single_constraint(self, gentoo_name: str, constraint: str) -> Optional[str]:
        """Parse a single version constraint."""
        import re

        constraint = constraint.strip()
        if not constraint:
            return None

        # Match operator and version
        match = re.match(r'^([~>=<!]+)\s*(\d+(?:\.\d+)*(?:\.[a-zA-Z0-9]+)?)$', constraint)
        if not match:
            return f"dev-ruby/{gentoo_name}"

        op, version = match.groups()
        gentoo_version = self._translate_gem_version(version)

        if op == '~>':
            # Pessimistic constraint: ~> 2.1 means >= 2.1, < 3.0
            # ~> 2.1.3 means >= 2.1.3, < 2.2.0
            parts = version.split('.')
            if len(parts) >= 2:
                upper_parts = parts[:-1]
                upper_parts[-1] = str(int(upper_parts[-1]) + 1)
                upper_version = '.'.join(upper_parts)
                return f">=dev-ruby/{gentoo_name}-{gentoo_version} <dev-ruby/{gentoo_name}-{upper_version}"
            return f">=dev-ruby/{gentoo_name}-{gentoo_version}"
        elif op == '>=':
            return f">=dev-ruby/{gentoo_name}-{gentoo_version}"
        elif op == '>':
            return f">dev-ruby/{gentoo_name}-{gentoo_version}"
        elif op == '<=':
            return f"<=dev-ruby/{gentoo_name}-{gentoo_version}"
        elif op == '<':
            return f"<dev-ruby/{gentoo_name}-{gentoo_version}"
        elif op == '=' or op == '==':
            return f"=dev-ruby/{gentoo_name}-{gentoo_version}"
        elif op == '!=':
            return f"!=dev-ruby/{gentoo_name}-{gentoo_version}"

        return f"dev-ruby/{gentoo_name}"

    def _translate_gem_version(self, gem_version: str) -> str:
        """
        Translate gem version to Gentoo format.

        Gem versions:
        - 1.0.0.pre1 -> 1.0.0_pre1
        - 1.0.0.beta1 -> 1.0.0_beta1
        - 1.0.0.rc1 -> 1.0.0_rc1
        - 1.0.0.alpha -> 1.0.0_alpha
        - 1.0.0.alpha.pre.4 -> 1.0.0_alpha_pre_p4 (standalone numbers become _p)
        """
        import re

        # Standard Gentoo suffix names
        standard_suffixes = {'alpha', 'beta', 'pre', 'rc'}

        # Ruby shorthand -> Gentoo suffix (e.g., 5.a -> 5_alpha)
        shorthand_map = {'a': 'alpha', 'b': 'beta'}

        # Split into base version and suffix
        match = re.match(r'^(\d+(?:\.\d+)*)(.*)$', gem_version)
        if not match:
            return gem_version

        base, suffix = match.groups()

        if not suffix:
            return base

        # Parse suffix components
        suffix = suffix.lstrip('.')
        if not suffix:
            return base

        components = suffix.split('.')

        # Build the Gentoo suffix
        gentoo_suffix = ''
        for comp in components:
            comp_lower = comp.lower()

            # Check for Ruby shorthand (a, b, a1, b2)
            if comp_lower in shorthand_map:
                gentoo_suffix += f'_{shorthand_map[comp_lower]}'
            elif comp_lower in standard_suffixes:
                gentoo_suffix += f'_{comp_lower}'
            elif comp.isdigit():
                # Standalone number - treat as patchlevel
                gentoo_suffix += f'_p{comp}'
            else:
                # Check for shorthand with number (a1 -> alpha1, b2 -> beta2)
                m = re.match(r'^([ab])(\d+)$', comp_lower)
                if m:
                    gentoo_suffix += f'_{shorthand_map[m.group(1)]}{m.group(2)}'
                else:
                    # Check for combined suffix like 'alpha1', 'beta2'
                    m = re.match(r'^([a-z]+)(\d+)$', comp_lower)
                    if m and m.group(1) in standard_suffixes:
                        gentoo_suffix += f'_{m.group(1)}{m.group(2)}'
                    else:
                        # Non-standard suffix - keep as-is (may produce invalid version)
                        gentoo_suffix += f'.{comp}'

        return base + gentoo_suffix

    def _translate_license(self, licenses: List[str]) -> str:
        """
        Translate RubyGems licenses to Gentoo license names.

        Common mappings:
        - MIT -> MIT
        - Apache-2.0 -> Apache-2.0
        - GPL-2.0 -> GPL-2
        - BSD-3-Clause -> BSD
        """
        if not licenses:
            return "unknown"

        license_map = {
            'MIT': 'MIT',
            'Apache-2.0': 'Apache-2.0',
            'Apache 2.0': 'Apache-2.0',
            'GPL-2.0': 'GPL-2',
            'GPL-3.0': 'GPL-3',
            'BSD-3-Clause': 'BSD',
            'BSD-2-Clause': 'BSD-2',
            'Ruby': 'Ruby',
            'ISC': 'ISC',
            'LGPL-2.1': 'LGPL-2.1',
            'LGPL-3.0': 'LGPL-3',
            'MPL-2.0': 'MPL-2.0',
        }

        translated = []
        for lic in licenses:
            if lic in license_map:
                translated.append(license_map[lic])
            else:
                translated.append(lic)

        return ' '.join(translated)

    def _escape_string(self, s: str) -> str:
        """Escape string for use in ebuild."""
        return s.replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')


class RubyGemsPlugin(EcosystemPlugin):
    """
    RubyGems ecosystem plugin.

    This plugin provides RubyGems integration for the portage-fuse system,
    enabling installation of Ruby gems through Portage using ruby-fakegem.
    """

    @property
    def name(self) -> str:
        return "rubygems"

    @property
    def display_name(self) -> str:
        return "RubyGems"

    @property
    def default_category(self) -> str:
        return "dev-ruby"

    @property
    def default_repo_location(self) -> str:
        return "/var/db/repos/rubygems"

    @property
    def repo_name(self) -> str:
        return "portage-gem-fuse"

    def get_metadata_provider(
        self,
        cache_dir: Optional[str] = None,
        cache_ttl: int = 3600,
        **kwargs
    ) -> MetadataProviderBase:
        """Get the RubyGems metadata provider."""
        return RubyGemsMetadataProvider(
            cache_dir=cache_dir,
            cache_ttl=cache_ttl
        )

    def get_ebuild_generator(self, **kwargs) -> EbuildGeneratorBase:
        """Get the Ruby ebuild generator."""
        return RubyGemsEbuildGenerator(**kwargs)

    def get_name_translator(self):
        """Get the gem -> Gentoo name translator."""
        from portage_pip_fuse.ecosystems.rubygems.name_translator import (
            RubyGemsNameTranslator,
            create_rubygems_translator
        )
        return create_rubygems_translator()

    def get_source_providers(self, enable_git: bool = True, **kwargs) -> List['SourceProviderBase']:
        """Get the source providers for Ruby gems."""
        from portage_pip_fuse.ecosystems.rubygems.source_provider import (
            GemSourceProvider,
            RubyGitProvider,
        )

        providers = [GemSourceProvider()]

        if enable_git:
            git_source_patch_store = kwargs.get('git_source_patch_store')
            providers.append(RubyGitProvider(git_source_patch_store))

        # Sort by priority
        providers.sort(key=lambda p: p.priority(), reverse=True)
        return providers

    def get_version_filters(self) -> List[Any]:
        """Get default version filters for Ruby gems."""
        # Import Ruby-specific filters
        from portage_pip_fuse.ecosystems.rubygems.filters import (
            RubyCompatFilter,
            GemSourceFilter,
        )
        return [
            GemSourceFilter(),
            RubyCompatFilter(),
        ]

    def get_package_filters(self) -> List[Any]:
        """Get default package filters."""
        return []

    def register_cli_commands(self, parser: 'ArgumentParser') -> None:
        """Register gem/bundle CLI commands."""
        # Commands are registered in the main CLI module
        pass

    def get_cli_handler(self, command: str) -> Optional[Callable[['Namespace'], int]]:
        """Get handler for a CLI command."""
        if command == 'gem':
            from portage_pip_fuse.ecosystems.rubygems.cli import gem_command
            return lambda args: gem_command()
        elif command == 'bundle':
            from portage_pip_fuse.ecosystems.rubygems.cli import bundle_command
            return lambda args: bundle_command()
        return None

    def get_static_dirs(self) -> Set[str]:
        """Get static directories for RubyGems filesystem."""
        dirs = super().get_static_dirs()
        # Add .sys virtual filesystem directories
        dirs.update({
            "/.sys",
            "/.sys/RDEPEND",
            "/.sys/RDEPEND/dev-ruby",
            "/.sys/RDEPEND-patch",
            "/.sys/RDEPEND-patch/dev-ruby",
            "/.sys/DEPEND",
            "/.sys/DEPEND/dev-ruby",
            "/.sys/DEPEND-patch",
            "/.sys/DEPEND-patch/dev-ruby",
            "/.sys/ruby-compat",
            "/.sys/ruby-compat/dev-ruby",
            "/.sys/ruby-compat-patch",
            "/.sys/ruby-compat-patch/dev-ruby",
            "/.sys/name-translation",
            "/.sys/git-source",
            "/.sys/git-source/dev-ruby",
            "/.sys/git-source-patch",
            "/.sys/git-source-patch/dev-ruby",
        })
        return dirs


# Register the plugin when the module is imported
PluginRegistry.register('rubygems', RubyGemsPlugin)
