# How pypi.eclass Chooses Between Wheel and Sdist

This documents how Gentoo's `pypi.eclass` (located at `/var/db/repos/gentoo/eclass/pypi.eclass`) handles the choice between wheel and source distribution (sdist) artifacts from PyPI.

## Short Answer

**It doesn't choose. It always defaults to sdist (.tar.gz).**

The eclass provides helper functions for both formats, but the default behavior only sets up sdist downloads.

## Default Behavior

When an ebuild inherits `pypi`, the `_pypi_set_globals` function runs automatically and sets:

```bash
SRC_URI=${_PYPI_SDIST_URL}   # Always sdist
```

This means:
- `SRC_URI` → sdist URL (`.tar.gz` from `files.pythonhosted.org/packages/source/...`)
- `S` → extracted source directory based on normalized package name

## To Use a Wheel Instead

The ebuild author must **explicitly override** `SRC_URI`:

```bash
inherit pypi

# Override to use wheel instead of sdist
SRC_URI="$(pypi_wheel_url --unpack)"
BDEPEND="app-arch/unzip"  # wheels are zip files
```

The `--unpack` flag adds a SRC_URI arrow operator that renames the wheel with a `.zip` suffix so portage's default `src_unpack` handles it.

## Available Helper Functions

| Function | Purpose |
|----------|---------|
| `pypi_sdist_url` | Generate URL for source distribution (default) |
| `pypi_wheel_url` | Generate URL for wheel |
| `pypi_wheel_name` | Generate wheel filename |
| `pypi_normalize_name` | Normalize project name (lowercase, underscores) |
| `pypi_translate_version` | Convert Gentoo version to PEP 440 |

## URL Formats

**Sdist URL pattern:**
```
https://files.pythonhosted.org/packages/source/{first_letter}/{project}/{normalized_name}-{version}.tar.gz
```

**Wheel URL pattern:**
```
https://files.pythonhosted.org/packages/{python_tag}/{first_letter}/{project}/{wheel_name}.whl
```

## Why Sdist is the Default

Gentoo's philosophy is **build from source**. Sdists contain source code that gets compiled on the user's system, allowing:

- Architecture-specific optimizations (CFLAGS, march, etc.)
- Custom compiler flags
- Verification of what's being installed
- Consistency with Gentoo's build-from-source model

Wheels are pre-built binaries, which Gentoo generally avoids except for:
- Bootstrapping scenarios
- Packages where no sdist exists
- Pure-Python wheels (no compiled code) in specific cases

## Implications for portage-pip-fuse

Since Gentoo expects sdists by default, portage-pip-fuse should:

1. **Filter packages to those with sdists available** - the `source-dist` filter does this
2. **Generate SRC_URI using sdist URLs** - matching what pypi.eclass does
3. **Only consider wheels when sdist is unavailable** - and even then, only pure-Python wheels

This is why the `source-dist` filter is essential and enabled by default.
