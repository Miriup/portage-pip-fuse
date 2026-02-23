# Package Filtering System

## Overview

The portage-pip-fuse filesystem includes a sophisticated filtering system that controls which PyPI packages are visible in the `/dev-python/` directory. This is essential because PyPI contains over 746,000 packages, making it impractical to list them all in a single directory.

## Why Filtering?

1. **Performance**: Listing 746k+ packages causes significant delays and timeouts
2. **Relevance**: Most users only need a small subset of packages
3. **Dependency Management**: When installing a specific package, you only need its dependencies
4. **System Resources**: Reduces memory usage and API calls

## Available Filters

### 1. Curated Filter (Default)
**Command**: `--filter=curated`

Shows a manually curated list of popular Python packages including web frameworks, data science tools, testing frameworks, and development utilities.

```bash
python -m portage_pip_fuse.cli /var/db/repos/pypi --filter=curated
```

**Packages included**:
- Web frameworks: Django, Flask, FastAPI, Tornado, etc.
- Data science: NumPy, Pandas, SciPy, Matplotlib, etc.
- ML/AI: TensorFlow, PyTorch, Transformers, OpenAI, etc.
- Testing: pytest, tox, coverage, hypothesis, etc.
- Development tools: black, flake8, mypy, ruff, etc.

### 1.1. Source Distribution Filter (Default)
**Command**: `--filter=source-dist` (enabled by default)

Shows only packages that have source distributions (`.tar.gz` files) available on PyPI. This filter excludes wheel-only packages that don't provide source code, which aligns with Gentoo's build-from-source philosophy.

```bash
# Explicitly enable (though it's default)
python -m portage_pip_fuse.cli /var/db/repos/pypi --filter=source-dist

# Disable to show all packages including wheel-only
python -m portage_pip_fuse.cli /var/db/repos/pypi --no-filter=source-dist
```

**Why it's important**:
- Ensures compatibility with Gentoo's source-based package management
- Excludes proprietary or pre-compiled packages that can't be built from source
- Reduces the package list to only those suitable for ebuild generation
- Improves performance by filtering out packages that would fail during emerge

### 2. Dependency Tree Filter
**Command**: `--filter=deps --deps-for=PACKAGE [--use-flags=EXTRAS]`

Shows only packages in the dependency tree of specified root packages. This is the most practical filter for actual installations.

```bash
# Show all dependencies for requests
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=deps --deps-for=requests

# Show dependencies for multiple packages
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=deps --deps-for=django --deps-for=celery

# Include optional dependencies (extras/USE flags)
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=deps --deps-for=requests --use-flags=socks,security
```

**How it works**:
1. Fetches package metadata from PyPI JSON API
2. Recursively resolves all dependencies
3. Evaluates marker conditions for conditional dependencies
4. Handles Python extras as Gentoo USE flags
5. Detects and breaks circular dependencies

### 3. Recent Packages Filter
**Command**: `--filter=recent [--filter-days=N]`

Shows packages updated within the last N days (default: 30) using PyPI's RSS feed.

```bash
# Show packages updated in last 7 days
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=recent --filter-days=7
```

**Limitations**: RSS feed provides maximum 100 recent updates

### 4. Newest Packages Filter
**Command**: `--filter=newest [--filter-count=N]`

Shows the N most recently created packages (default: 100) using PyPI's RSS feed.

```bash
# Show 50 newest packages
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=newest --filter-count=50
```

### 5. All Packages Filter
**Command**: `--filter=all`

Shows ALL packages from PyPI (746,000+). 

⚠️ **WARNING**: This is extremely slow and not recommended for production use!

```bash
python -m portage_pip_fuse.cli /var/db/repos/pypi --filter=all
```

## Default Filter System

By default, the filesystem enables multiple filters to provide a balanced set of packages:

**Default filters**: `curated` and `source-dist`

This means that without any filter arguments, you get:
- Popular, well-known Python packages (curated filter)
- Only packages with source code available (source-dist filter)

### Customizing Active Filters

You can customize which filters are active using `--filter` and `--no-filter`:

```bash
# Add dependency resolution to defaults
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=deps --deps-for=django

# Disable source-dist filter to include wheel-only packages
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --no-filter=source-dist

# Only use dependency filter (disable all defaults)
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --no-filter=curated --no-filter=source-dist \
    --filter=deps --deps-for=requests

# Combine multiple filters
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=recent --filter-days=7 \
    --filter=deps --deps-for=fastapi
```

### Filter Combining Logic

When multiple filters are active, packages must pass **ALL** active filters (AND logic). For example:

```bash
# Show packages that are BOTH in curated list AND have source distributions
python -m portage_pip_fuse.cli /var/db/repos/pypi  # (defaults)

# Show packages that are dependencies of django AND have source distributions
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=deps --deps-for=django  # (source-dist still default)
```

This ensures that the resulting package list meets all specified criteria.

## Performance Options

### Timestamp Lookup Control

By default, the filesystem fetches actual PyPI upload timestamps for all files and directories, providing accurate modification times. However, this can slow down operations when you need fast access.

#### Disable Timestamps for Speed
**Command**: `--no-timestamps`

Disables PyPI timestamp lookup and uses current time for all files. This significantly improves performance for `ls`, `stat`, and directory traversal operations.

```bash
# Fast mode - no timestamp lookups
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=curated --no-timestamps

# With any filter for maximum speed
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=recent --filter-days=365 --no-timestamps
```

#### Performance Comparison

| Operation | With Timestamps | Without Timestamps |
|-----------|----------------|-------------------|
| `ls /dev-python/` | 2-5 seconds | <1 second |
| `ls -l /dev-python/requests/` | 3-8 seconds | <1 second |
| `find /dev-python -mtime -30` | Very slow/timeout | Fast |
| Directory traversal | API rate limited | No network calls |

#### When to Use Each Mode

**Use `--no-timestamps` when:**
- You need fast directory listings
- Performing bulk operations
- Don't need accurate file modification times
- Working with large package sets
- Bandwidth or API rate limits are a concern

**Use normal mode (with timestamps) when:**
- You need accurate PyPI upload dates
- Using time-based filtering tools
- Analyzing package release history
- Want realistic file modification times for tools

### Cache Configuration

```bash
# Increase cache TTL for less frequent API calls
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --cache-ttl 7200  # 2 hours instead of default 1 hour

# Use persistent cache directory
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --cache-dir ~/.cache/portage-pip-fuse
```

## Technical Implementation

### Filter Architecture

```python
FilterBase (Abstract)
├── FilterCurated         # Hardcoded list of popular packages (default)
├── FilterSourceDistribution  # Only packages with source code (default)
├── FilterDependencyTree  # Recursive dependency resolution
├── FilterRecent          # RSS-based recent updates
├── FilterNewest          # RSS-based new packages
├── FilterAll             # Complete PyPI index
└── FilterChain           # Combines multiple filters
```

### Dependency Resolution Algorithm

The `FilterDependencyTree` class implements sophisticated dependency resolution:

```python
def _resolve_package_dependencies(package_name, depth=0, visited=None):
    1. Check for cycles and depth limits
    2. Fetch package metadata from PyPI
    3. Parse requires_dist entries
    4. For each dependency:
       a. Parse requirement specification
       b. Evaluate marker conditions
       c. Check if extras match USE flags
       d. Recursively resolve if applicable
    5. Return complete dependency set
```

### Marker Evaluation

Python dependencies can have conditions (markers) that control when they apply:

```python
# Example markers from package metadata
"PySocks!=1.5.7,>=1.5.6; extra == 'socks'"
"cryptography>=1.3.4; extra == 'security'"
"typing-extensions>=4.0; python_version < '3.10'"
```

The filter evaluates these conditions:
- `extra == 'name'`: Matched against provided USE flags
- `python_version`: Would need Python version context
- `platform_system`: Would need system information

### USE Flags Mapping

Python "extras" map directly to Gentoo USE flags:

| PyPI Extra | Gentoo USE Flag | Example |
|------------|-----------------|---------|
| `[socks]` | `socks` | `requests[socks]` → `USE="socks"` |
| `[security]` | `security` | `requests[security]` → `USE="security"` |
| `[all]` | `all` | `fastapi[all]` → `USE="all"` |

## Performance Considerations

### Caching Strategy

1. **Memory Cache**: Package metadata cached in `_resolution_cache`
2. **Disk Cache**: PyPI responses cached in `cache_dir`
3. **TTL**: Configurable cache time-to-live (default: 1 hour)

### Optimization Opportunities

Current implementation resolves dependencies synchronously during directory listing, which can be slow for complex packages. Potential improvements:

1. **Pre-resolution**: Resolve dependencies at mount time
2. **Parallel Fetching**: Use asyncio for concurrent API calls
3. **Incremental Loading**: Load dependencies on-demand
4. **Persistent Cache**: Store resolved dependency trees

## Usage Examples

### Installing a Web Application

```bash
# Mount with Django dependencies
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=deps --deps-for=django --use-flags=argon2,bcrypt

# Now only Django and its dependencies are visible
ls /var/db/repos/pypi/dev-python/
# Shows: django, asgiref, sqlparse, pytz, etc.

# Install Django
emerge dev-python/django
```

### Data Science Environment

```bash
# Mount with common data science packages
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=deps \
    --deps-for=pandas \
    --deps-for=matplotlib \
    --deps-for=scikit-learn

# All scientific computing dependencies are available
emerge dev-python/pandas dev-python/matplotlib
```

### Exploring New Packages

```bash
# See what's new on PyPI
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=newest --filter-count=20

# Or recent updates (fast mode)
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=recent --filter-days=3 --no-timestamps
```

### High-Performance Bulk Operations

```bash
# Fast directory traversal for automation (uses defaults: curated + source-dist)
python -m portage_pip_fuse.cli /var/db/repos/pypi --no-timestamps

# Quick dependency resolution without timestamp overhead
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=deps --deps-for=django --no-timestamps

# Maximum performance for large package sets (disable source-dist for more packages)
python -m portage_pip_fuse.cli /var/db/repos/pypi \
    --filter=all --no-filter=source-dist --no-timestamps --cache-ttl 7200
```

## Configuration File Support

While not yet implemented, the filter system is designed to support configuration files:

```yaml
# /etc/portage-pip-fuse/filters.yaml
default_filter: deps
dependency_packages:
  - requests
  - django
  - pytest
use_flags:
  - socks
  - security
cache_dir: /var/cache/portage-pip-fuse
```

## Troubleshooting

### Slow Directory Listing

If `ls /var/db/repos/pypi/dev-python/` is slow:

1. **Use `--no-timestamps` for immediate speed improvement**:
   ```bash
   # Fastest option - disable timestamp lookups
   python -m portage_pip_fuse.cli /var/db/repos/pypi --no-timestamps
   ```

2. Check the log file for dependency resolution progress
3. Consider using a simpler filter (curated or recent)
4. Pre-cache package metadata:
   ```bash
   # Pre-fetch and cache metadata
   python -c "
   from portage_pip_fuse.package_filter import FilterDependencyTree
   filter = FilterDependencyTree(['package-name'])
   filter.get_packages()  # Pre-resolves and caches
   "
   ```

### Missing Dependencies

If expected dependencies don't appear:

1. Check if package uses markers with unsupported conditions
2. Verify USE flags are correctly specified
3. Check log file for resolution errors
4. Try increasing max_depth if deep dependency tree

### Missing Packages (Wheel-Only)

If expected packages don't appear and you suspect they're wheel-only:

1. **Check if source-dist filter is active** (it's enabled by default):
   ```bash
   # Disable source-dist filter to see wheel-only packages
   python -m portage_pip_fuse.cli /var/db/repos/pypi --no-filter=source-dist
   ```

2. **Verify package has source distribution**:
   ```bash
   # Check PyPI simple index manually
   curl -s https://pypi.org/simple/package-name/ | grep '\.tar\.gz'
   ```

3. **For debugging, check what's being filtered**:
   ```bash
   # Enable debug logging to see filter decisions
   python -m portage_pip_fuse.cli /var/db/repos/pypi \
       --debug --logfile=filter-debug.log --no-filter=source-dist
   ```

### Memory Usage

For large dependency trees:

1. Use persistent cache directory with `--cache-dir`
2. Consider breaking into multiple smaller filters
3. Monitor memory usage with `top` or `htop`

## API Reference

### FilterBase Abstract Methods

```python
class FilterBase(ABC):
    @abstractmethod
    def get_packages(self) -> Set[str]:
        """Return set of PyPI package names that pass this filter."""
        
    @abstractmethod
    def get_description(self) -> str:
        """Get human-readable description of this filter."""
```

### Filter Initialization

```python
# Dependency tree with USE flags
filter = FilterDependencyTree(
    root_packages=['django', 'celery'],
    use_flags=['redis', 'msgpack'],
    cache_dir=Path('/tmp/cache'),
    max_depth=10
)

# Recent packages
filter = FilterRecent(days=7)

# Newest packages
filter = FilterNewest(count=50)

# Source distribution filter
filter = FilterSourceDistribution(cache_dir=Path('/tmp/cache'))

# Combine filters
filter = FilterChain(
    filters=[filter1, filter2],
    operator='OR',  # or 'AND'
    max_results=1000
)
```

## Future Enhancements

1. **Async Resolution**: Use aiohttp for parallel dependency fetching
2. **Incremental Loading**: Load dependencies as directories are accessed
3. **Filter Persistence**: Save and reload filter results
4. **Smart Caching**: Track PyPI package update times
5. **Filter Profiles**: Pre-defined filter sets for common use cases
6. **Interactive Mode**: CLI tool to build and test filters
7. **Dependency Graph Export**: Generate visualizations of dependency trees