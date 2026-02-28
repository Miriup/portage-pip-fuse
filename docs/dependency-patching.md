# Dependency Patching System

The dependency patching system provides a virtual filesystem API for runtime modification of PyPI package dependencies. This allows overriding overly-restrictive version constraints that conflict with system packages installed via portage.

## Overview

When PyPI packages specify exact version constraints (e.g., `urllib3==1.26.0`), these may conflict with newer versions already installed on a Gentoo system. The dependency patching system allows you to modify these constraints without forking packages or manually editing generated ebuilds.

### Use Cases

- **Loosen exact version pins**: Change `=dev-python/urllib3-1.26.0` to `>=dev-python/urllib3-1.26.0`
- **Remove obsolete dependencies**: Remove dependencies that are no longer needed
- **Add missing dependencies**: Add dependencies not declared by upstream
- **Apply patches to all versions**: Use `_all` to patch all versions of a package at once

## Directory Structure

The patching system exposes a `.sys/` virtual filesystem at the repository root:

```
/var/db/repos/pypi/.sys/
    dependencies/
        dev-python/
            {package}/
                {version}/                    # e.g., requests/2.31.0/
                    >=dev-python/urllib3-1.21[${PYTHON_USEDEP}]  # one file per dep
                _all/                         # patches apply to all versions
    dependencies-patch/
        dev-python/
            {package}/
                {version}.patch               # e.g., 2.31.0.patch
                _all.patch
```

### dependencies/

This directory shows dependencies for each package version. Each dependency appears as a file whose name is the dependency atom with `/` replaced by `::`.

For example, `>=dev-python/urllib3-1.21[${PYTHON_USEDEP}]` becomes:
`>=dev-python::urllib3-1.21[${PYTHON_USEDEP}]`

- File operations on these files modify the patches
- Dependencies shown reflect original deps with patches applied
- Use shell quoting when working with these filenames (they contain `$`, `[`, `]`)

### dependencies-patch/

This directory contains patch files in a portable text format. These can be exported, shared, and imported on other systems.

## Operations

| Filesystem Op | Effect |
|--------------|--------|
| `mv old_dep new_dep` | Modify dependency version constraint |
| `rm dep` | Remove dependency entirely |
| `touch dep` | Add new dependency |
| `cat *.patch` | View all patches for a package/version |
| `cp *.patch target/` | Export patches for portability |
| `echo "..." > *.patch` | Import patches from another system |

### Filename Encoding

Dependency filenames use `::` instead of `/` since `/` is a path separator.

| In ebuild | As filename |
|-----------|-------------|
| `>=dev-python/urllib3-1.21` | `>=dev-python::urllib3-1.21` |
| `=dev-python/requests-2.31.0` | `=dev-python::requests-2.31.0` |

Remember to quote filenames in shell commands due to special characters like `$`, `[`, `]`.

## Patch File Format

Patch files use a simple line-based format:

```
# Comments start with #
-> =dev-python/xyz-1.0[${PYTHON_USEDEP}] >=dev-python/xyz-1.0[${PYTHON_USEDEP}]
-- =dev-python/unwanted-1.0[${PYTHON_USEDEP}]
++ >=dev-python/needed-2.0[${PYTHON_USEDEP}]
```

| Prefix | Operation |
|--------|-----------|
| `->` | Modify: `-> old_dep new_dep` |
| `--` | Remove: `-- dep_to_remove` |
| `++` | Add: `++ new_dep` |

## CLI Options

```bash
# Mount with default patch file (~/.config/portage-pip-fuse/patches.json)
portage-pip-fuse mount

# Mount with custom patch file
portage-pip-fuse mount --patch-file /path/to/patches.json

# Mount without patching (read-only ebuild generation)
portage-pip-fuse mount --no-patches
```

## Usage Examples

### Example 1: Loosen urllib3 version constraint in requests

The `requests` package often pins an exact urllib3 version. To allow any compatible version:

```bash
cd /var/db/repos/pypi/.sys/dependencies/dev-python/requests/2.31.0/

# List current dependencies (/ shown as ::)
ls
# Shows: =dev-python::urllib3-1.26.18[${PYTHON_USEDEP}]  (etc.)

# Modify the constraint (rename the file)
# Change = to >=
mv '=dev-python::urllib3-1.26.18[${PYTHON_USEDEP}]' \
   '>=dev-python::urllib3-1.26.18[${PYTHON_USEDEP}]'

# Verify the ebuild now has the modified dependency
cat /var/db/repos/pypi/dev-python/requests/requests-2.31.0.ebuild | grep urllib3
```

### Example 2: Remove an unwanted dependency

Some packages declare optional dependencies as required. To remove one:

```bash
cd /var/db/repos/pypi/.sys/dependencies/dev-python/problematic-pkg/1.0.0/

# List deps to find the one to remove
ls

# Remove the dependency
rm '=dev-python::unwanted-dep-1.0[${PYTHON_USEDEP}]'
```

### Example 3: Add a missing dependency

If upstream forgot to declare a dependency:

```bash
cd /var/db/repos/pypi/.sys/dependencies/dev-python/incomplete-pkg/1.0.0/

# Add the missing dependency (use :: instead of /)
touch '>=dev-python::missing-dep-1.0[${PYTHON_USEDEP}]'
```

### Example 4: Apply patches to all versions

To apply the same patch to all versions of a package:

```bash
cd /var/db/repos/pypi/.sys/dependencies/dev-python/requests/_all/

# Remove a deprecated dependency from all versions
rm '=dev-python::old-dep-1.0[${PYTHON_USEDEP}]'
```

### Example 5: Export and import patches

To share patches between systems:

```bash
# Export patches
cat /var/db/repos/pypi/.sys/dependencies-patch/dev-python/requests/2.31.0.patch > ~/requests-patches.txt

# Import on another system
cat ~/requests-patches.txt > /var/db/repos/pypi/.sys/dependencies-patch/dev-python/requests/2.31.0.patch
```

## Persistence

Patches are stored in a JSON file and persist across remounts:

- **Default location**: `~/.config/portage-pip-fuse/patches.json`
- **Custom location**: Use `--patch-file PATH`

Patches are automatically saved when:
- The filesystem is unmounted
- The mount process receives SIGTERM

## Patch Application Order

Patches are applied in the following order:

1. **_all patches** (sorted by timestamp)
2. **Version-specific patches** (sorted by timestamp)

This allows _all patches to set baseline modifications that version-specific patches can override.

## Verifying Patches

To verify patches are applied correctly:

```bash
# Check the generated ebuild
cat /var/db/repos/pypi/dev-python/requests/requests-2.31.0.ebuild | grep RDEPEND

# Use emerge to show dependencies
emerge -pv dev-python/requests
```

## Troubleshooting

### Patches not applied?

1. Check if patching is enabled: Look for "Dependency patching enabled" in mount output
2. Verify the patch file location: `ls ~/.config/portage-pip-fuse/patches.json`
3. Check patch file contents: `cat ~/.config/portage-pip-fuse/patches.json`

### File operations fail with "Read-only filesystem"?

- The `.sys/` filesystem is only available when patching is enabled
- Use `--patch-file` or ensure default patch directory is writable

### Patches reset after remount?

- Patches should be saved automatically on unmount
- Use `fusermount -u` or send SIGTERM for clean unmount
- Avoid `kill -9` which skips the save

## Implementation Details

### Storage Format

Patches are stored in JSON:

```json
{
  "version": 1,
  "patches": [
    {
      "category": "dev-python",
      "package": "requests",
      "version": "2.31.0",
      "patches": [
        {
          "operation": "modify",
          "old_dep": "=dev-python/urllib3-1.26.18[${PYTHON_USEDEP}]",
          "new_dep": ">=dev-python/urllib3-1.26.18[${PYTHON_USEDEP}]",
          "timestamp": 1700000000.0
        }
      ]
    }
  ]
}
```

### Data Flow

```
User file operation in .sys/dependencies/
    |
    v
FUSE: rename/unlink/create
    |
    v
DependencyPatchStore.modify/remove/add_dependency()
    |
    v
patches.json (persisted on shutdown)
    |
    v
Portage reads ebuild --> _generate_ebuild() --> apply_patches() --> RDEPEND
```

## Limitations

- Patches only affect RDEPEND and OPTIONAL_DEPEND, not BDEPEND or DEPEND
- Package names in patches must exactly match the generated atom format
- USE flag conditions are not directly patchable (add/remove entire atoms instead)
