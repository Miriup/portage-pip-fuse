# Coding Style Guide for portage-fuse

This document outlines the coding style and development practices for the portage-fuse project (formerly portage-pip-fuse).

## Multi-Ecosystem Architecture

This project supports multiple package ecosystems through a plugin architecture:

| Ecosystem | CLI Command | Default Category | Overlay Path |
|-----------|-------------|------------------|--------------|
| PyPI | `portage-pypi-fuse` | `dev-python` | `/var/db/repos/pypi` |
| RubyGems | `portage-gem-fuse` | `dev-ruby` | `/var/db/repos/rubygems` |

The legacy `portage-pip-fuse` command remains available for backwards compatibility.

## Project Goals and Non-Goals

### Primary Goals
- Provide FUSE filesystem interface for package ecosystems (PyPI, RubyGems) to Gentoo portage
- **Command translation**: Translate `pip install` / `gem install` / `bundle install` to `emerge` commands
- Filter packages by interpreter compatibility (PYTHON_TARGETS / USE_RUBY)
- Filter packages to those with source distributions available
- Support dependency resolution for specific packages with USE flags
- Maintain high performance with comprehensive caching

### Non-Goals  
- **Curated package lists**: The curated filter is NOT intended for production use. We need automatic filtering based on system compatibility, not manual curation.
- **Supporting all PyPI packages**: Only packages compatible with system Python and having source distributions should be visible.

## Key Design Decisions

### Default Filters
The following filters MUST work properly by default:
1. **source-dist**: Filters packages to those with source distributions OR git repositories (required for Gentoo's build-from-source philosophy)
   - Prefers sdist when available
   - Falls back to git-r3 when package has a git repository URL but no sdist
   - Falls back to wheel only as last resort
2. **python-compat**: MUST be implemented efficiently to filter packages by system PYTHON_TARGETS
   - Current implementation needs work - checking all 746k packages is impractical
   - Should work on cached/indexed packages or use a more efficient approach
   - This is ESSENTIAL for manageable package counts

### Python Compatibility Enforcement
Python compatibility is enforced at TWO levels:
1. **Pre-filtering**: The python-compat filter reduces visible packages to compatible ones
2. **Ebuild level**: PYTHON_COMPAT generation ensures accurate compatibility declarations

### Bug Fixing Philosophy
- **Never disable features to "fix" bugs** - properly implement them instead
- **Filters are essential** - the python-compat filter is critical because PyPI has too many packages to handle without filtering
- **Performance matters** - filters must be efficient enough to be practical
- **Default behavior must be sensible** - showing incompatible packages wastes resources and confuses users

## General Principles

1. **Use Original Code When Possible**: Whenever possible, utilize original pip or portage code from the reference repositories instead of reimplementing functionality.

2. **Documentation First**: All modules, classes, and methods should have comprehensive documentation including doctests.

3. **Extensibility**: Design interfaces that allow for future extensions (e.g., caching, preloading).

## Code Style

### Python Version

- Target Python 3.8+ (minimum version available in Gentoo main repository)
- Use type hints for all function signatures
- Avoid features only available in newer Python versions

### Formatting

- **Line Length**: Maximum 100 characters (configured in pyproject.toml)
- **Indentation**: 4 spaces (no tabs)
- **String Quotes**: Use double quotes for docstrings, single quotes for other strings
- **Imports**: Group in order: standard library, third-party, local imports

### Tools

```bash
# Format code
black portage_pip_fuse --line-length 100

# Sort imports
isort portage_pip_fuse --profile black --line-length 100

# Type checking
mypy portage_pip_fuse

# Linting
flake8 portage_pip_fuse --max-line-length 100
```

## Documentation Standards

### Module Documentation

Every module should start with:
```python
"""
Brief description of the module.

Longer description explaining the purpose, design decisions,
and any important context.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""
```

### Function/Method Documentation

All functions and methods must have docstrings with:
- Brief description
- Args section (if applicable)
- Returns section (if applicable)
- Raises section (if applicable)
- Examples section with doctests

Example:
```python
def translate_name(self, name: str) -> str:
    """
    Brief description of what the function does.
    
    Longer explanation if needed.
    
    Args:
        name: Description of the parameter
        
    Returns:
        Description of the return value
        
    Raises:
        ValueError: When and why this is raised
        
    Examples:
        >>> obj = MyClass()
        >>> obj.translate_name("example")
        'result'
    """
```

### Doctests

- All public methods should include doctests in their docstrings
- Doctests should cover typical use cases and edge cases
- Use realistic examples from actual PyPI/Gentoo packages
- Run doctests with: `python -m doctest portage_pip_fuse/*.py -v`

## Design Patterns

### Abstract Base Classes

Use ABC for interfaces that will have multiple implementations:

```python
from abc import ABC, abstractmethod

class TranslatorBase(ABC):
    @abstractmethod
    def translate(self, name: str) -> str:
        pass
```

### Caching and Performance

- Design interfaces that allow caching to be added later
- Use simple implementations first, optimize when needed
- Document performance characteristics in docstrings

### Error Handling

- Use strict_mode parameter for toggling between exceptions and best-effort
- Provide clear error messages that help users understand the problem
- Document all exceptions that can be raised

## Testing

### Unit Tests

- Test files go in `tests/` directory
- Name test files as `test_*.py`
- Use pytest for testing framework
- Include both positive and negative test cases

### Test Coverage

- Aim for high test coverage (>80%)
- Test edge cases and error conditions
- Use real-world data when possible (e.g., actual PyPI package names)

### Integration Tests

- Test with actual pip and portage infrastructure when available
- Use the reference repositories for validation
- Document any external dependencies needed for tests

## Import Strategy

### From Reference Repositories

When using code from reference implementations:

```python
# Try to import from pip's vendored libraries first
try:
    from pip._vendor.packaging.utils import canonicalize_name
except ImportError:
    # Fallback to our own implementation
    def canonicalize_name(name: str) -> str:
        # Implementation based on the standard
        pass
```

### Conditional Imports

Always provide fallbacks for optional dependencies:

```python
try:
    import optional_module
    HAS_OPTIONAL = True
except ImportError:
    HAS_OPTIONAL = False
```

## Commit Messages

Follow conventional commit format:

```
type: brief description

Longer explanation of the change, why it was needed,
and any important context.

- Bullet points for specific changes
- Another change
```

Types: feat, fix, docs, style, refactor, test, chore

## File Headers

All Python files should include copyright and license information:

```python
"""
Module description.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""
```

## Naming Conventions

### Variables and Functions
- Use snake_case for variables and functions
- Be descriptive but concise
- Avoid abbreviations unless widely understood

### Classes
- Use PascalCase for class names
- Suffix with Base for abstract classes
- Suffix with Mixin for mixin classes

### Constants
- Use UPPER_SNAKE_CASE for module-level constants
- Group related constants together

### Private Members
- Prefix with single underscore for internal use
- Double underscore only when name mangling is specifically needed

## Package Organization

```
portage_pip_fuse/
├── __init__.py          # Package initialization, version info
├── plugin.py            # Plugin infrastructure (EcosystemPlugin, PluginRegistry)
├── name_translator.py   # Name translation logic
├── filesystem.py        # FUSE filesystem implementation
├── cli.py              # Command-line interface (mount, pip, gem, bundle, sync, etc.)
├── pip_metadata.py      # PyPI metadata extraction and ebuild data
├── sqlite_metadata.py   # SQLite backend for PyPI database
├── hybrid_metadata.py   # SQLite + JSON API fallback
├── version_filter.py    # Version filtering (source-dist, python-compat)
├── package_filter.py    # Package filtering (deps, recent, etc.)
├── constants.py         # Configuration constants
├── prefetcher.py        # Repository scanning utilities
├── source_provider.py   # Source provider abstraction (sdist, git, wheel)
├── git_provider.py      # Git URL detection and normalization
├── git_source_patch.py  # Git source configuration patches
└── ecosystems/          # Ecosystem-specific implementations
    ├── __init__.py
    ├── pypi/            # PyPI ecosystem plugin
    │   ├── __init__.py
    │   └── plugin.py    # PyPIPlugin, PyPIMetadataProvider, PyPIEbuildGenerator
    └── rubygems/        # RubyGems ecosystem plugin
        ├── __init__.py
        ├── plugin.py         # RubyGemsPlugin, metadata provider, ebuild generator
        ├── name_translator.py # Gem to Gentoo name translation
        ├── source_provider.py # GemSourceProvider, RubyGitProvider
        ├── filters.py        # RubyCompatFilter, PlatformFilter, PreReleaseFilter
        ├── cli.py            # gem_command(), bundle_command()
        └── gemfile_parser.py # Gemfile.lock parsing
```

## CLI Architecture

The CLI (`cli.py`) uses argparse with subcommand dispatch. Three entry points are available:

### PyPI CLI (portage-pypi-fuse / portage-pip-fuse)

```bash
# Main subcommands
portage-pypi-fuse mount      # Mount FUSE filesystem for PyPI
portage-pypi-fuse unmount    # Unmount filesystem
portage-pypi-fuse install    # Create repos.conf
portage-pypi-fuse sync       # Sync SQLite database
portage-pypi-fuse unsync     # Delete database
portage-pypi-fuse pip        # pip install translation
```

### RubyGems CLI (portage-gem-fuse)

```bash
# Main subcommands
portage-gem-fuse mount       # Mount FUSE filesystem for RubyGems
portage-gem-fuse unmount     # Unmount filesystem
portage-gem-fuse install     # Create repos.conf
portage-gem-fuse gem         # gem install translation
portage-gem-fuse bundle      # bundle install translation (from Gemfile.lock)
portage-gem-fuse debug       # Debug commands for inspecting gem metadata
```

### debug Subcommand

The `debug` subcommand provides tools for inspecting RubyGems metadata and troubleshooting:

```bash
# Show available versions for a gem (semantically sorted)
portage-gem-fuse debug versions <gem>
portage-gem-fuse debug versions faraday

# Show gem metadata (latest or specific version)
portage-gem-fuse debug info <gem>
portage-gem-fuse debug info rails --version 7.0.0

# Show name translation (gem <-> Gentoo)
portage-gem-fuse debug translate <name>
portage-gem-fuse debug translate iso-639

# Show which versions pass the filters (ruby-compat, platform, pre-release, source)
portage-gem-fuse debug filter <gem>
portage-gem-fuse debug filter nokogiri --use-ruby "ruby32 ruby33"

# Show dependencies with Gentoo name translation
portage-gem-fuse debug deps <gem>
portage-gem-fuse debug deps rails

# JSON output for scripting
portage-gem-fuse debug info rails --json
portage-gem-fuse debug versions faraday --json
```

### pip Subcommand Implementation

The `pip` subcommand (`pip_command()`) translates pip install syntax to emerge:

**Key helper functions:**
- `_translate_pypi_version()`: Convert PEP 440 versions to Gentoo format (a1→_alpha1, rc→_rc, .post→_p)
- `_format_gentoo_atom()`: Create Gentoo atoms from package name + specifier
- `_parse_requirements_file()`: Parse requirements.txt with full pip compatibility
- `_derive_set_name()`: Generate portage set name from requirements file path

**Flow for `-r requirements.txt`:**
1. Parse requirements file using pip's packaging library
2. Translate each requirement to Gentoo atom format
3. Create `/etc/portage/sets/{project}-dependencies` file
4. Execute `emerge @{project}-dependencies`

**Version specifier mapping:**
| PyPI Operator | Gentoo |
|---------------|--------|
| `>=` | `>=pkg-ver` |
| `==` | `=pkg-ver` |
| `~=` | `>=pkg-ver` + `<pkg-next` |
| `!=` | `!=pkg-ver` |
| `==X.*` | `=pkg-X*` |

**Extras handling:**
Package extras (`requests[security]`) are reported as USE flag requirements that users should add to `/etc/portage/package.use`.

## Git-based Source Support

When a PyPI package has no source distribution (sdist) but has a known git repository URL, the system can generate ebuilds using `git-r3.eclass` instead.

### Source Provider Priority

The system uses a provider chain with the following priority order:
1. **sdist (priority 100)**: Source distribution - preferred, uses `pypi.eclass`
2. **git (priority 75)**: Git repository - fallback, uses `git-r3.eclass`
3. **wheel (priority 50)**: Pure-Python wheel - last resort, extracts wheel archive

### Git URL Detection

Git repository URLs are extracted from PyPI `project_urls` metadata in this order:
1. `Repository` key
2. `Source` / `Source Code` key
3. `GitHub` / `GitLab` key
4. `Homepage` (only if it matches a known git host)

**Supported git hosts:** GitHub, GitLab, Codeberg, Bitbucket, SourceHut, GitLab instances (gnome.org, freedesktop.org)

### Git URL Normalization

URLs are normalized for use with `git-r3.eclass`:
- `/tree/main`, `/blob/main/file`, `/-/tree/main` suffixes are removed
- SSH URLs (`git@github.com:user/repo`) are converted to HTTPS
- `.git` suffix is added if missing (for GitHub/GitLab)

### Git-based Ebuild Structure

```bash
EAPI=8

DISTUTILS_USE_PEP517=setuptools
PYTHON_COMPAT=( python3_11 python3_12 python3_13 )

EGIT_REPO_URI="https://github.com/user/repo.git"
EGIT_COMMIT="v${PV}"

inherit distutils-r1 git-r3

DESCRIPTION="Package description"
HOMEPAGE="https://pypi.org/project/example"

LICENSE="MIT"
SLOT="0"
# Live ebuild from git - no keywords
KEYWORDS=""
```

### CLI Flags

```bash
portage-pip-fuse mount --no-git-source        # Disable git repository detection
portage-pip-fuse mount --git-tag-pattern STR  # Default tag pattern (default: v${PV})
```

### Manual Git Source Configuration

Use the `.sys/git-source/` virtual filesystem to manually configure git sources:

```bash
# Enable git source for all versions of a package
echo "== git" > /var/db/repos/pypi/.sys/git-source/dev-python/faster-whisper/_all

# Enable with custom URL
echo "== git https://github.com/custom/repo.git" > /var/db/repos/pypi/.sys/git-source/dev-python/faster-whisper/_all

# Enable with custom URL and tag pattern
echo "== git https://github.com/custom/repo.git release-{version}" > /var/db/repos/pypi/.sys/git-source/dev-python/faster-whisper/_all

# Force wheel fallback (disable git for this package)
echo "== wheel" > /var/db/repos/pypi/.sys/git-source/dev-python/some-package/_all
```

### Version Filter Integration

The `source-dist` version filter includes packages with git repositories by default:

```python
# Include both sdist and git sources (default)
filter = VersionFilterSourceDist(include_git=True)

# Only include packages with sdist
filter = VersionFilterSourceDist(include_git=False)
```

## Plugin System

The plugin architecture enables support for multiple package ecosystems while sharing ~70% of the codebase (FUSE mechanics, caching, CLI infrastructure).

### Core Abstractions

```python
from abc import ABC, abstractmethod

class EcosystemPlugin(ABC):
    """Base class for ecosystem plugins."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Ecosystem name (e.g., 'pypi', 'rubygems')."""
        pass

    @property
    @abstractmethod
    def default_category(self) -> str:
        """Default Gentoo category (e.g., 'dev-python', 'dev-ruby')."""
        pass

    @abstractmethod
    def get_metadata_provider(self, cache_dir, cache_ttl) -> MetadataProviderBase:
        """Get the metadata provider for this ecosystem."""
        pass

    @abstractmethod
    def get_ebuild_generator(self, **kwargs) -> EbuildGeneratorBase:
        """Get the ebuild generator for this ecosystem."""
        pass
```

### Plugin Discovery

Plugins are automatically discovered from the `ecosystems/` package:

```python
from portage_pip_fuse.plugin import PluginRegistry

# Get all registered plugins
plugins = PluginRegistry.get_all_plugins()  # {'pypi': PyPIPlugin, 'rubygems': RubyGemsPlugin}

# Get a specific plugin
pypi = PluginRegistry.get_plugin('pypi')
rubygems = PluginRegistry.get_plugin('rubygems')
```

### Creating a New Ecosystem Plugin

1. Create a new directory under `ecosystems/`:
   ```
   ecosystems/neweco/
   ├── __init__.py
   └── plugin.py
   ```

2. Implement the plugin class:
   ```python
   from portage_pip_fuse.plugin import EcosystemPlugin

   class NewEcoPlugin(EcosystemPlugin):
       @property
       def name(self) -> str:
           return "neweco"
       # ... implement other abstract methods
   ```

3. Register the plugin in `__init__.py`:
   ```python
   from .plugin import NewEcoPlugin
   ```

## RubyGems Ecosystem Support

The RubyGems ecosystem provides FUSE overlay support for Ruby packages from RubyGems.org.

### Key Differences from PyPI

| Aspect | RubyGems | PyPI |
|--------|----------|------|
| Compatibility variable | `USE_RUBY="ruby32 ruby33"` | `PYTHON_COMPAT=(python3_{11,12})` |
| Primary eclass | `ruby-fakegem` | `distutils-r1` |
| Dependency helpers | `ruby_add_rdepend "dep"` | `RDEPEND="dep[${PYTHON_USEDEP}]"` |
| Source format | `.gem` archive | sdist / wheel |
| Version constraint | `~> 2.1` (pessimistic) | `~= 2.1` (compatible) |

### RubyGems Ebuild Template

```bash
EAPI=8

USE_RUBY="ruby32 ruby33"
RUBY_FAKEGEM_RECIPE_TEST="none"
RUBY_FAKEGEM_RECIPE_DOC="none"

inherit ruby-fakegem

DESCRIPTION="Package description"
HOMEPAGE="https://rubygems.org/gems/example"
SRC_URI="https://rubygems.org/gems/${PN}-${PV}.gem"

LICENSE="MIT"
SLOT="0"
KEYWORDS="~amd64 ~arm64"

ruby_add_rdepend ">=dev-ruby/rails-7.0 <dev-ruby/rails-8"
```

### Version Constraint Translation

Ruby's pessimistic constraint operator (`~>`) is translated to Gentoo atoms:

| Ruby Constraint | Gentoo Atoms |
|-----------------|--------------|
| `~> 2.1.3` | `>=dev-ruby/pkg-2.1.3 <dev-ruby/pkg-2.2` |
| `~> 2.1` | `>=dev-ruby/pkg-2.1 <dev-ruby/pkg-3` |
| `>= 1.0, < 2.0` | `>=dev-ruby/pkg-1.0 <dev-ruby/pkg-2.0` |
| `= 1.0.0` | `=dev-ruby/pkg-1.0.0` |
| `!= 1.5.0` | `!=dev-ruby/pkg-1.5.0` |

### gem Command

Translates `gem install` to `emerge`:

```bash
# Basic install
portage-gem-fuse gem install rails
# Translates to: emerge dev-ruby/rails

# With version
portage-gem-fuse gem install rails -v 7.0.0
# Translates to: emerge =dev-ruby/rails-7.0.0

# Dry run
portage-gem-fuse gem install --dry-run rails nokogiri
# Shows: Would run: emerge --ask dev-ruby/rails dev-ruby/nokogiri
```

### bundle Command

Parses Gemfile.lock and installs all dependencies:

```bash
# From project directory with Gemfile.lock
cd ~/src/myproject
portage-gem-fuse bundle install

# Creates: /etc/portage/sets/myproject-gems
# Runs: emerge @myproject-gems

# Generate virtual ebuild instead
portage-gem-fuse bundle install --deps-overlay /var/db/repos/rubygems
# Creates: /var/db/repos/rubygems/virtual/myproject/myproject-0.ebuild
```

### Gemfile.lock Parser

The parser handles all standard Gemfile.lock sections:

```python
from portage_pip_fuse.ecosystems.rubygems.gemfile_parser import (
    parse_gemfile_lock,
    parse_gemfile_lock_full,
    GemDependency,
    GemfileLockData,
)

# Simple interface - list of gems
gems = parse_gemfile_lock('/path/to/Gemfile.lock')
for gem in gems:
    print(f"{gem.name} {gem.version} ({gem.source_type})")

# Full data including platforms, direct dependencies, etc.
data = parse_gemfile_lock_full('/path/to/Gemfile.lock')
print(f"Direct dependencies: {data.direct_dependencies}")
print(f"Platforms: {data.platforms}")
print(f"Bundler version: {data.bundled_with}")
```

Supported sections:
- **GEM**: Gems from RubyGems.org
- **GIT**: Gems from git repositories
- **PATH**: Local gems
- **PLATFORMS**: Target platforms
- **DEPENDENCIES**: Direct dependencies
- **RUBY VERSION**: Ruby version constraint
- **BUNDLED WITH**: Bundler version

### RubyGems Name Translation

Gem names are used **exactly as specified** in RubyGems, with minimal transformations for PMS compatibility:

1. **Underscores preserved**: Valid per PMS 3.1.2, distinguishes different gems:
   - `devise-secure_password` → `dev-ruby/devise-secure_password`
   - `devise-secure-password` → `dev-ruby/devise-secure-password`

2. **Trailing digits fixed**: Names ending in `-NUMBER` conflict with version parsing.
   Hyphen is replaced with underscore to avoid collisions with gems that already
   have no hyphen (e.g., `http-2` vs `http2` are different gems):
   - `iso-639` → `dev-ruby/iso_639`
   - `http-2` → `dev-ruby/http_2` (distinct from `http2` → `dev-ruby/http2`)

3. **No heuristic matching**: Unlike previous versions, we do NOT try to match `ruby-foo` to `foo` or vice versa. Each gem gets its own package name.

```python
from portage_pip_fuse.ecosystems.rubygems.name_translator import (
    create_rubygems_translator
)

translator = create_rubygems_translator()
translator.rubygems_to_gentoo('activerecord')          # 'activerecord'
translator.rubygems_to_gentoo('ruby-debug')            # 'ruby-debug' (NOT 'debug')
translator.rubygems_to_gentoo('debug')                 # 'debug'
translator.rubygems_to_gentoo('iso-639')               # 'iso_639' (trailing digits fixed)
translator.rubygems_to_gentoo('http-2')                # 'http_2' (distinct from 'http2')
```

**Handling mismatches**: When a gem name differs from an existing Gentoo package (e.g., Gentoo has `dev-ruby/foo` but the gem is `ruby-foo`), use the `.sys` patching mechanism to configure dependency mappings, similar to PyPI's approach with `sci-libs/torch`.

Known mappings for Rails ecosystem gems (like `active_support` → `activesupport`) are built-in. Additional mappings are extracted from Gentoo's `metadata.xml` files.

### RubyGems Filters

Available filters for gem version selection:

| Filter | Description |
|--------|-------------|
| `ruby-compat` | Filters by `required_ruby_version` against system USE_RUBY |
| `gem-source` | Filters to gems with `.gem` files or git repositories |
| `platform` | Excludes platform-specific gems (java, mswin, darwin) |
| `pre-release` | Excludes alpha/beta/rc versions |

```python
from portage_pip_fuse.ecosystems.rubygems.filters import (
    RubyCompatFilter,
    PlatformFilter,
    PreReleaseFilter,
    VersionFilterChain,
)

# Create filter chain
filters = VersionFilterChain([
    RubyCompatFilter(use_ruby=['ruby32', 'ruby33']),
    PlatformFilter(),
    PreReleaseFilter(include_pre=False),
])

# Filter versions
filtered = filters.filter_versions('rails', versions_metadata)
```

## Performance Considerations

- Profile before optimizing
- Document algorithmic complexity in docstrings
- Consider memory usage for large-scale operations
- Use generators for large datasets when possible

## Security

- Never log or expose sensitive information
- Validate all user input
- Use safe file operations
- Follow principle of least privilege

## Compatibility

- Maintain compatibility with both GPL-2.0 (portage) and MIT (pip) code
- Document any compatibility constraints
- Test on Gentoo Linux systems
- Ensure FUSE compatibility across kernel versions