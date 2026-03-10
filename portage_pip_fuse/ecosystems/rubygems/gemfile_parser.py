"""
Gemfile.lock parser for portage-gem-fuse.

This module parses Gemfile.lock files to extract gem dependencies
with their exact resolved versions.

Gemfile.lock format:
- GEM section: gems from rubygems.org
- GIT section: gems from git repositories
- PATH section: local gems
- PLATFORMS section: target platforms
- DEPENDENCIES section: direct dependencies
- BUNDLED WITH section: bundler version

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class GemDependency:
    """
    Represents a gem dependency from Gemfile.lock.

    Attributes:
        name: Gem name
        version: Resolved version (exact)
        source_type: Source type ('gem', 'git', 'path')
        source_uri: Source URI (RubyGems.org, git URL, or local path)
        git_ref: Git reference (branch, tag, or commit)
        platform: Target platform (e.g., 'ruby', 'java')
        dependencies: List of direct dependencies (name only)
    """
    name: str
    version: Optional[str] = None
    source_type: str = 'gem'
    source_uri: Optional[str] = None
    git_ref: Optional[str] = None
    platform: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)


@dataclass
class GemfileLockData:
    """
    Complete parsed Gemfile.lock data.

    Attributes:
        gems: Dict of gem name -> GemDependency
        direct_dependencies: Set of direct dependency names (from DEPENDENCIES section)
        platforms: List of target platforms
        ruby_version: Ruby version constraint (if any)
        bundled_with: Bundler version used
        git_sources: Dict of git remote -> list of gems
    """
    gems: Dict[str, GemDependency] = field(default_factory=dict)
    direct_dependencies: Set[str] = field(default_factory=set)
    platforms: List[str] = field(default_factory=list)
    ruby_version: Optional[str] = None
    bundled_with: Optional[str] = None
    git_sources: Dict[str, List[str]] = field(default_factory=dict)


def parse_gemfile_lock(path: str) -> List[GemDependency]:
    """
    Parse a Gemfile.lock file and return list of gem dependencies.

    This is the primary interface for extracting dependencies.

    Args:
        path: Path to Gemfile.lock file

    Returns:
        List of GemDependency objects

    Examples:
        >>> gems = parse_gemfile_lock('/path/to/Gemfile.lock')
        >>> len(gems) > 0
        True
        >>> gems[0].name
        'rails'
    """
    data = parse_gemfile_lock_full(path)
    return list(data.gems.values())


def parse_gemfile_lock_full(path: str) -> GemfileLockData:
    """
    Parse a Gemfile.lock file and return complete data.

    Args:
        path: Path to Gemfile.lock file

    Returns:
        GemfileLockData with all parsed information
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        logger.error(f"Gemfile.lock not found: {path}")
        return GemfileLockData()
    except IOError as e:
        logger.error(f"Error reading Gemfile.lock: {e}")
        return GemfileLockData()

    return _parse_content(content)


def _parse_content(content: str) -> GemfileLockData:
    """
    Parse Gemfile.lock content.

    Args:
        content: Raw file content

    Returns:
        Parsed GemfileLockData
    """
    data = GemfileLockData()
    lines = content.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]

        if line == 'GEM':
            i = _parse_gem_section(lines, i + 1, data)
        elif line == 'GIT':
            i = _parse_git_section(lines, i + 1, data)
        elif line == 'PATH':
            i = _parse_path_section(lines, i + 1, data)
        elif line == 'PLATFORMS':
            i = _parse_platforms_section(lines, i + 1, data)
        elif line == 'DEPENDENCIES':
            i = _parse_dependencies_section(lines, i + 1, data)
        elif line == 'RUBY VERSION':
            i = _parse_ruby_version_section(lines, i + 1, data)
        elif line == 'BUNDLED WITH':
            i = _parse_bundled_with_section(lines, i + 1, data)
        else:
            i += 1

    return data


def _parse_gem_section(lines: List[str], start: int, data: GemfileLockData) -> int:
    """
    Parse GEM section.

    Format:
    GEM
      remote: https://rubygems.org/
      specs:
        actioncable (7.1.0)
          actionpack (= 7.1.0)
          activesupport (= 7.1.0)
        ...
    """
    i = start
    remote = 'https://rubygems.org/'
    in_specs = False

    while i < len(lines):
        line = lines[i]

        # Check for next section (no leading whitespace)
        if line and not line[0].isspace():
            break

        stripped = line.strip()

        if stripped.startswith('remote:'):
            remote = stripped[7:].strip()
        elif stripped == 'specs:':
            in_specs = True
        elif in_specs and stripped:
            # Parse gem entry
            gem = _parse_gem_entry(lines, i, remote)
            if gem:
                data.gems[gem.name] = gem
                # Skip to next gem (after dependencies)
                i = _skip_gem_dependencies(lines, i)
                continue

        i += 1

    return i


def _parse_gem_entry(lines: List[str], index: int, source_uri: str) -> Optional[GemDependency]:
    """
    Parse a single gem entry line.

    Format: "    gemname (version)" or "    gemname (version-platform)"
    """
    line = lines[index].strip()

    # Match gem name and version
    match = re.match(r'^([a-zA-Z0-9_-]+)\s+\(([^)]+)\)$', line)
    if not match:
        return None

    name = match.group(1)
    version_str = match.group(2)

    # Handle platform suffix (e.g., "1.0.0-java", "1.18.9-x86_64-linux-gnu")
    platform = None
    version = version_str

    # Platform suffixes can be complex like x86_64-linux-gnu, arm64-darwin, etc.
    # They typically start with an architecture or simple platform name
    platform_patterns = [
        # Architecture-based platforms
        r'-x86_64-.*$',
        r'-x86-.*$',
        r'-arm64-.*$',
        r'-aarch64-.*$',
        r'-i686-.*$',
        r'-universal-.*$',
        # Simple platforms
        r'-(ruby|java|jruby|mswin|mswin32|mswin64|mingw|mingw32|x64-mingw32|x64-mingw-ucrt|darwin)$',
    ]

    for pattern in platform_patterns:
        match = re.search(pattern, version_str)
        if match:
            platform = match.group(0)[1:]  # Remove leading hyphen
            version = version_str[:match.start()]
            break

    # Parse dependencies (indented lines after this)
    dependencies = []
    i = index + 1
    while i < len(lines):
        dep_line = lines[i]
        # Check if still in dependency list (6+ spaces indent)
        if not dep_line.startswith('      ') or not dep_line.strip():
            break
        # Extract dependency name (before version constraint)
        dep_match = re.match(r'^\s+([a-zA-Z0-9_-]+)', dep_line)
        if dep_match:
            dependencies.append(dep_match.group(1))
        i += 1

    return GemDependency(
        name=name,
        version=version,
        source_type='gem',
        source_uri=source_uri,
        platform=platform,
        dependencies=dependencies
    )


def _skip_gem_dependencies(lines: List[str], index: int) -> int:
    """Skip past a gem's dependency lines."""
    i = index + 1
    while i < len(lines):
        line = lines[i]
        # Dependencies are indented with 6+ spaces
        if not line.startswith('      ') or not line.strip():
            # Check if next line is a new gem (4 spaces)
            if line.startswith('    ') and line.strip():
                return i
            # Might be end of section
            if not line.startswith('  '):
                return i
        i += 1
    return i


def _parse_git_section(lines: List[str], start: int, data: GemfileLockData) -> int:
    """
    Parse GIT section.

    Format:
    GIT
      remote: https://github.com/user/repo.git
      revision: abc123
      branch: main
      specs:
        gemname (version)
    """
    i = start
    remote = None
    revision = None
    branch = None
    tag = None
    in_specs = False

    while i < len(lines):
        line = lines[i]

        if line and not line[0].isspace():
            break

        stripped = line.strip()

        if stripped.startswith('remote:'):
            remote = stripped[7:].strip()
        elif stripped.startswith('revision:'):
            revision = stripped[9:].strip()
        elif stripped.startswith('branch:'):
            branch = stripped[7:].strip()
        elif stripped.startswith('tag:'):
            tag = stripped[4:].strip()
        elif stripped == 'specs:':
            in_specs = True
        elif in_specs and stripped:
            # Parse gem entry
            match = re.match(r'^([a-zA-Z0-9_-]+)\s+\(([^)]+)\)', stripped)
            if match:
                name = match.group(1)
                version = match.group(2)

                gem = GemDependency(
                    name=name,
                    version=version,
                    source_type='git',
                    source_uri=remote,
                    git_ref=tag or branch or revision
                )
                data.gems[name] = gem

                # Track git sources
                if remote:
                    if remote not in data.git_sources:
                        data.git_sources[remote] = []
                    data.git_sources[remote].append(name)

        i += 1

    return i


def _parse_path_section(lines: List[str], start: int, data: GemfileLockData) -> int:
    """
    Parse PATH section.

    Format:
    PATH
      remote: .
      specs:
        myapp (0.1.0)
    """
    i = start
    remote = '.'
    in_specs = False

    while i < len(lines):
        line = lines[i]

        if line and not line[0].isspace():
            break

        stripped = line.strip()

        if stripped.startswith('remote:'):
            remote = stripped[7:].strip()
        elif stripped == 'specs:':
            in_specs = True
        elif in_specs and stripped:
            match = re.match(r'^([a-zA-Z0-9_-]+)\s+\(([^)]+)\)', stripped)
            if match:
                name = match.group(1)
                version = match.group(2)

                gem = GemDependency(
                    name=name,
                    version=version,
                    source_type='path',
                    source_uri=remote
                )
                data.gems[name] = gem

        i += 1

    return i


def _parse_platforms_section(lines: List[str], start: int, data: GemfileLockData) -> int:
    """
    Parse PLATFORMS section.

    Format:
    PLATFORMS
      ruby
      x86_64-linux
    """
    i = start

    while i < len(lines):
        line = lines[i]

        if line and not line[0].isspace():
            break

        stripped = line.strip()
        if stripped:
            data.platforms.append(stripped)

        i += 1

    return i


def _parse_dependencies_section(lines: List[str], start: int, data: GemfileLockData) -> int:
    """
    Parse DEPENDENCIES section.

    Format:
    DEPENDENCIES
      rails (~> 7.0)
      pg
      puma (~> 6.0)
    """
    i = start

    while i < len(lines):
        line = lines[i]

        if line and not line[0].isspace():
            break

        stripped = line.strip()
        if stripped:
            # Extract gem name (before version constraint or !)
            match = re.match(r'^([a-zA-Z0-9_-]+)', stripped)
            if match:
                data.direct_dependencies.add(match.group(1))

        i += 1

    return i


def _parse_ruby_version_section(lines: List[str], start: int, data: GemfileLockData) -> int:
    """
    Parse RUBY VERSION section.

    Format:
    RUBY VERSION
       ruby 3.2.0p0
    """
    i = start

    while i < len(lines):
        line = lines[i]

        if line and not line[0].isspace():
            break

        stripped = line.strip()
        if stripped:
            data.ruby_version = stripped

        i += 1

    return i


def _parse_bundled_with_section(lines: List[str], start: int, data: GemfileLockData) -> int:
    """
    Parse BUNDLED WITH section.

    Format:
    BUNDLED WITH
       2.4.0
    """
    i = start

    while i < len(lines):
        line = lines[i]

        if line and not line[0].isspace():
            break

        stripped = line.strip()
        if stripped:
            data.bundled_with = stripped

        i += 1

    return i


def filter_runtime_gems(data: GemfileLockData) -> List[GemDependency]:
    """
    Filter to only runtime dependencies (exclude development/test gems).

    This uses the DEPENDENCIES section to determine which gems are
    direct dependencies, then recursively includes their dependencies.

    Args:
        data: Parsed Gemfile.lock data

    Returns:
        List of runtime gem dependencies
    """
    # Start with direct dependencies
    to_include: Set[str] = set(data.direct_dependencies)
    included: Set[str] = set()

    # Recursively add dependencies
    while to_include:
        name = to_include.pop()
        if name in included:
            continue

        included.add(name)

        gem = data.gems.get(name)
        if gem:
            for dep in gem.dependencies:
                if dep not in included:
                    to_include.add(dep)

    # Return gems in included set
    return [data.gems[name] for name in included if name in data.gems]


def filter_platform_gems(
    gems: List[GemDependency],
    platforms: Optional[List[str]] = None
) -> List[GemDependency]:
    """
    Filter gems by platform compatibility.

    Args:
        gems: List of gems to filter
        platforms: Target platforms (default: ['ruby'])

    Returns:
        Filtered list of platform-compatible gems
    """
    if platforms is None:
        platforms = ['ruby', None]  # None means no platform (universal)

    compatible_platforms = {'ruby', '', None}
    if 'linux' in str(platforms):
        compatible_platforms.add('linux')

    filtered = []
    for gem in gems:
        if gem.platform in compatible_platforms:
            filtered.append(gem)

    return filtered
