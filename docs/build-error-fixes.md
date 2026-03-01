# Fixing Build Errors with .sys/ Patches

When packages fail to build, you can use the `.sys/` virtual filesystem to patch Python compatibility or dependencies without modifying the generated ebuilds directly.

## Quick Reference

| Error Type | Solution Directory |
|------------|-------------------|
| Python API incompatibility | `.sys/python-compat/` |
| Missing/wrong Python version | `.sys/python-compat/` |
| Runtime dependency conflict | `.sys/RDEPEND/` |
| Missing runtime dependency | `.sys/RDEPEND/` |
| Missing build dependency | `.sys/DEPEND/` |
| Missing USE flags | `.sys/iuse/` |
| Custom build phase needed | `.sys/ebuild-append/` |

## Analyzing Build Errors

When you encounter a build error, look for:

1. **Package name and version** - from the ebuild path or error message
2. **Python version** - look for `python3_XX` in paths like `/work/package-python3_13/`
3. **Error type** - compiler errors, import errors, version conflicts

## Python Compatibility Patches

Use `.sys/python-compat/` when a package fails to build for a specific Python version.

### Common Symptoms

- Compiler errors about missing/changed Python C API functions
- `ImportError` during build for Python-version-specific modules
- Package was released before a Python version existed
- Multi-Python builds fail due to cached build artifacts (e.g., autotools cache conflicts)

### Patch Format

```
++ python3_13          # Add Python implementation
-- python3_14          # Remove Python implementation
== python3_11 python3_12 python3_13   # Set explicit list (replaces all)
```

### Two Interfaces

There are two ways to apply PYTHON_COMPAT patches:

| Interface | Path | Use When |
|-----------|------|----------|
| Browse & modify | `.sys/python-compat/` | Package is cached, want to see current implementations |
| Direct patch file | `.sys/python-compat-patch/` | Package not cached, or prefer direct patch creation |

**Important:** The `.sys/python-compat/{package}/{version}/` directory listing requires package metadata to be cached. If the directory appears empty, use the patch file interface instead.

### Examples Using Browse Interface

**Remove incompatible Python version (if directory is populated):**
```bash
cd /var/db/repos/pypi/.sys/python-compat/dev-python/{package}/{version}/
rm python3_13
```

**Add Python version:**
```bash
touch python3_12
```

### Examples Using Patch File Interface (Always Works)

**Remove incompatible Python version:**
```bash
echo '-- python3_13' > /var/db/repos/pypi/.sys/python-compat-patch/dev-python/{package}/{version}.patch
```

**Set explicit compatible versions:**
```bash
echo '== python3_11 python3_12' > /var/db/repos/pypi/.sys/python-compat-patch/dev-python/{package}/{version}.patch
```

**Apply to all versions of a package:**
```bash
echo '-- python3_13' > /var/db/repos/pypi/.sys/python-compat-patch/dev-python/{package}/_all.patch
```

**Multiple operations in one patch:**
```bash
cat > /var/db/repos/pypi/.sys/python-compat-patch/dev-python/{package}/{version}.patch << 'EOF'
# Remove Python versions that fail to build
-- python3_11
-- python3_14
EOF
```

## IUSE Patches (USE Flags)

Use `.sys/iuse/` when a package needs additional USE flags that aren't auto-detected from PyPI metadata.

### Common Symptoms

- Package has optional features that require USE flags
- Build fails because it expects certain features to be disabled
- Need to integrate with system libraries instead of bundled ones

### Directory Structure

```
.sys/iuse/
    dev-python/
        {package}/
            {version}/
                embed_cares        # USE flag (file = flag exists)
            _all/
.sys/iuse-patch/
    dev-python/
        {package}/
            {version}.patch
            _all.patch
```

### Patch Format

```
++ embed_cares        # Add USE flag
-- test               # Remove USE flag
```

### Examples

**Add USE flags for gevent to use system libraries:**
```bash
# Add USE flags
touch /var/db/repos/pypi/.sys/iuse/dev-python/gevent/_all/embed_cares
touch /var/db/repos/pypi/.sys/iuse/dev-python/gevent/_all/embed_libev

# Verify
ls /var/db/repos/pypi/.sys/iuse/dev-python/gevent/_all/
cat /var/db/repos/pypi/dev-python/gevent/gevent-25.9.1.ebuild | grep IUSE
```

**Remove a USE flag:**
```bash
rm /var/db/repos/pypi/.sys/iuse/dev-python/gevent/_all/embed_cares
```

## Ebuild Phase Appending

Use `.sys/ebuild-append/` when you need to add custom code to ebuild phase functions (like `src_configure`, `src_compile`, etc.).

### Common Symptoms

- Package needs environment variables set during build
- Need to disable bundled libraries and use system versions
- Custom configure flags required
- Build system needs workarounds

### Directory Structure

```
.sys/ebuild-append/
    dev-python/
        {package}/
            {version}/
                src_configure      # Content appended to src_configure()
                src_compile        # Content appended to src_compile()
            _all/
.sys/ebuild-append-patch/
    dev-python/
        {package}/
            {version}.patch
            _all.patch
```

### Supported Phase Functions

| Phase | When It Runs |
|-------|--------------|
| `src_configure` | Before configure |
| `src_compile` | Before compile |
| `src_install` | Before install |
| `src_test` | Before tests |

### Examples

**Configure gevent to use system c-ares and libev:**
```bash
# Create src_configure with shell redirection
echo 'export GEVENTSETUP_EMBED_CARES=0' > \
  /var/db/repos/pypi/.sys/ebuild-append/dev-python/gevent/_all/src_configure
echo 'export GEVENTSETUP_EMBED_LIBEV=0' >> \
  /var/db/repos/pypi/.sys/ebuild-append/dev-python/gevent/_all/src_configure
echo 'distutils-r1_src_configure' >> \
  /var/db/repos/pypi/.sys/ebuild-append/dev-python/gevent/_all/src_configure

# Verify
cat /var/db/repos/pypi/.sys/ebuild-append/dev-python/gevent/_all/src_configure
cat /var/db/repos/pypi/dev-python/gevent/gevent-25.9.1.ebuild | grep -A10 'src_configure'
```

Note: Use `>` to overwrite and `>>` to append. The append operator works correctly to build up multi-line phase functions.

### Patch File Format

```
[src_configure]
export GEVENTSETUP_EMBED_CARES=0
export GEVENTSETUP_EMBED_LIBEV=0
distutils-r1_src_configure

[src_compile]
# Custom compile commands here
```

## Build-Time Dependencies (DEPEND)

Use `.sys/DEPEND/` when a package needs build-time dependencies (headers, libraries for compilation).

### Common Symptoms

- Compiler errors about missing headers
- Linker errors about missing libraries
- Package bundles a library but you want to use the system version

### Directory Structure

```
.sys/DEPEND/
    dev-python/
        {package}/
            {version}/
                net-dns::c-ares    # Build-time dependency
            _all/
```

### Examples

**Add build dependencies for gevent:**
```bash
# gevent needs c-ares and libev headers to build against system libraries
touch '/var/db/repos/pypi/.sys/DEPEND/dev-python/gevent/_all/net-dns::c-ares'
touch '/var/db/repos/pypi/.sys/DEPEND/dev-python/gevent/_all/dev-libs::libev'

# Verify
cat /var/db/repos/pypi/dev-python/gevent/gevent-25.9.1.ebuild | grep -E '^DEPEND='
```

## Runtime Dependency Patches

Use `.sys/RDEPEND/` when dependency version constraints cause conflicts.

### Common Symptoms

- `emerge` reports slot conflicts
- Package requires exact version that conflicts with system
- Missing dependency not declared by upstream

### Patch Format

```
-> old_dep new_dep     # Modify dependency
-- dep_to_remove       # Remove dependency
++ new_dep             # Add dependency
```

### Examples

**Loosen version constraint:**
```bash
cd /var/db/repos/pypi/.sys/RDEPEND/dev-python/{package}/{version}/
mv '=dev-python::urllib3-1.26.0[${PYTHON_USEDEP}]' \
   '>=dev-python::urllib3-1.26.0[${PYTHON_USEDEP}]'
```

**Remove dependency:**
```bash
rm '=dev-python::unwanted-1.0[${PYTHON_USEDEP}]'
```

**Add missing dependency:**
```bash
touch '>=dev-python::missing-dep-1.0[${PYTHON_USEDEP}]'
```

## Example: psycopg2-2.9.5 Python 3.13 Failure

### Error Output
```
psycopg/utils.c:397:12: error: implicit declaration of function '_PyInterpreterState_Get'
error: command '/usr/bin/x86_64-pc-linux-gnu-gcc' failed with exit code 1
 * ERROR: dev-python/psycopg-2.9.5::portage-pip-fuse failed (compile phase):
```

### Analysis

- **Package**: psycopg-2.9.5
- **Python version**: 3.13 (from build path)
- **Error type**: Python C API incompatibility
- **Cause**: `_PyInterpreterState_Get()` was removed in Python 3.13

### Fix

```bash
# Remove Python 3.13 from this version
cd /var/db/repos/pypi/.sys/python-compat/dev-python/psycopg/2.9.5/
echo '-- python3_13' > patch

# Or apply to all versions before 2.9.9 (when Python 3.13 support was added)
cd /var/db/repos/pypi/.sys/python-compat/dev-python/psycopg/_all/
echo '-- python3_13' > patch
```

Then re-run:
```bash
emerge dev-python/psycopg
```

## Example: gevent Multi-Python Build Cache Conflict

### Error Output
```
configure: error: `CFLAGS' has changed since the previous run:
configure:   former value:  `-Wsign-compare -DNDEBUG -O2 -march=znver4 -mfpmath=sse'
configure:   current value: `-fno-strict-overflow -Wsign-compare -DNDEBUG -O2 -march=znver4 -mfpmath=sse'
configure: error: `PKG_CONFIG_PATH' has changed since the previous run:
configure:   former value:  `/var/tmp/portage/.../python3.11/pkgconfig'
configure:   current value: `/var/tmp/portage/.../python3.12/pkgconfig'
configure: error: changes in the environment can compromise the build
 * ERROR: dev-python/gevent-25.9.1::portage-pip-fuse failed (compile phase):
```

### Analysis

- **Package**: gevent-25.9.1
- **Error type**: Autotools configure cache conflict
- **Cause**: gevent bundles c-ares which uses autotools with caching (`-C` flag). When building for multiple Python versions sequentially, the cache from python3.11 conflicts with python3.12's different environment variables.

### Fix

Reduce PYTHON_COMPAT to a single Python version to avoid the cache conflict:

```bash
# Build only for python3_12
echo '== python3_12' > /var/db/repos/pypi/.sys/python-compat-patch/dev-python/gevent/25.9.1.patch
```

Or remove just the problematic version:

```bash
# Remove python3_11, keep others
echo '-- python3_11' > /var/db/repos/pypi/.sys/python-compat-patch/dev-python/gevent/25.9.1.patch
```

Then re-run:
```bash
emerge dev-python/gevent
```

**Note:** This is a workaround for packages with bundled native code that doesn't clean build artifacts between Python version builds. The proper upstream fix would be to run `make distclean` or `rm config.cache` between builds.

## Example: Complete gevent Setup with System Libraries

This example shows how to configure gevent to build against system c-ares and libev instead of bundled copies, which avoids the autotools cache conflicts.

### Problem

gevent bundles c-ares and libev, which causes:
1. Autotools cache conflicts when building for multiple Python versions
2. Larger package size from bundled libraries
3. Potential security issues from outdated bundled code

### Solution: Use System Libraries

```bash
# 1. Add USE flags for the optional features
touch /var/db/repos/pypi/.sys/iuse/dev-python/gevent/_all/embed_cares
touch /var/db/repos/pypi/.sys/iuse/dev-python/gevent/_all/embed_libev

# 2. Add build-time dependencies
touch '/var/db/repos/pypi/.sys/DEPEND/dev-python/gevent/_all/net-dns::c-ares'
touch '/var/db/repos/pypi/.sys/DEPEND/dev-python/gevent/_all/dev-libs::libev'

# 3. Add runtime dependencies
touch '/var/db/repos/pypi/.sys/RDEPEND/dev-python/gevent/_all/net-dns::c-ares'
touch '/var/db/repos/pypi/.sys/RDEPEND/dev-python/gevent/_all/dev-libs::libev'

# 4. Configure build to use system libraries
echo 'export GEVENTSETUP_EMBED_CARES=0' > \
  /var/db/repos/pypi/.sys/ebuild-append/dev-python/gevent/_all/src_configure
echo 'export GEVENTSETUP_EMBED_LIBEV=0' >> \
  /var/db/repos/pypi/.sys/ebuild-append/dev-python/gevent/_all/src_configure
echo 'distutils-r1_src_configure' >> \
  /var/db/repos/pypi/.sys/ebuild-append/dev-python/gevent/_all/src_configure

# 5. Verify the generated ebuild
cat /var/db/repos/pypi/dev-python/gevent/gevent-25.9.1.ebuild

# 6. Install
emerge dev-python/gevent
```

### Generated ebuild will contain:

```bash
IUSE="embed_cares embed_libev"

DEPEND="
    net-dns/c-ares
    dev-libs/libev
"

RDEPEND="
    net-dns/c-ares
    dev-libs/libev
    ...
"

src_configure() {
    export GEVENTSETUP_EMBED_CARES=0
    export GEVENTSETUP_EMBED_LIBEV=0
    distutils-r1_src_configure
}
```

## Verifying Patches

After applying patches, verify they took effect:

```bash
# Check PYTHON_COMPAT in generated ebuild
grep PYTHON_COMPAT /var/db/repos/pypi/dev-python/psycopg/psycopg-2.9.5.ebuild

# Check dependencies
grep RDEPEND /var/db/repos/pypi/dev-python/requests/requests-2.31.0.ebuild

# Test with emerge
emerge -pv dev-python/psycopg
```

## Persistence

Patches are stored in `~/.config/portage-pip-fuse/patches.json` and persist across remounts.

To export patches for sharing:
```bash
cat /var/db/repos/pypi/.sys/python-compat-patch/dev-python/psycopg/2.9.5.patch
```

## Masking Incompatible Versions

Sometimes the best fix is to mask old package versions and use a newer release that has the fix upstream. This is especially useful for:

- Build tool incompatibilities (e.g., Cython 3.x breaking older packages)
- Packages with known security vulnerabilities
- Versions that predate Python version support

### Example: gevent Cython 3.x Incompatibility

#### Error Output
```
src/gevent/libev/corecext.pyx:69:26: undeclared name not builtin: long
Cython.Compiler.Errors.CompileError: src/gevent/libev/corecext.pyx
 * ERROR: dev-python/gevent-24.2.1::portage-pip-fuse failed (compile phase):
```

#### Analysis

- **Package**: gevent-24.2.1
- **Error type**: Cython 3.x incompatibility
- **Cause**: Cython 3 rejects Python 2 syntax (`long`) even in unreachable code branches. gevent 24.10.1+ includes Cython 3 compatibility fixes.

#### Fix

Mask old versions in `/etc/portage/package.mask/gevent`:
```
# Cython 3.x incompatibility - use >=24.10.1
<dev-python/gevent-24.10.1
```

Then re-run:
```bash
emerge dev-python/gevent
```

### When to Mask vs Patch

| Situation | Recommended Approach |
|-----------|---------------------|
| Newer version has the fix | Mask old versions |
| No fixed version available | Use `.sys/` patches |
| Need specific old version | Use `.sys/` patches |
| Build tool incompatibility | Mask old versions |
| Python version incompatibility | Either approach works |

## Common Python API Changes by Version

| Python Version | Common Breaking Changes |
|---------------|------------------------|
| 3.13 | `_PyInterpreterState_Get` removed, PEP 703 changes |
| 3.12 | `distutils` removed, `imp` module removed |
| 3.11 | `inspect.getargspec` removed |
| 3.10 | `collections.abc` moves |

When a package fails with API errors for a new Python version, check if a newer package version exists that supports it, or patch out the incompatible Python version.

## Multi-Python Build Issues

Some packages fail when building for multiple Python versions due to build system limitations:

| Symptom | Cause | Fix |
|---------|-------|-----|
| `configure: error: CFLAGS has changed` | Autotools cache conflict between Python builds | Reduce to single Python version |
| `configure: error: PKG_CONFIG_PATH has changed` | Same as above | Reduce to single Python version |
| Build artifacts from previous Python version | Build system doesn't clean between versions | Reduce to single Python version |

These issues occur when packages bundle native libraries (like c-ares, libev) that use autotools with caching. The build runs sequentially for each Python version, but cached configuration from python3.11 conflicts with python3.12's different environment.

**Workaround:** Use `== python3_12` to build for only one Python version until the package is fixed upstream.
