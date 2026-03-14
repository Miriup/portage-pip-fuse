"""
CLI commands for RubyGems integration.

This module provides the 'gem' and 'bundle' subcommands for
translating gem/bundle commands to emerge commands.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any

from portage_pip_fuse.ecosystems.rubygems.name_translator import (
    create_rubygems_translator
)
from portage_pip_fuse.ecosystems.rubygems.gemfile_parser import (
    parse_gemfile_lock,
    GemDependency,
)


# Lazy-initialized translator
_translator = None


def _get_translator():
    """Get the name translator, initializing on first use."""
    global _translator
    if _translator is None:
        _translator = create_rubygems_translator()
    return _translator


def gem_to_gentoo(gem_name: str) -> str:
    """Translate gem name to Gentoo package name."""
    return _get_translator().rubygems_to_gentoo(gem_name)


def _translate_gem_version(gem_version: str) -> str:
    """
    Translate gem version string to Gentoo format.

    Converts Ruby pre-release markers:
    - .alpha -> _alpha
    - .beta -> _beta
    - .pre -> _pre
    - .rc -> _rc
    """
    version = gem_version

    # Replace pre-release markers
    version = re.sub(r'\.alpha(\d*)', r'_alpha\1', version)
    version = re.sub(r'\.beta(\d*)', r'_beta\1', version)
    version = re.sub(r'\.pre(\d*)', r'_pre\1', version)
    version = re.sub(r'\.rc(\d*)', r'_rc\1', version)

    return version


def _format_gentoo_atom(gem_name: str, version_constraint: Optional[str] = None) -> str:
    """
    Format a Gentoo dependency atom from a gem name and optional constraint.

    Args:
        gem_name: RubyGems gem name
        version_constraint: Optional version constraint (e.g., "~> 2.0", ">= 1.0")

    Returns:
        Formatted Gentoo atom (e.g., ">=dev-ruby/rails-7.0.0")
    """
    gentoo_name = gem_to_gentoo(gem_name)

    if not version_constraint:
        return f"dev-ruby/{gentoo_name}"

    # Parse constraint
    # Gem constraints: ~> 2.1, >= 1.0, = 1.0, != 1.0, < 2.0, > 1.0

    # Split multiple constraints (e.g., ">= 1.0, < 2.0")
    constraints = [c.strip() for c in version_constraint.split(',')]
    atoms = []

    for constraint in constraints:
        match = re.match(r'^([~><=!]+)\s*(\d+(?:\.\d+)*(?:\.[a-zA-Z0-9]+)?)$', constraint.strip())
        if not match:
            continue

        op, version = match.groups()
        gentoo_version = _translate_gem_version(version)

        if op == '~>':
            # Pessimistic constraint
            parts = version.split('.')
            if len(parts) >= 2:
                upper_parts = parts[:-1]
                try:
                    upper_parts[-1] = str(int(upper_parts[-1]) + 1)
                    upper_version = '.'.join(upper_parts)
                    atoms.append(f">=dev-ruby/{gentoo_name}-{gentoo_version}")
                    atoms.append(f"<dev-ruby/{gentoo_name}-{upper_version}")
                except ValueError:
                    atoms.append(f">=dev-ruby/{gentoo_name}-{gentoo_version}")
            else:
                atoms.append(f">=dev-ruby/{gentoo_name}-{gentoo_version}")
        elif op == '>=':
            atoms.append(f">=dev-ruby/{gentoo_name}-{gentoo_version}")
        elif op == '>':
            atoms.append(f">dev-ruby/{gentoo_name}-{gentoo_version}")
        elif op == '<=':
            atoms.append(f"<=dev-ruby/{gentoo_name}-{gentoo_version}")
        elif op == '<':
            atoms.append(f"<dev-ruby/{gentoo_name}-{gentoo_version}")
        elif op == '=' or op == '==':
            atoms.append(f"=dev-ruby/{gentoo_name}-{gentoo_version}")
        elif op == '!=':
            atoms.append(f"!=dev-ruby/{gentoo_name}-{gentoo_version}")

    if not atoms:
        return f"dev-ruby/{gentoo_name}"

    if len(atoms) == 1:
        return atoms[0]

    # Return first atom for simplicity (emerge handles multiple separately)
    return atoms[0]


def _get_project_name(directory: Path) -> Optional[str]:
    """
    Detect project name from directory.

    Looks for:
    - *.gemspec file
    - config/application.rb (Rails)
    - Gemfile
    """
    # Check for gemspec
    gemspecs = list(directory.glob('*.gemspec'))
    if gemspecs:
        return gemspecs[0].stem

    # Check for Rails application
    app_rb = directory / 'config' / 'application.rb'
    if app_rb.exists():
        content = app_rb.read_text()
        match = re.search(r'module\s+(\w+)', content)
        if match:
            # Convert CamelCase to snake_case
            name = match.group(1)
            name = re.sub(r'([A-Z])', r'-\1', name).lower().strip('-')
            return name

    # Use directory name
    return directory.name


def _generate_virtual_ebuild(
    project_name: str,
    gems: List[GemDependency],
    ruby_version: Optional[str] = None
) -> str:
    """
    Generate a virtual ebuild for project dependencies.

    Args:
        project_name: Name of the project
        gems: List of gem dependencies
        ruby_version: Optional Ruby version constraint

    Returns:
        Complete ebuild content
    """
    # Determine USE_RUBY dynamically from eclass
    from .ruby_targets import get_all_ruby_impls
    use_ruby = ' '.join(get_all_ruby_impls())

    # Build RDEPEND (skip platform-specific gems - Gentoo builds from source)
    rdepend_lines = []
    seen_gems = set()  # Avoid duplicates when same gem appears with/without platform
    for gem in gems:
        # Skip platform-specific gems
        if gem.platform:
            continue

        # Skip duplicates
        if gem.name in seen_gems:
            continue
        seen_gems.add(gem.name)

        gentoo_name = gem_to_gentoo(gem.name)
        if gem.version:
            version = _translate_gem_version(gem.version)
            rdepend_lines.append(f"\t=dev-ruby/{gentoo_name}-{version}")
        else:
            rdepend_lines.append(f"\tdev-ruby/{gentoo_name}")

    rdepend = '\n'.join(rdepend_lines)

    return f'''# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

USE_RUBY="{use_ruby}"

inherit ruby-ng

DESCRIPTION="Virtual package for {project_name} gem dependencies"
HOMEPAGE=""
SRC_URI=""
S="${{WORKDIR}}"

LICENSE="metapackage"
SLOT="0"
KEYWORDS="~amd64 ~arm64"

RDEPEND="
{rdepend}
"

ruby_add_rdepend "${{RDEPEND}}"

src_unpack() {{
\teinfo "This is a metapackage that only pulls in dependencies for {project_name}."
\teinfo "It does not install any files itself."
}}
'''


def gem_command():
    """
    Handle gem subcommand - translates gem install to emerge.

    Supports:
    - gem install package1 package2 ...
    - gem install package -v VERSION
    """
    parser = argparse.ArgumentParser(
        prog='portage-gem-fuse gem',
        description='Translate gem install commands to emerge commands',
        usage='portage-gem-fuse gem install [options] [gems...]',
        epilog='''
Examples:
  %(prog)s install rails                    # Translate to: emerge dev-ruby/rails
  %(prog)s install rails -v 7.0.0           # emerge =dev-ruby/rails-7.0.0
  %(prog)s install --dry-run rails nokogiri # Show emerge command
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        'subcommand',
        nargs='?',
        choices=['install'],
        default='install',
        help='gem subcommand (currently only install is supported)'
    )

    parser.add_argument(
        'gems',
        nargs='*',
        help='Gems to install'
    )

    parser.add_argument(
        '-v', '--version',
        type=str,
        help='Specific version to install'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without executing'
    )

    parser.add_argument(
        '--pretend',
        action='store_true',
        help='Pass --pretend to emerge'
    )

    parser.add_argument(
        '--ask',
        action='store_true',
        default=True,
        help='Pass --ask to emerge (default: True)'
    )

    parser.add_argument(
        '--no-ask',
        action='store_true',
        help='Do not ask for confirmation'
    )

    # Parse args (removing 'gem' from argv)
    gem_argv = []
    skip_next = False
    for arg in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg == 'gem':
            continue
        gem_argv.append(arg)

    args = parser.parse_args(gem_argv)

    if args.subcommand != 'install':
        print(f"Error: Only 'gem install' is currently supported")
        return 1

    if not args.gems:
        print("Error: No gems specified")
        parser.print_help()
        return 1

    # Build emerge command
    emerge_cmd = ['emerge']

    if args.ask and not args.no_ask:
        emerge_cmd.append('--ask')

    if args.pretend:
        emerge_cmd.append('--pretend')

    # Add gems
    for gem in args.gems:
        if gem == 'install':
            continue

        if args.version:
            version = _translate_gem_version(args.version)
            gentoo_name = gem_to_gentoo(gem)
            emerge_cmd.append(f"=dev-ruby/{gentoo_name}-{version}")
        else:
            gentoo_name = gem_to_gentoo(gem)
            emerge_cmd.append(f"dev-ruby/{gentoo_name}")

    # Show or execute
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


def bundle_command():
    """
    Handle bundle subcommand - translates Gemfile.lock to emerge.

    Supports:
    - bundle install (from current directory)
    - bundle install --gemfile PATH
    """
    parser = argparse.ArgumentParser(
        prog='portage-gem-fuse bundle',
        description='Install gems from Gemfile.lock via emerge',
        usage='portage-gem-fuse bundle install [options]',
        epilog='''
Examples:
  %(prog)s install                         # Install from ./Gemfile.lock
  %(prog)s install --gemfile /path/to/Gemfile.lock
  %(prog)s install --dry-run               # Show what would be done
  %(prog)s install --deps-overlay /var/db/repos/rubygems  # Create virtual ebuild
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        'subcommand',
        nargs='?',
        choices=['install'],
        default='install',
        help='bundle subcommand (currently only install is supported)'
    )

    parser.add_argument(
        '--gemfile',
        type=str,
        help='Path to Gemfile.lock (default: ./Gemfile.lock)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without executing'
    )

    parser.add_argument(
        '--pretend',
        action='store_true',
        help='Pass --pretend to emerge'
    )

    parser.add_argument(
        '--ask',
        action='store_true',
        default=True,
        help='Pass --ask to emerge (default: True)'
    )

    parser.add_argument(
        '--no-ask',
        action='store_true',
        help='Do not ask for confirmation'
    )

    parser.add_argument(
        '--deps-overlay',
        type=str,
        metavar='PATH',
        help='Generate a virtual ebuild in the specified overlay instead of using set file'
    )

    parser.add_argument(
        '--set-dir',
        type=str,
        default='/etc/portage/sets',
        help='Directory for portage set files (default: /etc/portage/sets)'
    )

    # Parse args
    bundle_argv = []
    for arg in sys.argv[1:]:
        if arg == 'bundle':
            continue
        bundle_argv.append(arg)

    args = parser.parse_args(bundle_argv)

    if args.subcommand != 'install':
        print(f"Error: Only 'bundle install' is currently supported")
        return 1

    # Find Gemfile.lock
    if args.gemfile:
        gemfile_lock = Path(args.gemfile)
    else:
        gemfile_lock = Path.cwd() / 'Gemfile.lock'

    if not gemfile_lock.exists():
        print(f"Error: Gemfile.lock not found at {gemfile_lock}")
        print("Run 'bundle lock' first to generate Gemfile.lock")
        return 1

    # Parse Gemfile.lock
    print(f"Parsing {gemfile_lock}...")
    gems = parse_gemfile_lock(str(gemfile_lock))

    if not gems:
        print("Error: No gems found in Gemfile.lock")
        return 1

    print(f"Found {len(gems)} gems")

    # Get project name
    project_dir = gemfile_lock.parent
    project_name = _get_project_name(project_dir)
    if not project_name:
        project_name = 'bundle'

    # Handle --deps-overlay mode
    if args.deps_overlay:
        overlay_path = Path(args.deps_overlay)
        gentoo_name = gem_to_gentoo(project_name)
        ebuild_dir = overlay_path / 'virtual' / gentoo_name
        ebuild_path = ebuild_dir / f'{gentoo_name}-0.ebuild'

        ebuild_content = _generate_virtual_ebuild(project_name, gems)

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
                print(f"  emerge -av =virtual/{gentoo_name}-0")
            except PermissionError:
                print(f"Error: Permission denied writing {ebuild_path}")
                print("Try running with sudo or check overlay permissions")
                return 1

        return 0

    # Create portage set file
    set_dir = Path(args.set_dir)
    set_name = f"{project_name}-gems"
    set_path = set_dir / set_name

    # Generate set content (skip platform-specific gems - Gentoo builds from source)
    set_lines = [
        f"# Generated from {gemfile_lock}",
        f"# by portage-gem-fuse bundle install",
        ""
    ]

    seen_gems = set()  # Avoid duplicates
    for gem in gems:
        # Skip platform-specific gems
        if gem.platform:
            continue

        # Skip duplicates
        if gem.name in seen_gems:
            continue
        seen_gems.add(gem.name)

        gentoo_name = gem_to_gentoo(gem.name)
        if gem.version:
            version = _translate_gem_version(gem.version)
            set_lines.append(f"=dev-ruby/{gentoo_name}-{version}")
        else:
            set_lines.append(f"dev-ruby/{gentoo_name}")

    set_content = '\n'.join(set_lines) + '\n'

    if args.dry_run:
        print(f"\n--- Would create {set_path} ---")
        print(set_content)
        print(f"--- End {set_path} ---\n")
    else:
        try:
            set_dir.mkdir(parents=True, exist_ok=True)
            set_path.write_text(set_content)
            print(f"Created portage set: {set_path}")
        except PermissionError:
            print(f"Error: Permission denied writing {set_path}")
            print("Try running with sudo")
            return 1

    # Build emerge command
    emerge_cmd = ['emerge']

    if args.ask and not args.no_ask:
        emerge_cmd.append('--ask')

    if args.pretend:
        emerge_cmd.append('--pretend')

    emerge_cmd.append(f'@{set_name}')

    # Show or execute
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
