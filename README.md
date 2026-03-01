# portage-pip-fuse

A FUSE-based filesystem that presents PyPI packages as a Gentoo portage overlay, enabling direct installation of Python packages via `emerge`.

## Overview

This project provides a virtual filesystem that dynamically generates Gentoo ebuilds from PyPI package metadata. When mounted, it appears as a standard portage overlay containing all compatible PyPI packages, allowing seamless integration between Python's package ecosystem and Gentoo's package management.

## Features

- **FUSE virtual overlay**: Presents PyPI as a portage-compatible repository
- **pip command translation**: Run `pip install` commands and have them translated to `emerge`
- **SQLite metadata backend**: Uses bulk PyPI database (~1GB) for fast lookups instead of 746k+ individual API calls
- **Smart filtering**: Only shows packages compatible with your system's Python versions
- **Source distribution filtering**: Only shows packages with source tarballs (required for Gentoo)
- **Dynamic ebuild generation**: Creates ebuilds on-the-fly from PyPI metadata
- **Automatic name translation**: Converts between PyPI and Gentoo package naming conventions
- **Dependency mapping**: Translates PyPI dependencies to Gentoo atoms
- **Manifest generation**: Creates Manifest files with checksums from PyPI
- **Runtime patching**: Modify dependencies, Python compatibility, USE flags, and ebuild phases via `.sys/` virtual filesystem

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
portage-pip-fuse sync [--cache-dir DIR] [options]

# Delete the metadata database
portage-pip-fuse unsync [--force]

# Translate pip install to emerge (see below)
portage-pip-fuse pip install [packages...] [-r requirements.txt]
```

### Sync Command Options

The `sync` command downloads and manages the PyPI metadata database (~1GB compressed, ~10GB uncompressed):

```bash
# Standard sync (download and decompress)
portage-pip-fuse sync

# Only download the compressed database (no decompression)
portage-pip-fuse sync --only-download

# Only decompress existing .gz file (keeps .gz after decompression)
portage-pip-fuse sync --only-decompress

# Delete the compressed .gz file
portage-pip-fuse sync --delete-gz

# Delete the uncompressed SQLite database
portage-pip-fuse sync --delete-sqlite
```

#### Custom Workflow for Memory-Constrained Systems

For systems that cannot store the full 10GB SQLite database on disk, you can use an overlayfs with tmpfs:

```bash
# 1. Download the compressed database (~1GB)
portage-pip-fuse sync --only-download

# 2. Mount overlayfs with tmpfs on top (example)
mkdir -p /tmp/overlay/{upper,work}
sudo mount -t overlay overlay \
  -o lowerdir=~/.cache/portage-pip-fuse,upperdir=/tmp/overlay/upper,workdir=/tmp/overlay/work \
  ~/.cache/portage-pip-fuse

# 3. Decompress to the tmpfs overlay
portage-pip-fuse sync --only-decompress

# 4. Use the FUSE filesystem normally
portage-pip-fuse mount
emerge -av dev-python/requests

# 5. Clean up when done
portage-pip-fuse unmount
portage-pip-fuse sync --delete-sqlite

# 6. Sync overlayfs changes back to disk if needed
# (Any metadata cache files will be preserved)
sudo umount ~/.cache/portage-pip-fuse
```

This workflow allows you to use the large database on systems with limited disk space but sufficient RAM (16GB+ recommended).

### pip Command

The `pip` subcommand lets you copy-paste `pip install` commands from documentation and tutorials, translating them to appropriate `emerge` commands.

```bash
# Basic package installation
portage-pip-fuse pip install requests flask
# → emerge --ask dev-python/requests dev-python/flask

# With version constraints
portage-pip-fuse pip install "django>=4.0" "celery~=5.3.0"
# → emerge --ask >=dev-python/django-4.0 >=dev-python/celery-5.3.0

# From requirements file (creates portage set)
portage-pip-fuse pip install -r requirements.txt
# → Creates /etc/portage/sets/{project}-dependencies
# → emerge --ask @{project}-dependencies

# Upgrade packages
portage-pip-fuse pip install --upgrade requests
# → emerge --ask --update dev-python/requests

# Dry run (show what would happen)
portage-pip-fuse pip install --dry-run -r requirements.txt
```

#### pip Options

```
-r, --requirement FILE   Install from requirements file(s)
-U, --upgrade            Upgrade packages (emerge --update)
--dry-run                Show what would be done without executing
--pretend                Pass --pretend to emerge
--ask / --no-ask         Control emerge confirmation (default: --ask)
--set-dir DIR            Directory for portage sets (default: /etc/portage/sets)
```

#### Version Specifier Translation

| PyPI | Gentoo |
|------|--------|
| `>=2.0` | `>=pkg-2.0` |
| `==2.0.0` | `=pkg-2.0.0` |
| `~=2.0` | `>=pkg-2.0` (compatible release) |
| `!=2.0` | `!=pkg-2.0` |
| `==2.*` | `=pkg-2*` |
| `2.0a1` | `2.0_alpha1` |
| `2.0b1` | `2.0_beta1` |
| `2.0rc1` | `2.0_rc1` |
| `2.0.post1` | `2.0_p1` |

#### Extras and USE Flags

When packages specify extras (e.g., `requests[security]`), the command shows what USE flags need to be set:

```bash
$ portage-pip-fuse pip install "requests[security,socks]"
Note: The following packages require USE flags:
Add to /etc/portage/package.use:
  dev-python/requests security socks

Would run: emerge --ask dev-python/requests
```

#### Requirements File Support

Requirements files are parsed with support for:
- Package specifiers with versions: `django>=4.0`
- Extras: `flask[async]`
- Comments and blank lines
- Line continuations (`\`)
- Environment variables (`${VAR}`)
- Nested `-r` includes
- Environment markers (passed through)

When using `-r`, a portage set file is created at `/etc/portage/sets/{name}-dependencies` where `{name}` is derived from the requirements file path. This allows you to easily update dependencies later with `emerge @{name}-dependencies`.

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
   - Manifest with SHA256/MD5 checksums (PyPI doesn't provide BLAKE2B-512)

4. **Name Translation**: Automatically converts between naming conventions:
   - PyPI: `Pillow`, `scikit-learn`, `ruamel.yaml`
   - Gentoo: `pillow`, `scikit-learn`, `ruamel-yaml`

## Patching and Customization

The `.sys/` virtual filesystem allows runtime modification of generated ebuilds:

| Directory | Purpose |
|-----------|---------|
| `.sys/RDEPEND/` | Modify runtime dependencies (RDEPEND) |
| `.sys/DEPEND/` | Add build-time dependencies (DEPEND) |
| `.sys/python-compat/` | Adjust Python version compatibility |
| `.sys/iuse/` | Add/remove USE flags |
| `.sys/ebuild-append/` | Add custom phase functions |

### Quick Example: Fix a Package

```bash
# Remove incompatible Python version
echo '-- python3_13' > /var/db/repos/pypi/.sys/python-compat-patch/dev-python/oldpkg/_all.patch

# Add missing dependency
touch '/var/db/repos/pypi/.sys/RDEPEND/dev-python/broken-pkg/_all/>=dev-python::missing-1.0'

# Add custom src_configure
echo 'export MY_VAR=1' > /var/db/repos/pypi/.sys/ebuild-append/dev-python/pkg/_all/src_configure
```

See [docs/build-error-fixes.md](docs/build-error-fixes.md) for comprehensive examples.

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
