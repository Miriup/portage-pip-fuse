# portage-pip-fuse

A FUSE-based filesystem that presents PyPI packages as a Gentoo portage overlay, enabling direct installation of Python packages via `emerge`.

## Overview

This project provides a virtual filesystem that dynamically generates Gentoo ebuilds from PyPI package metadata. When mounted, it appears as a standard portage overlay containing all compatible PyPI packages, allowing seamless integration between Python's package ecosystem and Gentoo's package management.

## Features

- **FUSE virtual overlay**: Presents PyPI as a portage-compatible repository
- **SQLite metadata backend**: Uses bulk PyPI database (~1GB) for fast lookups instead of 746k+ individual API calls
- **Smart filtering**: Only shows packages compatible with your system's Python versions
- **Source distribution filtering**: Only shows packages with source tarballs (required for Gentoo)
- **Dynamic ebuild generation**: Creates ebuilds on-the-fly from PyPI metadata
- **Automatic name translation**: Converts between PyPI and Gentoo package naming conventions
- **Dependency mapping**: Translates PyPI dependencies to Gentoo atoms
- **Manifest generation**: Creates Manifest files with checksums from PyPI

## Requirements

- Python >= 3.8
- FUSE support in kernel (`modprobe fuse`)
- fusepy >= 3.0.1
- Gentoo Linux with portage
- ~1GB disk space for PyPI metadata cache

## Installation

```bash
# Install from source
git clone https://github.com/Miriup/portage-pip-fuse
cd portage-pip-fuse
pip install -e .

# Create the portage repos.conf entry
sudo portage-pip-fuse install

# Sync the PyPI metadata database (downloads ~1GB, expands to ~10GB)
portage-pip-fuse sync
```

## Usage

### Basic Usage

```bash
# Create mountpoint (if needed)
sudo mkdir -p /var/db/repos/pypi
sudo chown $(id -u):$(id -g) /var/db/repos/pypi

# Mount the filesystem
portage-pip-fuse mount

# Install PyPI packages via emerge
emerge -av dev-python/requests

# Unmount when done
portage-pip-fuse unmount
```

### CLI Commands

```bash
# Show help
portage-pip-fuse

# Mount the FUSE filesystem
portage-pip-fuse mount [mountpoint] [options]

# Unmount the filesystem
portage-pip-fuse unmount [mountpoint]
portage-pip-fuse unmount --pid-file /path/to/pidfile

# Create /etc/portage/repos.conf entry
portage-pip-fuse install [mountpoint] [--priority N]

# Sync PyPI metadata database
portage-pip-fuse sync [--cache-dir DIR]

# Delete the metadata database
portage-pip-fuse unsync [--force]
```

### Mount Options

```bash
portage-pip-fuse mount [mountpoint] [options]

Options:
  -f, --foreground     Run in foreground (default: daemonize)
  -d, --debug          Enable debug logging
  --logfile PATH       Log to file instead of stderr
  --cache-dir DIR      Cache directory for metadata
  --cache-ttl SEC      Cache TTL in seconds (default: 3600)
  --pid-file PATH      Write PID file for unmounting
  --timestamps         Enable PyPI upload timestamps (slower)
  --no-sqlite          Disable SQLite backend, use JSON API only
```

### Cache Locations

The metadata cache is stored in the first writable location:
1. `--cache-dir` (if specified)
2. `~/.cache/portage-pip-fuse`
3. `/var/cache/portage-pip-fuse`

## How It Works

1. **Metadata Backend**: On first sync, downloads a ~1GB SQLite database containing metadata for all PyPI packages. This is updated daily by [pypi-data](https://github.com/pypi-data/pypi-json-data).

2. **Package Filtering**: Only packages meeting these criteria are shown:
   - Have source distributions (sdist) available
   - Are compatible with your system's Python versions (PYTHON_TARGETS)

3. **Ebuild Generation**: When you access a package directory, ebuilds are generated dynamically:
   - Package metadata from PyPI/SQLite
   - PYTHON_COMPAT from package classifiers
   - Dependencies mapped to Gentoo atoms
   - Manifest with SHA256/BLAKE2B checksums

4. **Name Translation**: Automatically converts between naming conventions:
   - PyPI: `Pillow`, `scikit-learn`, `ruamel.yaml`
   - Gentoo: `pillow`, `scikit-learn`, `ruamel-yaml`

## Repository Structure

When mounted, the filesystem provides:

```
/var/db/repos/pypi/
├── dev-python/
│   ├── requests/
│   │   ├── requests-2.31.0.ebuild
│   │   ├── requests-2.28.1.ebuild
│   │   ├── metadata.xml
│   │   └── Manifest
│   ├── flask/
│   │   └── ...
│   └── ...
├── metadata/
│   └── layout.conf
└── profiles/
    └── repo_name
```

## Configuration

### repos.conf

The `install` command creates `/etc/portage/repos.conf/portage-pip-fuse.conf`:

```ini
[portage-pip-fuse]
location = /var/db/repos/pypi
sync-type =
auto-sync = no
priority = 50
```

### Environment

```bash
# Use HTTP proxy for database download
https_proxy=http://proxy:3128 portage-pip-fuse sync
```

## Troubleshooting

### "Repository is missing masters attribute"

This warning is harmless. The FUSE filesystem provides `layout.conf` with `masters = gentoo`.

### Slow package listings

The first access may be slow as metadata is fetched. Subsequent accesses use the cache. Use `--no-sqlite` only if needed, as it's much slower.

### Database sync fails

```bash
# Check disk space (needs ~12GB total)
df -h ~/.cache/portage-pip-fuse

# Retry with debug output
portage-pip-fuse sync --debug
```

## Development

```bash
# Install development dependencies
pip install -e .[dev]

# Run tests
pytest

# Run with test mode (no actual mounting)
portage-pip-fuse mount --test

# Format code
black portage_pip_fuse
isort portage_pip_fuse
```

## License

GPL-2.0 - See LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit pull requests.

## Related Projects

- [g-sorcery](https://github.com/jauhien/g-sorcery) - Framework for ebuild generators
- [gs-pypi](https://github.com/jauhien/gs-pypi) - PyPI backend for g-sorcery
- [pypi-data](https://github.com/pypi-data/pypi-json-data) - Daily PyPI metadata dumps
