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

## Future Improvements

Potential enhancements for the caching system:

1. **LRU eviction**: Limit memory cache size with least-recently-used eviction
2. **Compression**: Compress disk cache files to save space
3. **Batch prefetching**: Fetch multiple packages in parallel
4. **Differential updates**: Fetch only changed data after TTL
5. **Shared memory**: Allow cache sharing between processes