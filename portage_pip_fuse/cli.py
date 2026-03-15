#!/usr/bin/env python3
"""
Command-line interface for portage-pip-fuse.

This module provides the main CLI entry point for mounting and managing
the PyPI-to-Gentoo FUSE filesystem.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import argparse
import logging
import os
import re
import sys
import signal
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional, Set, Dict, Any

from portage_pip_fuse.filesystem import mount_filesystem, PortagePipFS
from portage_pip_fuse.package_filter import FilterRegistry
from portage_pip_fuse.sqlite_metadata import SQLiteMetadataBackend
from portage_pip_fuse.constants import REPO_NAME, REPO_LOCATION, find_cache_dir
from portage_pip_fuse.prefetcher import create_prefetched_translator
from portage_pip_fuse.pip_metadata import EbuildDataExtractor

# Lazy-initialized translator with Gentoo repository mappings
_prefetched_translator = None


def _get_translator():
    """
    Get the prefetched name translator, initializing it on first use.

    This lazy initialization avoids slow startup for commands that don't
    need name translation.
    """
    global _prefetched_translator
    if _prefetched_translator is None:
        _prefetched_translator = create_prefetched_translator()
    return _prefetched_translator


def pypi_to_gentoo(pypi_name: str) -> str:
    """
    Translate PyPI package name to Gentoo package name.

    Uses the prefetched translator which has mappings from actual Gentoo
    repositories (e.g., psycopg2 -> psycopg).
    """
    return _get_translator().pypi_to_gentoo(pypi_name)

# Try to import pip's packaging library for requirement parsing
try:
    from pip._vendor.packaging.requirements import Requirement, InvalidRequirement
    from pip._vendor.packaging.specifiers import SpecifierSet
except ImportError:
    try:
        from packaging.requirements import Requirement, InvalidRequirement
        from packaging.specifiers import SpecifierSet
    except ImportError:
        Requirement = None
        InvalidRequirement = ValueError
        SpecifierSet = None


def signal_handler(signum, frame):
    """Handle termination signals gracefully."""
    print(f"\nReceived signal {signum}, shutting down...")
    # Exit cleanly - FUSE will handle unmounting
    os._exit(0)


def validate_mountpoint(path: str) -> Path:
    """Validate mountpoint (must exist, will not be created)."""
    mountpoint = Path(path).resolve()

    # Check if mountpoint exists - do NOT create it
    if not mountpoint.exists():
        print(f"Error: Mountpoint does not exist: {mountpoint}")
        print(f"Create it first with:")
        print(f"  sudo mkdir -p {mountpoint}")
        print(f"  sudo chown $(id -u):$(id -g) {mountpoint}")
        sys.exit(1)

    # Check if it's a directory
    if not mountpoint.is_dir():
        print(f"Error: {mountpoint} is not a directory")
        sys.exit(1)
    
    # Check if directory is empty
    try:
        if any(mountpoint.iterdir()):
            print(f"Warning: {mountpoint} is not empty")
            response = input("Continue anyway? [y/N]: ")
            if response.lower() not in ['y', 'yes']:
                sys.exit(0)
    except PermissionError:
        print(f"Error: Permission denied accessing {mountpoint}")
        sys.exit(1)
    
    return mountpoint


def check_fuse_availability():
    """Check if FUSE is available on the system."""
    try:
        import fuse
    except ImportError:
        print("Error: Python FUSE library not found")
        print("Install with: pip install fusepy")
        sys.exit(1)
    
    # Check if /dev/fuse exists
    if not os.path.exists('/dev/fuse'):
        print("Error: FUSE not available on this system")
        print("Make sure FUSE kernel module is loaded: modprobe fuse")
        sys.exit(1)


def _translate_pypi_version(pypi_version: str) -> str:
    """
    Translate PyPI version string to Gentoo format.

    Converts PEP 440 pre-release and post-release markers:
    - a/alpha -> _alpha (e.g., 2.0a0 -> 2.0_alpha0)
    - b/beta -> _beta (e.g., 1.0b1 -> 1.0_beta1)
    - rc/c -> _rc (e.g., 3.0rc1 -> 3.0_rc1)
    - .post -> _p (e.g., 1.0.post1 -> 1.0_p1)
    - .dev -> _pre (e.g., 1.0.dev1 -> 1.0_pre1)
    """
    version = pypi_version

    # Handle pre-release markers (must check longer patterns first)
    # Use negative lookbehind to avoid matching 'a'/'b' in already-translated '_alpha'/'_beta'
    version = re.sub(r'\.?alpha(\d+)', r'_alpha\1', version)
    version = re.sub(r'(?<![a-z])\.?a(\d+)', r'_alpha\1', version)
    version = re.sub(r'\.?beta(\d+)', r'_beta\1', version)
    version = re.sub(r'(?<![a-z])\.?b(\d+)', r'_beta\1', version)
    version = re.sub(r'\.?rc(\d+)', r'_rc\1', version)
    version = re.sub(r'(?<!r)\.?c(\d+)', r'_rc\1', version)
    version = re.sub(r'\.post(\d+)', r'_p\1', version)
    version = re.sub(r'\.dev(\d+)', r'_pre\1', version)

    return version


def _format_gentoo_atom(package_name: str, specifier=None) -> str:
    """
    Format a Gentoo dependency atom from a PyPI package name and optional specifier.

    Args:
        package_name: PyPI package name
        specifier: packaging SpecifierSet or None

    Returns:
        Formatted Gentoo atom (e.g., ">=dev-python/requests-2.0.0")
    """
    gentoo_name = pypi_to_gentoo(package_name)

    if specifier is None or (SpecifierSet is not None and len(specifier) == 0):
        return f"dev-python/{gentoo_name}"

    # Convert PyPI version specifiers to Gentoo format
    dep_parts = []
    for spec in specifier:
        operator = spec.operator
        version = _translate_pypi_version(spec.version)

        if operator == '==':
            # Handle wildcard versions: PyPI ==23.* -> Gentoo =pkg-23*
            if version.endswith('.*'):
                version = version[:-2] + '*'
                dep_parts.append(f"=dev-python/{gentoo_name}-{version}")
            else:
                # Use ~ to match revision bumps (e.g., 1.0.0-r1)
                dep_parts.append(f"~dev-python/{gentoo_name}-{version}")
        elif operator == '>=':
            dep_parts.append(f">=dev-python/{gentoo_name}-{version}")
        elif operator == '>':
            dep_parts.append(f">dev-python/{gentoo_name}-{version}")
        elif operator == '<=':
            dep_parts.append(f"<=dev-python/{gentoo_name}-{version}")
        elif operator == '<':
            dep_parts.append(f"<dev-python/{gentoo_name}-{version}")
        elif operator == '!=':
            if version.endswith('.*'):
                version = version[:-2] + '*'
            dep_parts.append(f"!=dev-python/{gentoo_name}-{version}")
        elif operator == '~=':
            # Compatible release per PEP 440: ~=1.4 means >=1.4, <2
            version_parts = version.split('.')
            if len(version_parts) >= 2:
                upper_parts = version_parts[:-1]
                try:
                    upper_parts[-1] = str(int(upper_parts[-1]) + 1)
                    upper_version = '.'.join(upper_parts)
                    dep_parts.append(f">=dev-python/{gentoo_name}-{version}")
                    dep_parts.append(f"<dev-python/{gentoo_name}-{upper_version}")
                except ValueError:
                    dep_parts.append(f">=dev-python/{gentoo_name}-{version}")
            else:
                dep_parts.append(f">=dev-python/{gentoo_name}-{version}")

    if len(dep_parts) == 1:
        return dep_parts[0]
    else:
        # Return just the first constraint for simple cases
        # emerge handles multiple atoms as separate packages
        return dep_parts[0]


def _evaluate_marker(marker) -> bool:
    """
    Evaluate a PEP 508 environment marker against all supported Python versions.

    Uses the same logic as ebuild generation - evaluates against PYTHON_TARGETS
    from the Gentoo system, not just the running interpreter.

    Args:
        marker: A packaging.markers.Marker object, or None

    Returns:
        True if the marker evaluates to true for ANY supported Python version
    """
    if marker is None:
        return True

    # Get all supported Python versions from Gentoo's PYTHON_TARGETS
    supported_versions = EbuildDataExtractor._get_supported_python_versions()

    # Return True if the marker matches ANY supported version
    for py_ver in supported_versions:
        if EbuildDataExtractor._evaluate_marker_for_python(marker, py_ver):
            return True

    return False


def _generate_ebuild_deps(
    requirements: List[Tuple[str, Optional[Any], List[str], Optional[Any]]]
) -> Tuple[List[str], List[str]]:
    """
    Generate ebuild RDEPEND entries with conditional python_targets dependencies.

    Uses ${PYTHON_USEDEP} to ensure dependencies are built for the same Python
    targets as the package being installed. This is the standard Gentoo pattern
    for Python package dependencies.

    Args:
        requirements: List of (name, specifier, extras, marker) tuples

    Returns:
        Tuple of (rdepend_lines, extras_info) where:
        - rdepend_lines: List of dependency lines for RDEPEND
        - extras_info: List of USE flag requirements to report
    """
    from collections import defaultdict

    supported_versions = EbuildDataExtractor._get_supported_python_versions()

    # Group requirements by package name and determine which Python versions need which atom
    # Structure: {gentoo_name: {py_version: atom}}
    pkg_version_map: Dict[str, Dict[str, str]] = defaultdict(dict)
    pkg_extras: Dict[str, Set[str]] = defaultdict(set)

    for name, specifier, extras, marker in requirements:
        gentoo_name = pypi_to_gentoo(name)
        atom = _format_gentoo_atom(name, specifier)

        if extras:
            pkg_extras[gentoo_name].update(extras)

        # Determine which Python versions this requirement applies to
        for py_ver in supported_versions:
            if marker is None or EbuildDataExtractor._evaluate_marker_for_python(marker, py_ver):
                # This requirement applies to this Python version
                # If there's already a different atom for this version, keep the first one
                # (requirements files typically list more specific constraints first)
                if py_ver not in pkg_version_map[gentoo_name]:
                    pkg_version_map[gentoo_name][py_ver] = atom

    # Generate RDEPEND lines
    rdepend_lines = []
    extras_info = []

    # PYTHON_USEDEP ensures deps are built for the same Python targets
    usedep = '[${PYTHON_USEDEP}]'

    for gentoo_name in sorted(pkg_version_map.keys()):
        version_atoms = pkg_version_map[gentoo_name]
        extras = pkg_extras.get(gentoo_name, set())

        if extras:
            extras_info.append(f"dev-python/{gentoo_name} {' '.join(sorted(extras))}")

        # Check if all versions use the same atom
        unique_atoms = set(version_atoms.values())

        if len(unique_atoms) == 1:
            # All Python versions use the same atom - no conditional needed
            rdepend_lines.append(f"\t{unique_atoms.pop()}{usedep}")
        else:
            # Different atoms for different versions - generate conditionals
            # Group by atom to minimize repetition
            atom_to_versions: Dict[str, List[str]] = defaultdict(list)
            for py_ver, atom in version_atoms.items():
                atom_to_versions[atom].append(py_ver)

            for atom, versions in sorted(atom_to_versions.items()):
                for py_ver in sorted(versions):
                    use_flag = f"python_targets_python{py_ver.replace('.', '_')}"
                    rdepend_lines.append(f"\t{use_flag}? ( {atom}{usedep} )")

    return rdepend_lines, extras_info


def _generate_ebuild_content(
    project_name: str,
    requirements_file: str,
    rdepend_lines: List[str],
    python_compat: str
) -> str:
    """
    Generate complete ebuild content for a virtual dependency package.

    Args:
        project_name: Name of the project (used in description)
        requirements_file: Path to the requirements file
        rdepend_lines: List of RDEPEND dependency lines
        python_compat: PYTHON_COMPAT string

    Returns:
        Complete ebuild file content
    """
    rdepend_content = '\n'.join(rdepend_lines)

    return f'''# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

PYTHON_COMPAT=( {python_compat} )

inherit python-r1

DESCRIPTION="Virtual for {project_name} dependencies (from {requirements_file})"
HOMEPAGE=""
SRC_URI=""
S="${{WORKDIR}}"

LICENSE="metapackage"
SLOT="0"
KEYWORDS="~amd64 ~x86"
REQUIRED_USE="${{PYTHON_REQUIRED_USE}}"

RDEPEND="
\t${{PYTHON_DEPS}}
{rdepend_content}
"

src_unpack() {{
\teinfo "This is a stub package that only pulls in dependencies for {project_name}."
\teinfo "It does not install any files itself."
}}
'''


def _parse_requirements_file(filename: str) -> List[Tuple[str, Optional[Any], List[str], Optional[Any]]]:
    """
    Parse a requirements file and return list of (name, specifier, extras, marker) tuples.

    Handles:
    - Simple package names: requests
    - Versioned packages: requests>=2.0
    - Extras: requests[security]
    - Environment markers: requests; python_version < '3.11'
    - Comments and blank lines
    - Line continuations (\\)
    - Environment variables (${VAR})

    Args:
        filename: Path to requirements file

    Returns:
        List of (package_name, specifier, extras, marker) tuples
    """
    requirements = []

    if Requirement is None:
        print("Error: packaging library not available")
        print("Install with: pip install packaging")
        return requirements

    try:
        with open(filename, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: Requirements file not found: {filename}")
        return requirements
    except PermissionError:
        print(f"Error: Permission denied reading: {filename}")
        return requirements

    # Preprocess: join continued lines, expand env vars
    lines = []
    current_line = ""

    for line in content.splitlines():
        # Join lines ending with backslash
        if line.rstrip().endswith('\\'):
            current_line += line.rstrip()[:-1]
            continue
        else:
            current_line += line
            lines.append(current_line)
            current_line = ""

    if current_line:  # Handle last line if it had continuation
        lines.append(current_line)

    for line_num, line in enumerate(lines, start=1):
        # Strip comments
        if '#' in line:
            line = line.split('#')[0]
        line = line.strip()

        # Skip empty lines
        if not line:
            continue

        # Skip options lines (-r, -c, --index-url, etc.)
        if line.startswith('-'):
            # Handle nested -r requirements
            if line.startswith('-r ') or line.startswith('--requirement '):
                nested_file = line.split(None, 1)[1].strip()
                # Resolve relative paths
                if not os.path.isabs(nested_file):
                    nested_file = os.path.join(os.path.dirname(filename), nested_file)
                nested_reqs = _parse_requirements_file(nested_file)
                requirements.extend(nested_reqs)
            continue

        # Expand environment variables
        env_var_pattern = re.compile(r'\$\{([A-Z0-9_]+)\}')
        for match in env_var_pattern.finditer(line):
            var_name = match.group(1)
            var_value = os.environ.get(var_name, '')
            line = line.replace(match.group(0), var_value)

        # Parse the requirement
        try:
            req = Requirement(line)
            requirements.append((req.name, req.specifier, list(req.extras), req.marker))
        except (InvalidRequirement, ValueError) as e:
            print(f"Warning: Skipping invalid requirement at line {line_num}: {line}")
            print(f"  Error: {e}")
            continue

    return requirements


def _get_project_metadata(start_dir: str = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Detect project name and version from pyproject.toml, setup.cfg, or setup.py.

    Searches starting from the given directory (or cwd) up to the root.

    Args:
        start_dir: Directory to start searching from (default: current directory)

    Returns:
        Tuple of (name, version), either may be None if not found
    """
    if start_dir is None:
        start_dir = os.getcwd()

    search_dir = Path(start_dir).resolve()

    while search_dir != search_dir.parent:
        # Try pyproject.toml first
        pyproject = search_dir / 'pyproject.toml'
        if pyproject.exists():
            try:
                content = pyproject.read_text()
                name = None
                version = None

                # Look for [project] section
                name_match = re.search(
                    r'^\s*\[project\]\s*$.*?^\s*name\s*=\s*["\']([^"\']+)["\']',
                    content, re.MULTILINE | re.DOTALL
                )
                if name_match:
                    name = name_match.group(1)

                version_match = re.search(
                    r'^\s*\[project\]\s*$.*?^\s*version\s*=\s*["\']([^"\']+)["\']',
                    content, re.MULTILINE | re.DOTALL
                )
                if version_match:
                    version = version_match.group(1)

                # Also try [tool.poetry]
                if not name:
                    name_match = re.search(
                        r'^\s*\[tool\.poetry\]\s*$.*?^\s*name\s*=\s*["\']([^"\']+)["\']',
                        content, re.MULTILINE | re.DOTALL
                    )
                    if name_match:
                        name = name_match.group(1)

                if not version:
                    version_match = re.search(
                        r'^\s*\[tool\.poetry\]\s*$.*?^\s*version\s*=\s*["\']([^"\']+)["\']',
                        content, re.MULTILINE | re.DOTALL
                    )
                    if version_match:
                        version = version_match.group(1)

                if name:
                    return name, version
            except Exception:
                pass

        # Try setup.cfg
        setup_cfg = search_dir / 'setup.cfg'
        if setup_cfg.exists():
            try:
                content = setup_cfg.read_text()
                name = None
                version = None

                name_match = re.search(
                    r'^\s*\[metadata\]\s*$.*?^\s*name\s*=\s*(.+?)\s*$',
                    content, re.MULTILINE | re.DOTALL
                )
                if name_match:
                    name = name_match.group(1).strip()

                version_match = re.search(
                    r'^\s*\[metadata\]\s*$.*?^\s*version\s*=\s*(.+?)\s*$',
                    content, re.MULTILINE | re.DOTALL
                )
                if version_match:
                    version = version_match.group(1).strip()

                if name:
                    return name, version
            except Exception:
                pass

        # Try setup.py with simple regex
        setup_py = search_dir / 'setup.py'
        if setup_py.exists():
            try:
                content = setup_py.read_text()
                name = None
                version = None

                name_match = re.search(r'name\s*=\s*["\']([^"\']+)["\']', content)
                if name_match:
                    name = name_match.group(1)

                version_match = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
                if version_match:
                    version = version_match.group(1)

                if name:
                    return name, version
            except Exception:
                pass

        search_dir = search_dir.parent

    return None, None


def _get_project_name(start_dir: str = None) -> Optional[str]:
    """
    Detect project name from pyproject.toml, setup.cfg, or setup.py.

    Searches starting from the given directory (or cwd) up to the root.

    Args:
        start_dir: Directory to start searching from (default: current directory)

    Returns:
        Project name if found, None otherwise
    """
    name, _ = _get_project_metadata(start_dir)
    return name


def _derive_set_name(requirements_file: str) -> str:
    """
    Derive a portage set name from the project name or requirements file path.

    First tries to detect the project name from pyproject.toml, setup.cfg,
    or setup.py. Falls back to using the requirements file path.

    Examples:
        (with pyproject.toml name="odoo") -> odoo-dependencies
        requirements.txt -> requirements-dependencies
        my-project/requirements.txt -> my-project-dependencies
        requirements-dev.txt -> requirements-dev-dependencies
    """
    # First, try to get project name from project metadata
    req_dir = Path(requirements_file).resolve().parent
    project_name = _get_project_name(str(req_dir))

    if project_name:
        name = project_name
    else:
        # Fall back to filename/directory based naming
        path = Path(requirements_file)
        name = path.stem

        # If it's just "requirements", use parent directory name if available
        if name == 'requirements' and path.parent.name and path.parent.name != '.':
            name = path.parent.name

    # Sanitize the name for portage (lowercase, hyphens only)
    name = re.sub(r'[^a-zA-Z0-9-]', '-', name.lower())
    name = re.sub(r'-+', '-', name).strip('-')

    return f"{name}-dependencies"


def pip_command():
    """
    Handle pip subcommand - translates pip install arguments to emerge commands.

    Supports:
    - pip install package1 package2 ...
    - pip install -r requirements.txt
    - pip install --upgrade package
    - pip install package[extra1,extra2]
    - pip install package>=1.0

    For -r requirements.txt, creates /etc/portage/sets/{P}-dependencies
    """
    pip_parser = argparse.ArgumentParser(
        prog='portage-pip-fuse pip',
        description='Translate pip install commands to emerge commands',
        usage='portage-pip-fuse pip install [options] [packages...]',
        epilog='''
Examples:
  %(prog)s install requests                    # Translate to: emerge dev-python/requests
  %(prog)s install requests>=2.0 flask         # With version constraints
  %(prog)s install -r requirements.txt         # Create portage set and emerge it
  %(prog)s install --upgrade requests          # emerge --update requests
  %(prog)s install requests[security]          # Package with extras (USE flags)

The -r requirements.txt option creates a portage set file at:
  /etc/portage/sets/{project}-dependencies

Then runs: emerge @{project}-dependencies
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # We expect 'pip install' as the subcommand structure
    pip_parser.add_argument(
        'subcommand',
        nargs='?',
        choices=['install'],
        default='install',
        help='pip subcommand (currently only install is supported)'
    )

    pip_parser.add_argument(
        'packages',
        nargs='*',
        help='Package specifiers to install'
    )

    pip_parser.add_argument(
        '-r', '--requirement',
        action='append',
        dest='requirements',
        metavar='FILE',
        help='Install from requirements file(s)'
    )

    pip_parser.add_argument(
        '-U', '--upgrade',
        action='store_true',
        help='Upgrade packages (translates to emerge --update)'
    )

    pip_parser.add_argument(
        '-e', '--editable',
        action='append',
        dest='editables',
        metavar='PATH',
        help='Editable installs (not supported - will show warning)'
    )

    pip_parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without executing'
    )

    pip_parser.add_argument(
        '--pretend',
        action='store_true',
        help='Pass --pretend to emerge (show what would be merged)'
    )

    pip_parser.add_argument(
        '--ask',
        action='store_true',
        default=True,
        help='Pass --ask to emerge (default: True)'
    )

    pip_parser.add_argument(
        '--no-ask',
        action='store_true',
        help='Do not ask for confirmation before emerging'
    )

    pip_parser.add_argument(
        '--set-dir',
        type=str,
        default='/etc/portage/sets',
        help='Directory for portage set files (default: /etc/portage/sets)'
    )

    pip_parser.add_argument(
        '--no-deps',
        action='store_true',
        help='Ignored (emerge always handles dependencies)'
    )

    pip_parser.add_argument(
        '--pre',
        action='store_true',
        help='Include pre-release versions (passed as ~arch keyword)'
    )

    pip_parser.add_argument(
        '--deps-overlay',
        type=str,
        metavar='PATH',
        help='Generate a virtual dependency ebuild in the specified overlay. '
             'Creates virtual/{PN}/{PN}-{PV}.ebuild with proper python_targets_* conditionals.'
    )

    # Remove 'pip' from argv and parse remaining args
    pip_argv = []
    skip_next = False
    for i, arg in enumerate(sys.argv[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if arg == 'pip':
            continue
        pip_argv.append(arg)

    args = pip_parser.parse_args(pip_argv)

    # Check for install subcommand
    if args.subcommand != 'install':
        print(f"Error: Only 'pip install' is currently supported")
        return 1

    # Warn about editable installs
    if args.editables:
        print("Warning: Editable installs (-e) are not supported by portage")
        print("  Skipping:", ', '.join(args.editables))

    # Collect all packages to install
    all_packages: List[Tuple[str, Optional[Any], List[str], Optional[Any]]] = []
    set_files_created: List[Tuple[str, str]] = []  # (filename, set_name)
    skipped_markers: List[Tuple[str, str]] = []  # (name, marker) for reporting

    # Parse direct package arguments
    if args.packages:
        for pkg_spec in args.packages:
            # Skip 'install' if it appears as a package
            if pkg_spec == 'install':
                continue
            if Requirement is not None:
                try:
                    req = Requirement(pkg_spec)
                    # Check environment marker
                    if not _evaluate_marker(req.marker):
                        skipped_markers.append((req.name, str(req.marker)))
                        continue
                    all_packages.append((req.name, req.specifier, list(req.extras), req.marker))
                except (InvalidRequirement, ValueError) as e:
                    print(f"Warning: Invalid package specifier: {pkg_spec}")
                    print(f"  Error: {e}")
            else:
                # Fallback: just use the name
                all_packages.append((pkg_spec, None, [], None))

    # Parse requirements files
    if args.requirements:
        # Handle --deps-overlay mode
        if args.deps_overlay:
            # Combine all requirements files for ebuild generation
            all_reqs = []
            for req_file in args.requirements:
                reqs = _parse_requirements_file(req_file)
                if reqs:
                    all_reqs.extend(reqs)

            if not all_reqs:
                print("Error: No valid requirements found")
                return 1

            # Get project name and version
            req_dir = Path(args.requirements[0]).resolve().parent
            project_name, project_version = _get_project_metadata(str(req_dir))

            if not project_name:
                print("Error: Could not detect project name from pyproject.toml, setup.cfg, or setup.py")
                print("Make sure you're running from a Python project directory")
                return 1

            if not project_version:
                print("Warning: Could not detect project version, using '9999'")
                project_version = '9999'

            # Translate version to Gentoo format
            gentoo_version = _translate_pypi_version(project_version)

            # Get PYTHON_COMPAT
            supported_versions = EbuildDataExtractor._get_supported_python_versions()
            python_compat = ' '.join(f"python{v.replace('.', '_')}" for v in supported_versions)

            # Generate ebuild dependencies (includes ALL requirements with markers)
            rdepend_lines, extras_info = _generate_ebuild_deps(all_reqs)

            # Generate ebuild content
            ebuild_content = _generate_ebuild_content(
                project_name,
                ', '.join(args.requirements),
                rdepend_lines,
                python_compat
            )

            # Report extras as USE flags
            if extras_info:
                print("\nNote: The following packages require USE flags:")
                print("Add to /etc/portage/package.use:")
                for info in extras_info:
                    print(f"  {info}")
                print()

            # Construct ebuild path: <overlay>/virtual/<PN>/<PN>-<PV>.ebuild
            overlay_path = Path(args.deps_overlay)
            pkg_name = pypi_to_gentoo(project_name)
            ebuild_dir = overlay_path / 'virtual' / pkg_name
            ebuild_path = ebuild_dir / f'{pkg_name}-{gentoo_version}.ebuild'

            if args.dry_run:
                print(f"\n--- Would create {ebuild_path} ---")
                print(ebuild_content)
                print(f"--- End {ebuild_path} ---\n")
            else:
                try:
                    ebuild_dir.mkdir(parents=True, exist_ok=True)
                    ebuild_path.write_text(ebuild_content)
                    print(f"Created ebuild: {ebuild_path}")
                    print(f"\nTo install, run:")
                    print(f"  ebuild {ebuild_path} manifest")
                    print(f"  emerge -av =virtual/{pkg_name}-{gentoo_version}")
                except PermissionError:
                    print(f"Error: Permission denied writing {ebuild_path}")
                    print("Try running with sudo or check overlay permissions")
                    return 1

            return 0

        # Standard set file generation
        set_dir = Path(args.set_dir)

        for req_file in args.requirements:
            reqs = _parse_requirements_file(req_file)

            if not reqs:
                print(f"Warning: No valid requirements found in {req_file}")
                continue

            # Create portage set file
            set_name = _derive_set_name(req_file)
            set_path = set_dir / set_name

            # Generate set file content
            set_content_lines = [
                f"# Generated from {req_file}",
                f"# by portage-pip-fuse pip install -r {req_file}",
                ""
            ]

            # Filter by environment markers
            filtered_reqs = []
            for name, specifier, extras, marker in reqs:
                if not _evaluate_marker(marker):
                    skipped_markers.append((name, str(marker)))
                    continue
                filtered_reqs.append((name, specifier, extras, marker))

            # Deduplicate: group by package name and merge versions
            # This handles cases where different Python versions need different package versions
            from collections import defaultdict
            pkg_atoms: Dict[str, Set[str]] = defaultdict(set)
            pkg_extras: Dict[str, Set[str]] = defaultdict(set)

            for name, specifier, extras, marker in filtered_reqs:
                gentoo_name = pypi_to_gentoo(name)
                atom = _format_gentoo_atom(name, specifier)
                pkg_atoms[gentoo_name].add(atom)
                if extras:
                    pkg_extras[gentoo_name].update(extras)

            # Generate deduplicated atoms
            version_conflicts = []
            for gentoo_name in sorted(pkg_atoms.keys()):
                atoms = pkg_atoms[gentoo_name]
                extras = pkg_extras.get(gentoo_name, set())

                if extras:
                    use_flags = ' '.join(sorted(extras))
                    set_content_lines.append(f"# USE flags: {use_flags}")

                if len(atoms) == 1:
                    # Single version - use as-is
                    set_content_lines.append(atoms.pop())
                else:
                    # Multiple versions - use unversioned atom and report
                    set_content_lines.append(f"dev-python/{gentoo_name}")
                    version_conflicts.append((gentoo_name, sorted(atoms)))

            set_content = '\n'.join(set_content_lines) + '\n'

            # Report version conflicts
            if version_conflicts:
                print(f"\nNote: {len(version_conflicts)} packages have different versions for different Python targets.")
                print("Using unversioned atoms (portage will select appropriate version):")
                for pkg, atoms in version_conflicts:
                    print(f"  {pkg}: {', '.join(atoms)}")
                print()

            if args.dry_run:
                print(f"\n--- Would create {set_path} ---")
                print(set_content)
                print(f"--- End {set_path} ---\n")
                # Track for emerge command even in dry-run
                set_files_created.append((str(set_path), set_name))
            else:
                # Create set directory if needed
                try:
                    set_dir.mkdir(parents=True, exist_ok=True)
                except PermissionError:
                    print(f"Error: Permission denied creating {set_dir}")
                    print("Try running with sudo")
                    return 1

                # Write set file
                try:
                    set_path.write_text(set_content)
                    print(f"Created portage set: {set_path}")
                    set_files_created.append((str(set_path), set_name))
                except PermissionError:
                    print(f"Error: Permission denied writing {set_path}")
                    print("Try running with sudo")
                    return 1

            # Add filtered requirements to the package list for USE flag handling
            all_packages.extend(filtered_reqs)

    # Check if we have anything to install
    if not all_packages and not set_files_created and not args.dry_run:
        print("Error: No packages specified")
        pip_parser.print_help()
        return 1

    # Report skipped packages due to markers
    if skipped_markers:
        supported_versions = EbuildDataExtractor._get_supported_python_versions()
        versions_str = ', '.join(supported_versions)
        print(f"\nSkipped {len(skipped_markers)} packages (not applicable to Python {{{versions_str}}}):")
        for name, marker in skipped_markers:
            print(f"  {name}: {marker}")
        print()

    # Collect USE flag requirements (extras)
    use_requirements: Dict[str, Set[str]] = {}
    for name, specifier, extras, marker in all_packages:
        if extras:
            gentoo_name = pypi_to_gentoo(name)
            if gentoo_name not in use_requirements:
                use_requirements[gentoo_name] = set()
            use_requirements[gentoo_name].update(extras)

    # Show USE flag requirements
    if use_requirements:
        print("\nNote: The following packages require USE flags:")
        print("Add to /etc/portage/package.use:")
        for pkg, flags in sorted(use_requirements.items()):
            print(f"  dev-python/{pkg} {' '.join(sorted(flags))}")
        print()

    # Build emerge command
    emerge_cmd = ['emerge']

    if args.ask and not args.no_ask:
        emerge_cmd.append('--ask')

    if args.pretend:
        emerge_cmd.append('--pretend')

    if args.upgrade:
        emerge_cmd.append('--update')

    # If we created set files, emerge the sets
    if set_files_created:
        for _, set_name in set_files_created:
            emerge_cmd.append(f'@{set_name}')

    # Add individual packages (not from requirements files)
    if args.packages:
        for pkg_spec in args.packages:
            if pkg_spec == 'install':
                continue
            if Requirement is not None:
                try:
                    req = Requirement(pkg_spec)
                    # Check environment marker
                    if not _evaluate_marker(req.marker):
                        continue  # Already reported in skipped_markers
                    atom = _format_gentoo_atom(req.name, req.specifier)
                    emerge_cmd.append(atom)
                except (InvalidRequirement, ValueError):
                    gentoo_name = pypi_to_gentoo(pkg_spec)
                    emerge_cmd.append(f'dev-python/{gentoo_name}')
            else:
                gentoo_name = pypi_to_gentoo(pkg_spec)
                emerge_cmd.append(f'dev-python/{gentoo_name}')

    # Show or execute the command
    if len(emerge_cmd) > 1:  # More than just 'emerge'
        cmd_str = ' '.join(emerge_cmd)

        if args.dry_run:
            print(f"Would run: {cmd_str}")
        else:
            print(f"Running: {cmd_str}")
            try:
                result = subprocess.run(emerge_cmd)
                return result.returncode
            except FileNotFoundError:
                print("Error: emerge not found. Is Portage installed?")
                return 1
            except KeyboardInterrupt:
                print("\nInterrupted")
                return 130

    return 0


def install_command():
    """Handle install subcommand - creates repos.conf file."""
    install_parser = argparse.ArgumentParser(
        prog='portage-pip-fuse install',
        description='Create portage repos.conf file for the FUSE overlay'
    )

    install_parser.add_argument(
        'mountpoint',
        nargs='?',
        default=REPO_LOCATION,
        help=f'Directory where the filesystem will be mounted (default: {REPO_LOCATION})'
    )

    install_parser.add_argument(
        '--priority',
        type=int,
        default=-50,
        help='Repository priority (default: -50, lower than gentoo to act as fallback)'
    )

    # Remove 'install' from argv and parse remaining args
    install_argv = [arg for arg in sys.argv[1:] if arg != 'install']
    args = install_parser.parse_args(install_argv)

    mountpoint = Path(args.mountpoint).resolve()
    repos_conf_dir = Path('/etc/portage/repos.conf')
    conf_file = repos_conf_dir / f'{REPO_NAME}.conf'

    conf_content = f"""[{REPO_NAME}]
location = {mountpoint}
sync-type =
auto-sync = no
priority = {args.priority}
"""

    # Check if repos.conf directory exists
    if not repos_conf_dir.exists():
        print(f"Error: {repos_conf_dir} does not exist")
        return 1

    # Check if file already exists
    if conf_file.exists():
        print(f"Warning: {conf_file} already exists")
        response = input("Overwrite? [y/N]: ")
        if response.lower() not in ['y', 'yes']:
            print("Aborted")
            return 0

    try:
        conf_file.write_text(conf_content)
        print(f"Created {conf_file}")
        print(f"\nTo use the overlay:")
        print(f"  1. Mount the filesystem: portage-pip-fuse {mountpoint}")
        print(f"  2. Emerge packages: emerge -av dev-python/requests")
        return 0
    except PermissionError:
        print(f"Error: Permission denied writing to {conf_file}")
        print("Try running with sudo")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


def sync_command():
    """Handle sync subcommand."""
    sync_parser = argparse.ArgumentParser(
        prog='portage-pip-fuse sync',
        description='Sync PyPI metadata database with latest data',
        epilog='''
Custom flow for overlayfs/tmpfs:
  1. %(prog)s --only-download          # Download compressed database
  2. Mount overlayfs with tmpfs on top
  3. %(prog)s --only-decompress         # Decompress to tmpfs overlay
  4. Use the database normally
  5. %(prog)s --delete-sqlite           # Remove uncompressed database
  6. Sync overlayfs cache to disk

This workflow allows using the large SQLite database on memory-constrained systems.
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    sync_parser.add_argument(
        '--cache-dir',
        type=str,
        help='Cache directory for PyPI metadata database'
    )
    
    sync_parser.add_argument(
        '-d', '--debug',
        action='store_true',
        help='Enable debug output'
    )
    
    sync_parser.add_argument(
        '--only-download',
        action='store_true',
        help='Only download the compressed database and verify SHA256, do not decompress'
    )
    
    sync_parser.add_argument(
        '--only-decompress',
        action='store_true',
        help='Only decompress existing .gz file, do not delete it'
    )
    
    sync_parser.add_argument(
        '--delete-gz',
        action='store_true',
        help='Delete the compressed .gz file'
    )
    
    sync_parser.add_argument(
        '--delete-sqlite',
        action='store_true',
        help='Delete the uncompressed SQLite database (same as unsync command)'
    )
    
    # Remove 'sync' from argv and parse remaining args
    sync_argv = [arg for arg in sys.argv[1:] if arg != 'sync']
    args = sync_parser.parse_args(sync_argv)
    
    # Check for conflicting options
    exclusive_count = sum([args.only_download, args.only_decompress, args.delete_gz, args.delete_sqlite])
    if exclusive_count > 1:
        print("Error: --only-download, --only-decompress, --delete-gz, and --delete-sqlite are mutually exclusive")
        return 1
    
    # Set up logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level, format='%(levelname)s: %(message)s')
    
    # Determine cache directory
    cache_dir = find_cache_dir(args.cache_dir)
    
    # Handle delete operations first
    if args.delete_gz:
        gz_path = cache_dir / 'pypi-data.sqlite.gz'
        if not gz_path.exists():
            print("No compressed database file found")
            return 0
        try:
            size = gz_path.stat().st_size
            gz_path.unlink()
            print(f"✓ Deleted {gz_path} ({size / (1024*1024):.1f} MB)")
            return 0
        except Exception as e:
            print(f"✗ Failed to delete {gz_path}: {e}")
            return 1
    
    if args.delete_sqlite:
        # Same as unsync command
        db_path = cache_dir / 'pypi-data.sqlite'
        if not db_path.exists():
            print("No SQLite database found")
            return 0
        try:
            size = db_path.stat().st_size
            db_path.unlink()
            print(f"✓ Deleted {db_path} ({size / (1024*1024*1024):.1f} GB)")
            return 0
        except Exception as e:
            print(f"✗ Failed to delete {db_path}: {e}")
            return 1
    
    # Create SQLite backend and perform requested sync operation
    try:
        backend = SQLiteMetadataBackend(cache_dir=cache_dir)
        
        if args.only_download:
            success = backend.sync_database(only_download=True)
        elif args.only_decompress:
            success = backend.sync_database(only_decompress=True)
        else:
            # Standard flow
            success = backend.sync_database()
        
        if success:
            if args.only_download:
                print("✓ Database download completed successfully")
            elif args.only_decompress:
                print("✓ Database decompression completed successfully")
            else:
                print("✓ Database sync completed successfully")
            return 0
        else:
            if args.only_download:
                print("✗ Database download failed")
            elif args.only_decompress:
                print("✗ Database decompression failed")
            else:
                print("✗ Database sync failed")
            return 1
            
    except Exception as e:
        print(f"✗ Error during sync: {e}")
        return 1


def unsync_command():
    """Handle unsync subcommand - delete the database."""
    unsync_parser = argparse.ArgumentParser(
        prog='portage-pip-fuse unsync',
        description='Delete the PyPI metadata database'
    )

    unsync_parser.add_argument(
        '--cache-dir',
        type=str,
        help='Cache directory for PyPI metadata database'
    )

    unsync_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Delete without confirmation'
    )

    # Remove 'unsync' from argv and parse remaining args
    unsync_argv = [arg for arg in sys.argv[1:] if arg != 'unsync']
    args = unsync_parser.parse_args(unsync_argv)

    # Determine cache directory
    cache_dir = find_cache_dir(args.cache_dir)

    db_path = cache_dir / 'pypi-data.sqlite'
    gz_path = cache_dir / 'pypi-data.sqlite.gz'
    download_path = cache_dir / 'pypi-data.sqlite.gz.__download__'

    # Check what exists
    files_to_delete = []
    if db_path.exists():
        files_to_delete.append(db_path)
    if gz_path.exists():
        files_to_delete.append(gz_path)
    if download_path.exists():
        files_to_delete.append(download_path)

    if not files_to_delete:
        print("No database files found to delete")
        return 0

    # Show what will be deleted
    print("The following files will be deleted:")
    total_size = 0
    for f in files_to_delete:
        size = f.stat().st_size
        total_size += size
        print(f"  {f} ({size / (1024*1024):.1f} MB)")
    print(f"Total: {total_size / (1024*1024):.1f} MB")

    # Confirm deletion
    if not args.force:
        response = input("Delete these files? [y/N]: ")
        if response.lower() not in ['y', 'yes']:
            print("Aborted")
            return 0

    # Delete files
    for f in files_to_delete:
        try:
            f.unlink()
            print(f"✓ Deleted {f}")
        except Exception as e:
            print(f"✗ Failed to delete {f}: {e}")
            return 1

    print("✓ Database deleted successfully")
    return 0


def unmount_command():
    """Handle unmount subcommand."""
    unmount_parser = argparse.ArgumentParser(
        prog='portage-pip-fuse unmount',
        description='Unmount the PyPI FUSE filesystem'
    )

    unmount_parser.add_argument(
        'mountpoint',
        nargs='?',
        default=REPO_LOCATION,
        help=f'Directory where the filesystem is mounted (default: {REPO_LOCATION})'
    )

    unmount_parser.add_argument(
        '--pid-file',
        type=str,
        help='PID file to read process ID from (sends SIGINT instead of using fusermount)'
    )

    # Remove 'unmount' from argv and parse remaining args
    unmount_argv = [arg for arg in sys.argv[1:] if arg != 'unmount']
    args = unmount_parser.parse_args(unmount_argv)

    mountpoint = Path(args.mountpoint).resolve()

    # Check if it's mounted
    if not mountpoint.exists():
        print(f"Error: {mountpoint} does not exist")
        return 1

    # If PID file specified, use SIGINT
    if args.pid_file:
        pid_path = Path(args.pid_file)
        if not pid_path.exists():
            print(f"Error: PID file not found: {pid_path}")
            return 1

        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGINT)
            print(f"✓ Sent SIGINT to process {pid}")
            # Clean up PID file
            pid_path.unlink(missing_ok=True)
            return 0
        except ValueError:
            print(f"Error: Invalid PID in {pid_path}")
            return 1
        except ProcessLookupError:
            print(f"Error: Process {pid} not found (stale PID file?)")
            pid_path.unlink(missing_ok=True)
            return 1
        except PermissionError:
            print(f"Error: Permission denied sending signal to process {pid}")
            return 1
        except Exception as e:
            print(f"Error: {e}")
            return 1

    # Fall back to fusermount
    import subprocess
    try:
        result = subprocess.run(
            ['fusermount', '-u', str(mountpoint)],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print(f"✓ Unmounted {mountpoint}")
            return 0
        else:
            # Check if it's not mounted
            if 'not mounted' in result.stderr or 'not found' in result.stderr:
                print(f"Error: {mountpoint} is not mounted")
            else:
                print(f"Error: {result.stderr.strip()}")
            return 1
    except FileNotFoundError:
        print("Error: fusermount not found. Install fuse-utils.")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


def mount_command():
    """Handle mount subcommand."""
    mount_parser = argparse.ArgumentParser(
        prog='portage-pip-fuse mount',
        description='Mount the PyPI FUSE filesystem',
        epilog=f'''
Examples:
  %(prog)s                                     # Mount at default location ({REPO_LOCATION})
  %(prog)s /mnt/pypi                           # Mount at custom location
  %(prog)s -f                                  # Mount in foreground
  %(prog)s -f -d                               # Mount with debug output

After mounting, you can:
  ls {REPO_LOCATION}/dev-python/requests
  cat {REPO_LOCATION}/dev-python/requests/requests-2.28.1.ebuild

To unmount:
  fusermount -u {REPO_LOCATION}
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    mount_parser.add_argument(
        'mountpoint',
        nargs='?',
        default=REPO_LOCATION,
        help=f'Directory where the filesystem will be mounted (default: {REPO_LOCATION})'
    )
    
    mount_parser.add_argument(
        '-f', '--foreground',
        action='store_true',
        help='Run in foreground instead of daemonizing'
    )

    mount_parser.add_argument(
        '-d', '--debug',
        action='store_true',
        help='Enable debug output'
    )

    mount_parser.add_argument(
        '--logfile',
        type=str,
        help='Log file path for debug output (default: stderr)'
    )

    mount_parser.add_argument(
        '--cache-ttl',
        type=int,
        default=3600,
        help='Cache time-to-live in seconds (default: 3600)'
    )

    mount_parser.add_argument(
        '--cache-dir',
        type=str,
        help='Cache directory for PyPI metadata (default: /tmp/portage-pip-fuse-cache)'
    )

    mount_parser.add_argument(
        '--pid-file',
        type=str,
        help='Write process ID to this file (for use with unmount --pid-file)'
    )

    # Get available filters from registry
    available_filters = list(FilterRegistry.get_all_filters().keys())

    # Filter configuration arguments
    mount_parser.add_argument(
        '--filter',
        type=str,
        action='append',
        choices=available_filters,
        help=f'Add package filter (available: {", ".join(available_filters)}). Can be used multiple times.'
    )

    mount_parser.add_argument(
        '--no-filter',
        type=str,
        action='append',
        choices=available_filters,
        help=f'Disable specific filter (available: {", ".join(available_filters)}). Can be used multiple times.'
    )

    mount_parser.add_argument(
        '--deps-for',
        type=str,
        action='append',
        help='Show dependency tree for specified packages (use with --filter=deps)'
    )

    mount_parser.add_argument(
        '--use-flags',
        type=str,
        help='Comma-separated Python extras/USE flags for dependency resolution'
    )

    mount_parser.add_argument(
        '--filter-days',
        type=int,
        default=30,
        help='Days to look back for recent packages (default: 30)'
    )

    mount_parser.add_argument(
        '--filter-count',
        type=int,
        default=100,
        help='Number of newest packages to show (default: 100)'
    )

    mount_parser.add_argument(
        '--timestamps',
        action='store_true',
        help='Enable PyPI timestamp lookup (slower, uses actual upload times for file mtimes)'
    )

    mount_parser.add_argument(
        '--max-versions',
        type=int,
        default=0,
        metavar='N',
        help='Limit versions shown per package (0=unlimited, default: 0). Lower values speed up directory listings.'
    )

    mount_parser.add_argument(
        '--test',
        action='store_true',
        help='Run filesystem tests without mounting'
    )

    mount_parser.add_argument(
        '--use-sqlite',
        action='store_true',
        default=True,
        help='Use SQLite backend with PyPI JSON API fallback (default: enabled)'
    )

    mount_parser.add_argument(
        '--no-sqlite',
        action='store_true',
        help='Disable SQLite backend and use only PyPI JSON API'
    )

    # Dependency patching options
    mount_parser.add_argument(
        '--patch-file',
        type=str,
        metavar='PATH',
        help='Path to dependency patch file (default: ~/.config/portage-pip-fuse/patches.json)'
    )

    mount_parser.add_argument(
        '--no-patches',
        action='store_true',
        help='Disable the dependency patching system (.sys/ filesystem)'
    )

    mount_parser.add_argument(
        '--no-git-source',
        action='store_true',
        help='Disable git repository detection for wheel-only packages'
    )

    mount_parser.add_argument(
        '--git-tag-pattern',
        type=str,
        metavar='PATTERN',
        default='v${PV}',
        help='Default git tag pattern for version mapping (default: v${PV})'
    )

    # Remove 'mount' from argv and parse remaining args
    mount_argv = [arg for arg in sys.argv[1:] if arg != 'mount']
    args = mount_parser.parse_args(mount_argv)

    # Resolve cache directory
    cache_dir = find_cache_dir(args.cache_dir)

    # Build active filter list (package filters only)
    active_filters = set(FilterRegistry.get_default_filters())

    # Track disabled filters separately (for version filters)
    disabled_filters = set(args.no_filter) if args.no_filter else set()

    # Add explicitly requested filters
    if args.filter:
        active_filters.update(args.filter)
    
    # Remove explicitly disabled filters
    if args.no_filter:
        active_filters.difference_update(args.no_filter)
    
    # Validate filter configuration
    if 'deps' in active_filters and not args.deps_for:
        mount_parser.error("Filter 'deps' requires --deps-for to specify packages")
    
    # Parse USE flags if provided
    use_flags = []
    if args.use_flags:
        use_flags = [flag.strip() for flag in args.use_flags.split(',')]
    
    # Set up logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    # Configure logging with optional file output
    if args.logfile:
        # Validate logfile path
        logfile_path = Path(args.logfile).resolve()
        
        # Create log directory if needed
        try:
            logfile_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            print(f"Error: Cannot create log directory {logfile_path.parent}")
            return 1
        
        # Set up file logging
        logging.basicConfig(
            level=log_level,
            format=log_format,
            filename=str(logfile_path),
            filemode='a'  # Append mode
        )
        
        # Also add console output for important messages
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.WARNING)
        console_formatter = logging.Formatter('%(levelname)s: %(message)s')
        console_handler.setFormatter(console_formatter)
        logging.getLogger().addHandler(console_handler)
        
        print(f"Logging to file: {logfile_path}")
        
    else:
        # Standard logging to stderr
        logging.basicConfig(
            level=log_level,
            format=log_format
        )
    
    logger = logging.getLogger(__name__)
    
    # Determine backend configuration
    use_sqlite = args.use_sqlite and not args.no_sqlite
    
    # Build filter configuration dictionary
    filter_config = {
        'active_filters': list(active_filters),
        'disabled_filters': list(disabled_filters),
        'deps_for': args.deps_for or [],
        'use_flags': use_flags,
        'days': args.filter_days,
        'count': args.filter_count,
        'no_timestamps': not args.timestamps,
        'use_sqlite': use_sqlite,
        'max_versions': args.max_versions
    }
    
    if args.test:
        # Run tests
        print("Running portage-pip-fuse tests...")
        try:
            fs = PortagePipFS(cache_ttl=args.cache_ttl, cache_dir=str(cache_dir), filter_config=filter_config)
            print("✓ Filesystem initialization successful")
            
            # Test path parsing
            test_paths = [
                "/dev-python/requests",
                "/dev-python/requests/requests-2.28.1.ebuild",
                "/profiles/repo_name"
            ]
            
            for path in test_paths:
                parsed = fs._parse_path(path)
                print(f"✓ Path parsing: {path} -> {parsed['type']}")
            
            print("All tests passed!")
            return 0
            
        except Exception as e:
            print(f"✗ Test failed: {e}")
            return 1
    
    # Check system requirements only if not testing
    if not args.test:
        check_fuse_availability()
        
        # Validate mountpoint
        mountpoint = validate_mountpoint(args.mountpoint)
    else:
        mountpoint = None
    
    # Set up signal handlers
    pid_file_path = Path(args.pid_file) if args.pid_file else None

    def cleanup_pid_file():
        if pid_file_path and pid_file_path.exists():
            try:
                pid_file_path.unlink()
            except Exception:
                pass

    def signal_handler_with_cleanup(signum, frame):
        cleanup_pid_file()
        signal_handler(signum, frame)

    signal.signal(signal.SIGINT, signal_handler_with_cleanup)
    signal.signal(signal.SIGTERM, signal_handler_with_cleanup)

    # Write PID file if requested
    if pid_file_path:
        try:
            pid_file_path.parent.mkdir(parents=True, exist_ok=True)
            pid_file_path.write_text(str(os.getpid()))
        except Exception as e:
            print(f"Error: Failed to write PID file: {e}")
            return 1

    print(f"Mounting portage-pip FUSE filesystem at {mountpoint}")
    print(f"Cache directory: {cache_dir}")
    print(f"Cache TTL: {args.cache_ttl} seconds")
    print(f"Backend: {'SQLite + JSON API fallback' if use_sqlite else 'JSON API only'}")
    print(f"Active filters: {', '.join(active_filters) if active_filters else 'none'}")

    if 'deps' in active_filters and args.deps_for:
        print(f"Showing dependencies for: {', '.join(args.deps_for)}")
        if use_flags:
            print(f"With USE flags: {', '.join(use_flags)}")

    if args.timestamps:
        print("Timestamps enabled (using PyPI upload times)")

    if args.no_patches:
        print("Dependency patching disabled")
    elif args.patch_file:
        print(f"Patch file: {args.patch_file}")
    else:
        from portage_pip_fuse.constants import DEFAULT_PATCH_FILE
        print(f"Patch file: {DEFAULT_PATCH_FILE} (default)")

    if pid_file_path:
        print(f"PID file: {pid_file_path}")

    if args.foreground:
        print("Running in foreground (Ctrl+C to unmount)")
    else:
        print("Running in background")
        if pid_file_path:
            print(f"To unmount: portage-pip-fuse unmount --pid-file {pid_file_path}")
        else:
            print(f"To unmount: fusermount -u {mountpoint}")
    
    try:
        mount_filesystem(
            str(mountpoint),
            foreground=args.foreground,
            debug=args.debug,
            cache_ttl=args.cache_ttl,
            cache_dir=str(cache_dir),
            filter_config=filter_config,
            patch_file=args.patch_file,
            no_patches=args.no_patches,
            enable_git_source=not args.no_git_source,
            git_tag_pattern=args.git_tag_pattern
        )
    except KeyboardInterrupt:
        print("\nUnmounting...")
    except PermissionError:
        print("Error: Permission denied")
        print("Try running with sudo or check FUSE permissions")
        cleanup_pid_file()
        return 1
    except Exception as e:
        logger.error(f"Mount failed: {e}")
        cleanup_pid_file()
        return 1

    cleanup_pid_file()
    return 0


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog='portage-pip-fuse',
        description='FUSE filesystem that bridges PyPI packages to Gentoo portage',
        epilog=f'''
Subcommands:
  mount     Mount the PyPI FUSE filesystem
  unmount   Unmount the PyPI FUSE filesystem
  install   Create /etc/portage/repos.conf entry for the overlay
  sync      Sync PyPI metadata database with latest data
  unsync    Delete the PyPI metadata database
  pip       Translate pip install commands to emerge

Examples:
  %(prog)s mount                               # Mount at default location ({REPO_LOCATION})
  %(prog)s mount /mnt/pypi                     # Mount at custom location
  %(prog)s unmount                             # Unmount from default location
  %(prog)s install                             # Create repos.conf file
  %(prog)s sync                                # Sync PyPI database
  %(prog)s unsync                              # Delete the database
  %(prog)s pip install requests                # Install via emerge
  %(prog)s pip install -r requirements.txt    # Create portage set and emerge

For subcommand help:
  %(prog)s <subcommand> --help
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        'subcommand',
        nargs='?',
        choices=['mount', 'unmount', 'install', 'sync', 'unsync', 'pip'],
        help='Subcommand to run'
    )

    parser.add_argument(
        '--version',
        action='version',
        version='%(prog)s 0.1.0'
    )

    # Parse only the first argument to determine subcommand
    # If no args or help requested, show help
    if len(sys.argv) < 2 or sys.argv[1] in ['-h', '--help']:
        parser.print_help()
        return 0

    if sys.argv[1] == '--version':
        print('portage-pip-fuse 0.1.0')
        return 0

    subcommand = sys.argv[1]

    if subcommand == 'mount':
        return mount_command()
    elif subcommand == 'unmount':
        return unmount_command()
    elif subcommand == 'install':
        return install_command()
    elif subcommand == 'sync':
        return sync_command()
    elif subcommand == 'unsync':
        return unsync_command()
    elif subcommand == 'pip':
        return pip_command()
    else:
        print(f"Unknown subcommand: {subcommand}")
        parser.print_help()
        return 1


def main_pypi():
    """
    Entry point for portage-pypi-fuse CLI.

    This is an alias for the main() function with PyPI-specific branding.
    """
    return main()


def main_rubygems():
    """
    Entry point for portage-gem-fuse CLI.

    This provides RubyGems-specific commands: mount, unmount, gem, bundle
    """
    from portage_pip_fuse.ecosystems.rubygems.cli import gem_command, bundle_command

    parser = argparse.ArgumentParser(
        prog='portage-gem-fuse',
        description='FUSE filesystem that bridges RubyGems packages to Gentoo portage',
        epilog='''
Subcommands:
  mount     Mount the RubyGems FUSE filesystem
  unmount   Unmount the filesystem
  install   Create /etc/portage/repos.conf entry for the overlay
  gem       Translate gem install commands to emerge
  bundle    Install from Gemfile.lock via emerge
  debug     Debug commands for inspecting gem metadata

Examples:
  %(prog)s mount                               # Mount at /var/db/repos/rubygems
  %(prog)s mount /mnt/rubygems                 # Mount at custom location
  %(prog)s unmount                             # Unmount
  %(prog)s gem install rails                   # emerge dev-ruby/rails
  %(prog)s bundle install                      # Install from Gemfile.lock
  %(prog)s debug versions faraday              # Show available versions
  %(prog)s debug info rails                    # Show gem metadata

For subcommand help:
  %(prog)s <subcommand> --help
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        'subcommand',
        nargs='?',
        choices=['mount', 'unmount', 'install', 'gem', 'bundle', 'debug'],
        help='Subcommand to run'
    )

    parser.add_argument(
        '--version',
        action='version',
        version='%(prog)s 0.1.0'
    )

    if len(sys.argv) < 2 or sys.argv[1] in ['-h', '--help']:
        parser.print_help()
        return 0

    if sys.argv[1] == '--version':
        print('portage-gem-fuse 0.1.0')
        return 0

    subcommand = sys.argv[1]

    if subcommand == 'mount':
        return rubygems_mount_command()
    elif subcommand == 'unmount':
        return rubygems_unmount_command()
    elif subcommand == 'install':
        return rubygems_install_command()
    elif subcommand == 'gem':
        return gem_command()
    elif subcommand == 'bundle':
        return bundle_command()
    elif subcommand == 'debug':
        return rubygems_debug_command()
    else:
        print(f"Unknown subcommand: {subcommand}")
        parser.print_help()
        return 1


def rubygems_mount_command():
    """Handle mount subcommand for RubyGems."""
    from portage_pip_fuse.plugin import PluginRegistry, ensure_plugins_discovered

    ensure_plugins_discovered()
    plugin = PluginRegistry.get('rubygems')

    if plugin is None:
        print("Error: RubyGems plugin not found")
        return 1

    mount_parser = argparse.ArgumentParser(
        prog='portage-gem-fuse mount',
        description='Mount the RubyGems FUSE filesystem',
        epilog=f'''
Examples:
  %(prog)s                                     # Mount at default location ({plugin.default_repo_location})
  %(prog)s /mnt/rubygems                       # Mount at custom location
  %(prog)s -f                                  # Mount in foreground
  %(prog)s -f -d                               # Mount with debug output

After mounting, you can:
  ls {plugin.default_repo_location}/dev-ruby/rails
  cat {plugin.default_repo_location}/dev-ruby/rails/rails-7.0.0.ebuild

To unmount:
  fusermount -u {plugin.default_repo_location}
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    mount_parser.add_argument(
        'mountpoint',
        nargs='?',
        default=plugin.default_repo_location,
        help=f'Directory where the filesystem will be mounted (default: {plugin.default_repo_location})'
    )

    mount_parser.add_argument(
        '-f', '--foreground',
        action='store_true',
        help='Run in foreground instead of daemonizing'
    )

    mount_parser.add_argument(
        '-d', '--debug',
        action='store_true',
        help='Enable debug output'
    )

    mount_parser.add_argument(
        '--cache-ttl',
        type=int,
        default=3600,
        help='Cache time-to-live in seconds (default: 3600)'
    )

    mount_parser.add_argument(
        '--cache-dir',
        type=str,
        help='Cache directory for metadata'
    )

    mount_parser.add_argument(
        '--pid-file',
        type=str,
        help='Write process ID to this file'
    )

    mount_parser.add_argument(
        '--no-git-source',
        action='store_true',
        help='Disable git repository detection'
    )

    mount_parser.add_argument(
        '--use-ruby',
        type=str,
        default='ruby32 ruby33',
        help='Space-separated list of USE_RUBY targets (default: ruby32 ruby33)'
    )

    mount_parser.add_argument(
        '--logfile',
        type=str,
        help='Log file path for debug output (default: stderr)'
    )

    mount_parser.add_argument(
        '--max-versions',
        type=int,
        default=0,
        metavar='N',
        help='Limit versions shown per package (default: 0=unlimited)'
    )

    # Available version filters for RubyGems
    # Default filters: gentoo-version, ruby-compat, platform, gem-source
    # Optional filters: pre-release (filters out alpha/beta/rc versions)
    available_filters = ['gentoo-version', 'ruby-compat', 'platform', 'gem-source', 'pre-release']

    mount_parser.add_argument(
        '--filter',
        type=str,
        action='append',
        choices=available_filters,
        metavar='FILTER',
        help=f'Enable optional filter (available: {", ".join(available_filters)}). Can be used multiple times.'
    )

    mount_parser.add_argument(
        '--no-filter',
        type=str,
        action='append',
        choices=available_filters,
        metavar='FILTER',
        help=f'Disable default filter (available: {", ".join(available_filters)}). Can be used multiple times.'
    )

    # Remove 'mount' from argv
    mount_argv = [arg for arg in sys.argv[1:] if arg != 'mount']
    args = mount_parser.parse_args(mount_argv)

    # Parse USE_RUBY
    use_ruby = args.use_ruby.split()

    # Resolve cache directory
    base_cache_dir = find_cache_dir(args.cache_dir)
    cache_dir = base_cache_dir / 'rubygems'
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Set up logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

    if args.logfile:
        logfile_path = Path(args.logfile).resolve()
        try:
            logfile_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            print(f"Error: Cannot create log directory {logfile_path.parent}")
            return 1

        logging.basicConfig(
            level=log_level,
            format=log_format,
            filename=str(logfile_path),
            filemode='a'
        )
        print(f"Logging to file: {logfile_path}")
    else:
        logging.basicConfig(level=log_level, format=log_format)

    logger = logging.getLogger(__name__)

    # Build filter configuration
    # Default filters run unless disabled with --no-filter
    # Optional filters (like pre-release) only run if enabled with --filter
    enabled_filters = set(args.filter) if args.filter else set()
    disabled_filters = set(args.no_filter) if args.no_filter else set()

    filter_config = {
        'enabled_filters': list(enabled_filters),
        'disabled_filters': list(disabled_filters),
        'include_git': not args.no_git_source,
        'max_versions': args.max_versions,
    }

    # Validate mountpoint
    check_fuse_availability()
    mountpoint = validate_mountpoint(args.mountpoint)

    # Set up signal handlers
    pid_file_path = Path(args.pid_file) if args.pid_file else None

    def cleanup_pid_file():
        if pid_file_path and pid_file_path.exists():
            try:
                pid_file_path.unlink()
            except Exception:
                pass

    def signal_handler_with_cleanup(signum, frame):
        cleanup_pid_file()
        signal_handler(signum, frame)

    signal.signal(signal.SIGINT, signal_handler_with_cleanup)
    signal.signal(signal.SIGTERM, signal_handler_with_cleanup)

    # Write PID file if requested
    if pid_file_path:
        try:
            pid_file_path.parent.mkdir(parents=True, exist_ok=True)
            pid_file_path.write_text(str(os.getpid()))
        except Exception as e:
            print(f"Error: Failed to write PID file: {e}")
            return 1

    print(f"Mounting RubyGems FUSE filesystem at {mountpoint}")
    print(f"Cache directory: {cache_dir}")
    print(f"Cache TTL: {args.cache_ttl} seconds")
    print(f"USE_RUBY: {', '.join(use_ruby)}")
    print(f"Max versions per package: {args.max_versions if args.max_versions > 0 else 'unlimited'}")

    if enabled_filters:
        print(f"Enabled filters: {', '.join(sorted(enabled_filters))}")
    if disabled_filters:
        print(f"Disabled filters: {', '.join(sorted(disabled_filters))}")

    if pid_file_path:
        print(f"PID file: {pid_file_path}")

    if args.foreground:
        print("Running in foreground (Ctrl+C to unmount)")
    else:
        print("Running in background")
        if pid_file_path:
            print(f"To unmount: portage-gem-fuse unmount --pid-file {pid_file_path}")
        else:
            print(f"To unmount: fusermount -u {mountpoint}")

    try:
        from portage_pip_fuse.ecosystems.rubygems.filesystem import mount_rubygems_filesystem

        mount_rubygems_filesystem(
            str(mountpoint),
            foreground=args.foreground,
            debug=args.debug,
            cache_ttl=args.cache_ttl,
            cache_dir=str(cache_dir),
            filter_config=filter_config,
            use_ruby=use_ruby
        )
    except KeyboardInterrupt:
        print("\nUnmounting...")
    except PermissionError:
        print("Error: Permission denied")
        print("Try running with sudo or check FUSE permissions")
        cleanup_pid_file()
        return 1
    except Exception as e:
        logger.error(f"Mount failed: {e}")
        import traceback
        traceback.print_exc()
        cleanup_pid_file()
        return 1

    cleanup_pid_file()
    return 0


def rubygems_unmount_command():
    """Handle unmount subcommand for RubyGems."""
    from portage_pip_fuse.plugin import PluginRegistry, ensure_plugins_discovered

    ensure_plugins_discovered()
    plugin = PluginRegistry.get('rubygems')

    if plugin is None:
        print("Error: RubyGems plugin not found")
        return 1

    unmount_parser = argparse.ArgumentParser(
        prog='portage-gem-fuse unmount',
        description='Unmount the RubyGems FUSE filesystem'
    )

    unmount_parser.add_argument(
        'mountpoint',
        nargs='?',
        default=plugin.default_repo_location,
        help=f'Directory where the filesystem is mounted (default: {plugin.default_repo_location})'
    )

    mount_argv = [arg for arg in sys.argv[1:] if arg != 'unmount']
    args = unmount_parser.parse_args(mount_argv)

    mountpoint = Path(args.mountpoint).resolve()

    import subprocess
    try:
        result = subprocess.run(
            ['fusermount', '-u', str(mountpoint)],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print(f"✓ Unmounted {mountpoint}")
            return 0
        else:
            if 'not mounted' in result.stderr or 'not found' in result.stderr:
                print(f"Error: {mountpoint} is not mounted")
            else:
                print(f"Error: {result.stderr.strip()}")
            return 1
    except FileNotFoundError:
        print("Error: fusermount not found. Install fuse-utils.")
        return 1


def rubygems_install_command():
    """Handle install subcommand for RubyGems - creates repos.conf file."""
    from portage_pip_fuse.plugin import PluginRegistry, ensure_plugins_discovered

    ensure_plugins_discovered()
    plugin = PluginRegistry.get('rubygems')

    if plugin is None:
        print("Error: RubyGems plugin not found")
        return 1

    install_parser = argparse.ArgumentParser(
        prog='portage-gem-fuse install',
        description='Create portage repos.conf file for the RubyGems FUSE overlay'
    )

    install_parser.add_argument(
        'mountpoint',
        nargs='?',
        default=plugin.default_repo_location,
        help=f'Directory where the filesystem will be mounted (default: {plugin.default_repo_location})'
    )

    install_parser.add_argument(
        '--priority',
        type=int,
        default=-50,
        help='Repository priority (default: -50)'
    )

    mount_argv = [arg for arg in sys.argv[1:] if arg != 'install']
    args = install_parser.parse_args(mount_argv)

    mountpoint = Path(args.mountpoint).resolve()
    repos_conf_dir = Path('/etc/portage/repos.conf')
    conf_file = repos_conf_dir / f'{plugin.repo_name}.conf'

    conf_content = f"""[{plugin.repo_name}]
location = {mountpoint}
sync-type =
auto-sync = no
priority = {args.priority}
"""

    if not repos_conf_dir.exists():
        print(f"Error: {repos_conf_dir} does not exist")
        return 1

    if conf_file.exists():
        print(f"Warning: {conf_file} already exists")
        response = input("Overwrite? [y/N]: ")
        if response.lower() not in ['y', 'yes']:
            print("Aborted")
            return 0

    try:
        conf_file.write_text(conf_content)
        print(f"Created {conf_file}")
        print(f"\nTo use the overlay:")
        print(f"  1. Mount the filesystem: portage-gem-fuse mount {mountpoint}")
        print(f"  2. Emerge packages: emerge -av dev-ruby/rails")
        return 0
    except PermissionError:
        print(f"Error: Permission denied writing to {conf_file}")
        print("Try running with sudo")
        return 1


def rubygems_debug_command():
    """Handle debug subcommand for RubyGems - inspect gem metadata."""
    import json

    parser = argparse.ArgumentParser(
        prog='portage-gem-fuse debug',
        description='Debug commands for inspecting RubyGems metadata',
        epilog='''
Debug subcommands:
  versions <gem>    Show available versions for a gem
  info <gem>        Show gem metadata (latest version)
  translate <name>  Show name translation (gem <-> gentoo)
  filter <gem>      Show which versions pass the filters
  deps <gem>        Show dependencies for a gem

Examples:
  %(prog)s versions faraday          # List all versions
  %(prog)s info rails                # Show rails metadata
  %(prog)s translate iso-639         # Show name translation
  %(prog)s filter nokogiri           # Show filtered versions
  %(prog)s deps rails                # Show dependencies
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        'debug_command',
        choices=['versions', 'info', 'translate', 'filter', 'deps'],
        help='Debug command to run'
    )

    parser.add_argument(
        'name',
        help='Gem name or package name to inspect'
    )

    parser.add_argument(
        '--version', '-v',
        type=str,
        help='Specific version (for info/deps commands)'
    )

    parser.add_argument(
        '--json',
        action='store_true',
        help='Output as JSON'
    )

    parser.add_argument(
        '--use-ruby',
        type=str,
        default='ruby32 ruby33',
        help='USE_RUBY targets for filter command (default: ruby32 ruby33)'
    )

    # Parse args (skip 'debug' from argv)
    debug_argv = [arg for arg in sys.argv[1:] if arg != 'debug']

    if not debug_argv:
        parser.print_help()
        return 0

    args = parser.parse_args(debug_argv)

    # Import required modules
    from portage_pip_fuse.ecosystems.rubygems.plugin import RubyGemsMetadataProvider
    from portage_pip_fuse.ecosystems.rubygems.name_translator import create_rubygems_translator
    from portage_pip_fuse.ecosystems.rubygems.filters import (
        RubyCompatFilter,
        PlatformFilter,
        GemSourceFilter,
        VersionFilterChain,
    )

    provider = RubyGemsMetadataProvider()
    translator = create_rubygems_translator()

    if args.debug_command == 'versions':
        return _debug_versions(provider, args.name, args.json)
    elif args.debug_command == 'info':
        return _debug_info(provider, args.name, args.version, args.json)
    elif args.debug_command == 'translate':
        return _debug_translate(translator, args.name, args.json)
    elif args.debug_command == 'filter':
        use_ruby = args.use_ruby.split()
        return _debug_filter(provider, args.name, use_ruby, args.json)
    elif args.debug_command == 'deps':
        return _debug_deps(provider, translator, args.name, args.version, args.json)

    return 0


def _debug_versions(provider, gem_name, as_json):
    """Show available versions for a gem."""
    import json
    from packaging.version import Version

    versions = provider.get_package_versions(gem_name)

    if not versions:
        print(f"No versions found for '{gem_name}'")
        return 1

    # Sort semantically, handling invalid versions gracefully
    def version_key(v):
        try:
            # Valid versions get (1, Version) - higher priority in descending sort
            return (1, Version(v))
        except Exception:
            # Invalid versions get (0, string) - appear at end in descending sort
            return (0, v)

    versions = sorted(versions, key=version_key, reverse=True)

    if as_json:
        print(json.dumps({'gem': gem_name, 'versions': versions}, indent=2))
    else:
        print(f"Versions for {gem_name} ({len(versions)} total):")
        print()
        # Show in columns
        cols = 5
        for i in range(0, len(versions), cols):
            row = versions[i:i+cols]
            print("  " + "  ".join(f"{v:15}" for v in row))

    return 0


def _debug_info(provider, gem_name, version, as_json):
    """Show gem metadata."""
    import json

    if version:
        info = provider.get_version_info(gem_name, version)
    else:
        info = provider.get_package_info(gem_name)

    if not info:
        print(f"No info found for '{gem_name}'")
        return 1

    if as_json:
        print(json.dumps(info, indent=2, default=str))
    else:
        print(f"Gem: {info.get('name', gem_name)}")
        print(f"Version: {info.get('version', info.get('number', 'latest'))}")
        print(f"Platform: {info.get('platform', 'ruby')}")
        print(f"Authors: {info.get('authors', 'N/A')}")
        print(f"License: {info.get('licenses', 'N/A')}")
        print(f"Homepage: {info.get('homepage_uri', info.get('project_uri', 'N/A'))}")
        print(f"Ruby Version: {info.get('required_ruby_version', info.get('ruby_version', 'any'))}")
        print()
        print(f"Summary: {info.get('summary', info.get('info', 'N/A')[:200])}")
        print()

        # Show dependencies if available
        deps = info.get('dependencies', {})
        if deps:
            runtime_deps = deps.get('runtime', [])
            dev_deps = deps.get('development', [])

            if runtime_deps:
                print(f"Runtime dependencies ({len(runtime_deps)}):")
                for dep in runtime_deps[:10]:
                    print(f"  {dep.get('name')}: {dep.get('requirements', '>= 0')}")
                if len(runtime_deps) > 10:
                    print(f"  ... and {len(runtime_deps) - 10} more")

            if dev_deps:
                print(f"\nDevelopment dependencies ({len(dev_deps)}):")
                for dep in dev_deps[:5]:
                    print(f"  {dep.get('name')}: {dep.get('requirements', '>= 0')}")
                if len(dev_deps) > 5:
                    print(f"  ... and {len(dev_deps) - 5} more")

    return 0


def _debug_translate(translator, name, as_json):
    """Show name translation both directions."""
    import json

    gentoo_name = translator.rubygems_to_gentoo(name)
    gem_name = translator.gentoo_to_rubygems(name)

    if as_json:
        print(json.dumps({
            'input': name,
            'as_gem_to_gentoo': gentoo_name,
            'as_gentoo_to_gem': gem_name,
        }, indent=2))
    else:
        print(f"Input: {name}")
        print()
        print(f"As gem name -> Gentoo:  {name} -> {gentoo_name}")
        print(f"As Gentoo -> gem name:  {name} -> {gem_name}")
        print()
        print(f"Gentoo atom: dev-ruby/{gentoo_name}")
        print(f"RubyGems URL: https://rubygems.org/gems/{gem_name}")

    return 0


def _debug_filter(provider, gem_name, use_ruby, as_json):
    """Show which versions pass the filters."""
    import json
    from packaging.version import Version
    from portage_pip_fuse.ecosystems.rubygems.filters import (
        RubyCompatFilter,
        PlatformFilter,
        GemSourceFilter,
        VersionFilterChain,
    )

    # Get all versions metadata
    versions_data = provider.get_versions_metadata(gem_name)

    if not versions_data:
        print(f"No versions found for '{gem_name}'")
        return 1

    # Build versions dict
    all_versions = {}
    for v in versions_data:
        if isinstance(v, dict):
            ver = v.get('number', '')
            if ver:
                all_versions[ver] = v

    # Create filter chain (default filters, pre-release is opt-in)
    filters = [
        RubyCompatFilter(use_ruby=use_ruby),
        PlatformFilter(),
        GemSourceFilter(include_git=True),
    ]
    filter_chain = VersionFilterChain(filters)

    # Apply filters
    filtered = filter_chain.filter_versions(gem_name, all_versions)

    # Sort versions (handle invalid versions gracefully)
    def version_key(v):
        try:
            return (1, Version(v))
        except Exception:
            return (0, v)

    all_sorted = sorted(all_versions.keys(), key=version_key, reverse=True)
    filtered_sorted = sorted(filtered.keys(), key=version_key, reverse=True)

    if as_json:
        print(json.dumps({
            'gem': gem_name,
            'total_versions': len(all_versions),
            'filtered_versions': len(filtered),
            'use_ruby': use_ruby,
            'filters': filter_chain.get_description(),
            'passed': filtered_sorted,
            'rejected': [v for v in all_sorted if v not in filtered],
        }, indent=2))
    else:
        print(f"Filter results for {gem_name}")
        print(f"USE_RUBY: {', '.join(use_ruby)}")
        print(f"Filters: {filter_chain.get_description()}")
        print()
        print(f"Total versions: {len(all_versions)}")
        print(f"Passed filter: {len(filtered)}")
        print()

        if filtered_sorted:
            print("Versions that pass (newest first):")
            cols = 5
            for i in range(0, min(len(filtered_sorted), 20), cols):
                row = filtered_sorted[i:i+cols]
                print("  " + "  ".join(f"{v:15}" for v in row))
            if len(filtered_sorted) > 20:
                print(f"  ... and {len(filtered_sorted) - 20} more")
        else:
            print("No versions pass the filters!")
            print()
            # Show why some versions were rejected
            print("Sample rejected versions:")
            for v in all_sorted[:5]:
                meta = all_versions[v]
                reasons = []
                if meta.get('prerelease'):
                    reasons.append('pre-release')
                platform = meta.get('platform', 'ruby')
                if platform not in ('ruby', None, ''):
                    reasons.append(f'platform={platform}')
                ruby_req = meta.get('required_ruby_version') or meta.get('ruby_version')
                if ruby_req:
                    reasons.append(f'ruby={ruby_req}')
                print(f"  {v}: {', '.join(reasons) if reasons else 'unknown reason'}")

    return 0


def _debug_deps(provider, translator, gem_name, version, as_json):
    """Show dependencies for a gem."""
    import json

    if version:
        info = provider.get_version_info(gem_name, version)
    else:
        info = provider.get_package_info(gem_name)

    if not info:
        print(f"No info found for '{gem_name}'")
        return 1

    deps = info.get('dependencies', {})
    runtime_deps = deps.get('runtime', [])

    if as_json:
        # Add Gentoo translations
        for dep in runtime_deps:
            dep['gentoo_name'] = translator.rubygems_to_gentoo(dep.get('name', ''))
        print(json.dumps({
            'gem': gem_name,
            'version': version or info.get('version', 'latest'),
            'runtime_dependencies': runtime_deps,
        }, indent=2))
    else:
        ver = version or info.get('version', 'latest')
        print(f"Dependencies for {gem_name}-{ver}")
        print()

        if not runtime_deps:
            print("No runtime dependencies")
            return 0

        print(f"Runtime dependencies ({len(runtime_deps)}):")
        print()
        print(f"  {'Gem Name':<25} {'Constraint':<20} {'Gentoo Name':<25}")
        print(f"  {'-'*25} {'-'*20} {'-'*25}")

        for dep in runtime_deps:
            dep_name = dep.get('name', '')
            constraint = dep.get('requirements', '>= 0')
            gentoo_name = translator.rubygems_to_gentoo(dep_name)
            print(f"  {dep_name:<25} {constraint:<20} {gentoo_name:<25}")

    return 0


if __name__ == "__main__":
    sys.exit(main())