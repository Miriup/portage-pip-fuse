# Fixing Build Errors with .sys/ Patches

When packages fail to build, you can use the `.sys/` virtual filesystem to patch Python compatibility or dependencies without modifying the generated ebuilds directly.

## Quick Reference

| Error Type | Solution Directory |
|------------|-------------------|
| Python API incompatibility | `.sys/python-compat/` |
| Missing/wrong Python version | `.sys/python-compat/` |
| Dependency version conflict | `.sys/dependencies/` |
| Missing dependency | `.sys/dependencies/` |

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

## Dependency Patches

Use `.sys/dependencies/` when dependency version constraints cause conflicts.

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
cd /var/db/repos/pypi/.sys/dependencies/dev-python/{package}/{version}/
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
