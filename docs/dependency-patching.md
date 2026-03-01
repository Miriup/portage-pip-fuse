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
    RDEPEND/
        dev-python/
            {package}/
                {version}/                    # e.g., requests/2.31.0/
                    >=dev-python/urllib3-1.21[${PYTHON_USEDEP}]  # one file per dep
                _all/                         # patches apply to all versions
    RDEPEND-patch/
        dev-python/
            {package}/
                {version}.patch               # e.g., 2.31.0.patch
                _all.patch
```

### RDEPEND/

This directory shows dependencies for each package version. Each dependency appears as a file whose name is the dependency atom with `/` replaced by `::`.

For example, `>=dev-python/urllib3-1.21[${PYTHON_USEDEP}]` becomes:
`>=dev-python::urllib3-1.21[${PYTHON_USEDEP}]`

- File operations on these files modify the patches
- Dependencies shown reflect original deps with patches applied
- Use shell quoting when working with these filenames (they contain `$`, `[`, `]`)

### RDEPEND-patch/

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
cd /var/db/repos/pypi/.sys/RDEPEND/dev-python/requests/2.31.0/

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
cd /var/db/repos/pypi/.sys/RDEPEND/dev-python/problematic-pkg/1.0.0/

# List deps to find the one to remove
ls

# Remove the dependency
rm '=dev-python::unwanted-dep-1.0[${PYTHON_USEDEP}]'
```

### Example 3: Add a missing dependency

If upstream forgot to declare a dependency:

```bash
cd /var/db/repos/pypi/.sys/RDEPEND/dev-python/incomplete-pkg/1.0.0/

# Add the missing dependency (use :: instead of /)
touch '>=dev-python::missing-dep-1.0[${PYTHON_USEDEP}]'
```

### Example 4: Apply patches to all versions

To apply the same patch to all versions of a package:

```bash
cd /var/db/repos/pypi/.sys/RDEPEND/dev-python/requests/_all/

# Remove a deprecated dependency from all versions
rm '=dev-python::old-dep-1.0[${PYTHON_USEDEP}]'
```

### Example 5: Fix slot conflicts with patch file

When emerge reports slot conflicts between portage-pip-fuse and gentoo packages:

```bash
# Create patch file to loosen multiple dependencies at once
cat > /var/db/repos/pypi/.sys/dependencies-patch/dev-python/open-webui/0.8.5.patch << 'EOF'
# httpx: gentoo has 0.28.1-r1, fuse wants exactly 0.28.1
-> =dev-python/httpx-0.28.1[${PYTHON_USEDEP}] >=dev-python/httpx-0.28.1[${PYTHON_USEDEP}]
# pillow: gentoo has 11.3.0, fuse wants 12.1.0
-> =dev-python/pillow-12.1.0[${PYTHON_USEDEP}] >=dev-python/pillow-11.0[${PYTHON_USEDEP}]
EOF
```

### Example 6: Remove upper bound constraint

When a package has an upper bound that conflicts with installed versions:

```bash
cat > /var/db/repos/pypi/.sys/dependencies-patch/dev-python/youtube-transcript-api/1.2.4.patch << 'EOF'
# defusedxml: package wants <0.8 but gentoo has 0.8.0_rc2
-> <dev-python/defusedxml-0.8[${PYTHON_USEDEP}] dev-python/defusedxml[${PYTHON_USEDEP}]
EOF
```

### Example 7: Lower minimum version requirement

When a package requires a newer version than what's in gentoo:

```bash
cat > /var/db/repos/pypi/.sys/dependencies-patch/dev-python/black/26.1.0.patch << 'EOF'
# pathspec: package wants >=1.0 but gentoo has 0.12.1
-> >=dev-python/pathspec-1.0[${PYTHON_USEDEP}] >=dev-python/pathspec-0.12[${PYTHON_USEDEP}]
EOF
```

### Example 8: Export and import patches

To share patches between systems:

```bash
# Export patches
cat /var/db/repos/pypi/.sys/RDEPEND-patch/dev-python/requests/2.31.0.patch > ~/requests-patches.txt

# Import on another system
cat ~/requests-patches.txt > /var/db/repos/pypi/.sys/RDEPEND-patch/dev-python/requests/2.31.0.patch
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

### Storage Format (v3)

Patches are stored in JSON with mount-point namespacing:

```json
{
  "version": 3,
  "mount_points": {
    "/var/db/repos/pypi": {
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
      ],
      "python_compat_patches": [...],
      "ebuild_appends": [...],
      "iuse_patches": [...],
      "git_file_content": "gitdir: /home/user/pypi-config/.git/worktrees/pypi"
    }
  }
}
```

#### Version History

| Version | Changes |
|---------|---------|
| 1 | Initial format with top-level `patches` array |
| 2 | Added `python_compat_patches` |
| 3 | Added mount-point namespacing and `git_file_content` |

#### Backward Compatibility

When loading v1/v2 files, patches are read from the top level. On first save, the file is migrated to v3 format with all existing data moved to the current mount point's namespace.

### Data Flow

```
User file operation in .sys/RDEPEND/
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

## Build-Time Dependencies (DEPEND)

In addition to runtime dependencies (RDEPEND), you can also patch build-time dependencies using `.sys/DEPEND/`:

```
/var/db/repos/pypi/.sys/
    DEPEND/
        dev-python/
            {package}/
                {version}/
                    net-dns::c-ares          # Build-time dependency
                _all/
    DEPEND-patch/
        dev-python/
            {package}/
                {version}.patch
                _all.patch
```

### Example: Add Build Dependencies for gevent

gevent needs c-ares and libev development headers at build time:

```bash
# Add build-time dependencies
touch '/var/db/repos/pypi/.sys/DEPEND/dev-python/gevent/_all/net-dns::c-ares'
touch '/var/db/repos/pypi/.sys/DEPEND/dev-python/gevent/_all/dev-libs::libev'

# Verify in the ebuild
cat /var/db/repos/pypi/dev-python/gevent/gevent-25.9.1.ebuild | grep DEPEND
```

## Handling `|| ( )` Groups

When PyPI specifies exact versions, portage-pip-fuse often generates `|| ( )` groups to handle version normalization:

```
|| ( =dev-python/httpx-0.28.1[${PYTHON_USEDEP}] =dev-python/httpx-0.28.1.0[${PYTHON_USEDEP}] )
```

**Patches automatically match these groups.** When you write:

```
-> =dev-python/httpx-0.28.1[${PYTHON_USEDEP}] >=dev-python/httpx-0.28.1[${PYTHON_USEDEP}]
```

The patch system extracts the package name (`dev-python/httpx`) from the first atom in the `|| ( )` group and replaces the entire group with your new dependency.

This means you don't need to know whether the dependency is a simple atom or an `|| ( )` group - just target the package name and version.

## Multiple Mount Points

When running multiple FUSE instances with different mount points (e.g., `/var/db/repos/pypi` and `/mnt/pypi-wheels`), each instance has **isolated configuration**.

### Mount-Point Namespacing

Patches are namespaced by mount point in the JSON storage:

```json
{
  "version": 3,
  "mount_points": {
    "/var/db/repos/pypi": {
      "patches": [...],
      "python_compat_patches": [...],
      "ebuild_appends": [...],
      "iuse_patches": [...]
    },
    "/mnt/pypi-wheels": {
      "patches": [...],
      ...
    }
  }
}
```

This means:
- Patches added on one mount point don't affect others
- Each mount point can have different dependency modifications
- The same `patches.json` file can be shared between mounts safely

### Example: Separate Configurations

```bash
# Mount two instances with different purposes
portage-pip-fuse mount /var/db/repos/pypi      # Production
portage-pip-fuse mount /mnt/pypi-testing       # Testing

# Add a patch to production only
touch '/var/db/repos/pypi/.sys/dependencies/dev-python/requests/2.31.0/>=dev-python::urllib3-1.26'

# The testing mount won't see this patch
ls /mnt/pypi-testing/.sys/dependencies/dev-python/requests/2.31.0/
# (urllib3 constraint unchanged)
```

### Using Separate Patch Files

For complete isolation, use separate patch files:

```bash
portage-pip-fuse mount /var/db/repos/pypi --patch-file ~/.config/pypi-prod.json
portage-pip-fuse mount /mnt/pypi-test --patch-file ~/.config/pypi-test.json
```

### Race Condition Warning

When multiple FUSE instances share the same `patches.json` file, concurrent saves may cause one instance's changes to be lost. Each instance reads the full file, modifies its section, and writes back.

**Mitigation**: Each mount point has an isolated namespace, so normal usage is safe. For guaranteed isolation with concurrent writes, use separate `--patch-file` paths.

## Git Worktree Support

The `.sys/` virtual filesystem can be version-controlled using git worktrees. This allows you to:
- Track configuration changes in git
- Share patches between systems via git
- Roll back configuration changes
- Maintain different configurations per branch

### Setting Up Git Worktree

1. Create a git repository to store your configuration:

```bash
# Create a repo for your PyPI patches
mkdir ~/pypi-config
cd ~/pypi-config
git init
git commit --allow-empty -m "Initial commit"
```

2. Add a worktree at your mount point's `.sys` directory:

```bash
# Mount the FUSE filesystem first
portage-pip-fuse mount /var/db/repos/pypi

# Add worktree (creates .sys/.git as a FILE, not directory)
cd ~/pypi-config
git worktree add /var/db/repos/pypi/.sys main
```

3. The `.sys/.git` file will contain:
```
gitdir: /home/user/pypi-config/.git/worktrees/pypi
```

### Git Worktree vs Git Init

**Important**: Use `git worktree add`, NOT `git init`.

- `git worktree add` creates `.git` as a **file** pointing to the main repo
- `git init` tries to create `.git` as a **directory**, which is **denied** by the FUSE filesystem

If you accidentally run `git init`, you'll see:
```
error: cannot mkdir .git: Operation not permitted
```

The log will show:
```
WARNING - Attempted mkdir .sys/.git - use 'git worktree add' instead of 'git init'.
Create a repo elsewhere and use: git worktree add /mountpoint/.sys <branch>
```

### Committing Configuration Changes

After making patch changes:

```bash
cd /var/db/repos/pypi/.sys

# Stage your changes
git add dependencies-patch/dev-python/requests/2.31.0.patch

# Commit
git commit -m "Loosen urllib3 constraint for requests 2.31.0"

# Push to remote for backup/sharing
git push origin main
```

### Managing Multiple Systems

Create branches for different systems:

```bash
cd ~/pypi-config

# Create branches for different machines
git branch workstation
git branch server

# On workstation: use workstation branch
git worktree add /var/db/repos/pypi/.sys workstation

# On server: use server branch
git worktree add /var/db/repos/pypi/.sys server
```

### Handling Merge Conflicts

Since the FUSE filesystem stores patch content in `patches.json`, merge conflicts should be handled in the main repository clone (not the worktree):

```bash
cd ~/pypi-config

# Resolve conflicts in the main repo
git merge feature-branch
# ... resolve conflicts ...
git add .
git commit

# Changes automatically appear in worktree
```

### Example Workflow

```bash
# 1. Initial setup (once)
mkdir ~/pypi-config && cd ~/pypi-config
git init
git commit --allow-empty -m "Initial pypi-config repo"

# 2. Mount and link
portage-pip-fuse mount /var/db/repos/pypi
git worktree add /var/db/repos/pypi/.sys main

# 3. Make changes via .sys filesystem
cd /var/db/repos/pypi/.sys
touch 'dependencies/dev-python/requests/2.31.0/>=dev-python::urllib3-1.26'

# 4. Commit changes
git add -A
git commit -m "Loosen urllib3 for requests"

# 5. Share with another system
git remote add origin git@github.com:user/pypi-config.git
git push -u origin main
```

## PEP517 Backend Patching

The `.sys/pep517/` virtual filesystem allows overriding the `DISTUTILS_USE_PEP517` value in generated ebuilds. This is useful when portage-pip-fuse's auto-detection fails or when a package requires a specific build backend.

### Directory Structure

```
/var/db/repos/pypi/.sys/
    pep517/
        dev-python/
            {package}/
                {version}             # File containing backend name
                _all                   # Global for all versions
    pep517-patch/
        dev-python/
            {package}/
                {version}.patch
                _all.patch
```

### Valid Backend Values

| Value | Description |
|-------|-------------|
| `standalone` | Auto-detect (default) |
| `setuptools` | setuptools backend |
| `flit` | flit_core backend |
| `hatchling` | hatchling backend |
| `poetry` | poetry-core backend |
| `pdm-backend` | pdm backend |
| `maturin` | maturin (Rust) backend |
| `meson-python` | meson-python backend |
| `scikit-build-core` | scikit-build-core backend |
| `sip` | sip backend |
| `no` | Disable PEP517 (legacy setup.py) |

### Example: Fix PEP517 Backend for pypdf

When a package fails to build due to PEP517 backend mismatch:

```bash
# Set the backend for a specific version
echo 'flit' > /var/db/repos/pypi/.sys/pep517/dev-python/pypdf/5.4.0

# Or for all versions
echo 'flit' > /var/db/repos/pypi/.sys/pep517/dev-python/pypdf/_all

# Verify in the ebuild
grep DISTUTILS_USE_PEP517 /var/db/repos/pypi/dev-python/pypdf/pypdf-5.4.0.ebuild
# Output: DISTUTILS_USE_PEP517=flit
```

### Patch File Format

The patch file format is simpler than other patch types since it's a single value:

```
# Comment
== flit
```

### Auto-Fix with bashrc Hook

A portage bashrc hook can automatically detect PEP517 mismatches and either patch them or provide instructions:

```bash
# Install the hook
sudo cp docs/bashrc/pep517-autofix.bashrc /etc/portage/bashrc

# Or source from existing bashrc:
echo 'source /path/to/pep517-autofix.bashrc' >> /etc/portage/bashrc
```

When a build fails due to PEP517 mismatch, the hook will:
1. Detect the actual backend from pyproject.toml
2. Map it to the correct DISTUTILS_USE_PEP517 value
3. Auto-patch the filesystem (if not sandboxed)
4. Print instructions for manual fix (if sandboxed)

Example output when hook detects a mismatch:
```
 * ==============================================
 * PEP517 Backend Mismatch Detected!
 * ==============================================
 * The package uses: flit_core.buildapi
 * Expected DISTUTILS_USE_PEP517=flit
 *
 * To fix, run:
 *   echo 'flit' > /var/db/repos/pypi/.sys/pep517/dev-python/pypdf/5.4.0
 *
 * Then re-emerge the package:
 *   emerge -1 dev-python/pypdf
 * ==============================================
```

## Limitations

- USE flag conditions are not directly patchable (add/remove entire atoms instead)
