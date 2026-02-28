# PyPI Metadata Caching Architecture

This document explains how portage-pip-fuse caches PyPI metadata to provide fast, efficient access while minimizing load on PyPI servers.

## Overview

The system uses a two-level caching architecture that ensures metadata is fetched from PyPI only once per hour per package, with subsequent accesses being nearly instantaneous.

## Two-Level Cache Design

### Level 1: In-Memory Cache (Fastest)

- **Storage**: Python dictionary `self._memory_cache`
- **Key format**: `package_name` or `package_name_version`
- **TTL**: 3600 seconds (1 hour) by default
- **Scope**: Shared within a single `PyPIMetadataExtractor` instance
- **Access time**: ~0.000001 seconds

### Level 2: Disk Cache (Persistent)

- **Location**: `~/.cache/portage-pip-fuse/` or `/tmp/portage-pip-fuse-cache/`
- **File format**: JSON files named `{package_name}.json` or `{package_name}_{version}.json`
- **TTL**: 3600 seconds (1 hour) - validated via file modification time
- **Persistence**: Survives process restarts
- **Organization**: Subdirectories based on first 2 characters to avoid filesystem limitations
- **Access time**: ~0.001 seconds

## Request Flow

When any part of the code requests PyPI metadata:

```
Request for package metadata
    ↓
Check in-memory cache
    ├─ Hit (< 1 hour old) → Return immediately (~0.000001s)
    └─ Miss → Check disk cache
        ├─ Hit (< 1 hour old) → Load + populate memory cache (~0.001s)
        └─ Miss → Fetch from PyPI
            ├─ Success → Save to disk + memory cache → Return
            └─ 404 → Cache negative result (avoid repeated lookups)
```

## API Methods

### `get_package_json(package_name, version=None)`

- **Purpose**: Retrieve raw PyPI JSON metadata
- **Use cases**: 
  - Getting list of available versions
  - Fetching basic package information
  - Checking package existence
- **Caching**: Both memory and disk
- **Returns**: Raw JSON dictionary from PyPI API

### `get_complete_package_info(package_name, version=None)`

- **Purpose**: Get enriched package information
- **Use cases**:
  - Ebuild generation
  - Python version compatibility checks
  - Dependency resolution
- **Processing**: Parses and enriches raw PyPI data with:
  - Extracted Python versions from classifiers
  - Parsed dependencies
  - Source distribution information
- **Caching**: Caches its own processed result separately
- **Returns**: Enriched metadata dictionary

## Performance Characteristics

| Access Pattern | Time | Description |
|---------------|------|-------------|
| First request | 0.2-1s | HTTP request to PyPI |
| Same process, subsequent | ~0.000001s | Memory cache hit |
| Different process | ~0.001s | Disk cache hit |
| After 1 hour | 0.2-1s | Cache expired, re-fetch |

## Cache Key Examples

```python
# Package without version (latest)
"requests" → cache key: "requests"

# Specific version
"requests", "2.28.0" → cache key: "requests_2.28.0"

# Normalized (lowercase)
"Requests" → cache key: "requests"
```

## Usage Example

```python
# First access - fetches from PyPI (slow)
extractor = PyPIMetadataExtractor()
data = extractor.get_package_json("aiocache")  # ~0.5s

# Same package, different method - uses memory cache (instant)
info = extractor.get_complete_package_info("aiocache")  # ~0.000001s

# Same package, specific version - memory cache (instant)
version_data = extractor.get_package_json("aiocache", "0.12.0")  # ~0.000001s

# Different process/instance - uses disk cache (fast)
new_extractor = PyPIMetadataExtractor()
data = new_extractor.get_package_json("aiocache")  # ~0.001s
```

## Cache Features

### Negative Caching

- **404 responses** are cached to prevent repeated lookups for non-existent packages
- Reduces unnecessary API calls for typos or removed packages
- Same TTL as successful responses

### Atomic Writes

- Disk cache writes use temporary file + atomic rename
- Prevents corruption from concurrent access or crashes
- Ensures cache consistency

### Automatic Cleanup

- **Stale entries**: Removed automatically when TTL expires
- **Corrupted files**: Detected and removed on read
- **Failed reads**: Trigger re-fetch from PyPI

### Memory Management

- In-memory cache is limited to process lifetime
- No explicit size limits (relies on TTL for cleanup)
- Expired entries removed on access

## Configuration

### Cache Directory

Default locations (in order of preference):
1. User-specified via `cache_dir` parameter
2. `~/.cache/portage-pip-fuse/`
3. `/tmp/portage-pip-fuse-cache/`

### TTL Settings

- Default: 3600 seconds (1 hour)
- Configurable via `cache_ttl` parameter
- Applies to both memory and disk caches

## Benefits

1. **Performance**: After initial fetch, access is nearly instantaneous
2. **Consistency**: All code paths see the same data within TTL window
3. **Efficiency**: Each package fetched only once per hour from PyPI
4. **Reliability**: Disk cache survives crashes and restarts
5. **Scalability**: Handles thousands of packages without overwhelming PyPI
6. **Transparency**: Caching is automatic and invisible to callers

## Cache Statistics

The cache system logs its operations for monitoring:

```
INFO: PyPI metadata cache initialized at /home/user/.cache/portage-pip-fuse
DEBUG: Using memory cached data for requests
DEBUG: Using disk cached data for numpy
DEBUG: Cached django_4.2.0 to disk
```

## Thread Safety

- **Memory cache**: Not thread-safe (single process assumption)
- **Disk cache**: Safe for concurrent reads, atomic writes prevent corruption
- **FUSE operation**: Single-threaded mode (`nothreads=False`)

## Cache File Structure Details

### Why Both Package and Version-Specific Files Exist

The cache contains both `{package}.json` and `{package}_{version}.json` files:

| File | PyPI API Endpoint | Contains | Needed For |
|------|-------------------|----------|------------|
| `requests.json` | `/pypi/requests/json` | Latest version metadata + all version URLs | Package discovery, version listing |
| `requests_2.28.0.json` | `/pypi/requests/2.28.0/json` | That specific version's metadata | Accurate ebuild generation |

**This is NOT redundant.** The PyPI API returns different data for each endpoint:

- The **package-only** endpoint returns `info` metadata for the **latest version only**
- The **version-specific** endpoint returns `info` metadata for **that exact version**

Fields like `requires_python`, `requires_dist` (dependencies), and `classifiers` are **version-specific** and change between versions. You cannot generate an accurate ebuild for `requests-2.20.0` using metadata from `requests-2.28.0`.

### Current Implementation: Dual Cache Paths

The current implementation uses two different path strategies in `pip_metadata.py`:

1. **`get_package_json()` (line 277)**: Flat path in cache root
   - Path: `self.cache_dir / f"{cache_key}.json"`
   - Example: `~/.cache/portage-pip-fuse/requests.json`
   - Contains: Raw PyPI JSON API response

2. **`_get_cache_path()` (lines 103-109)**: Subdirectory based on first 2 chars
   - Path: `cache_subdir / f"{cache_key}.json"`
   - Example: `~/.cache/portage-pip-fuse/re/requests.json`
   - Contains: Processed ebuild-ready data (from `get_complete_package_info()`)

These store **different data** (raw vs processed), not duplicates of the same data.

### Future Consolidation Option

If cleanup is desired later, consolidate both paths to use the subdirectory structure:

1. Refactor `get_package_json()` to use `_get_disk_cache()` / `_set_disk_cache()` helpers
2. Add `cache_type` parameter to `_get_cache_key()` to distinguish raw vs processed:
   - `requests.json` - Raw PyPI JSON API response
   - `requests.complete.json` - Processed ebuild-ready data
3. Update `get_complete_package_info()` to use 'complete' cache type

**Benefits**: Consistent code, better scalability for large cache directories
**Trade-offs**: Migration effort, potential for breaking existing caches

## Future Improvements

Potential enhancements for the caching system:

1. **LRU eviction**: Limit memory cache size with least-recently-used eviction
2. **Compression**: Compress disk cache files to save space
3. **Batch prefetching**: Fetch multiple packages in parallel
4. **Differential updates**: Fetch only changed data after TTL
5. **Shared memory**: Allow cache sharing between processes
6. **Consolidate cache paths**: Unify flat and subdirectory strategies (see above)