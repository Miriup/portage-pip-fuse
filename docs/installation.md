# Installation and Setup

This guide covers the installation and configuration of portage-pip-fuse.

## System Requirements

- **Operating System**: Gentoo Linux
- **Python**: 3.8 or later 
- **FUSE**: Filesystem in Userspace support
- **Permissions**: Access to `/etc/fuse.conf` for multi-user access

## FUSE Configuration

For the filesystem to be accessible by the `portage` user (required for `emerge` to work), you must enable user access to the `allow_other` FUSE option.

### Enable user_allow_other

Edit `/etc/fuse.conf` and uncomment the `user_allow_other` line:

```bash
# Edit /etc/fuse.conf
sudo nano /etc/fuse.conf
```

Ensure this line is present and uncommented:
```
user_allow_other
```

This allows non-root users to mount FUSE filesystems that other users (including `portage`) can access.

## Repository Configuration

Create the portage repository configuration:

```bash
# Create repository config
sudo tee /etc/portage/repos.conf/pypi.conf << EOF
[portage-pip-fuse]
location = /var/db/repos/pypi
auto-sync = no
EOF
```

**Note**: The section name `[portage-pip-fuse]` must match the repository name set by the FUSE filesystem.

## Directory Setup

Create the mount point:

```bash
# Create mount point
sudo mkdir -p /var/db/repos/pypi
sudo chown $(id -u):$(id -g) /var/db/repos/pypi
```

## Mounting the Filesystem

Start the FUSE filesystem:

```bash
# Basic mount
python -m portage_pip_fuse.cli /var/db/repos/pypi

# With debugging
python -m portage_pip_fuse.cli /var/db/repos/pypi --debug --logfile=debug.log

# Background mount
python -m portage_pip_fuse.cli /var/db/repos/pypi &
```

## Verification

Verify the repository is recognized by portage:

```bash
# Check repository list
emerge --info | grep -A 5 portage-pip-fuse

# Test package availability
emerge -p dev-python/requests::portage-pip-fuse
```

## Troubleshooting

### Permission Denied Errors

If `emerge` shows "no ebuilds to satisfy" or permission errors:

1. **Check FUSE mount permissions**:
   ```bash
   mount | grep pypi
   # Should show: allow_other in mount options
   ```

2. **Verify /etc/fuse.conf**:
   ```bash
   grep "user_allow_other" /etc/fuse.conf
   # Should return: user_allow_other (uncommented)
   ```

3. **Check repository visibility**:
   ```bash
   # As portage user
   sudo -u portage ls /var/db/repos/pypi/dev-python/
   ```

### Repository Not Found

If portage doesn't see the repository:

1. **Check repository config**:
   ```bash
   cat /etc/portage/repos.conf/pypi.conf
   ```

2. **Verify filesystem structure**:
   ```bash
   ls -la /var/db/repos/pypi/
   cat /var/db/repos/pypi/profiles/repo_name
   ```

3. **Check FUSE mount status**:
   ```bash
   mount | grep pypi
   ps aux | grep portage_pip_fuse
   ```

### Mount Fails with "Operation not permitted"

This usually means `user_allow_other` is not enabled in `/etc/fuse.conf`. Follow the FUSE Configuration steps above.

## Performance Tuning

For better performance with large package sets:

```bash
# Disable timestamp lookups
python -m portage_pip_fuse.cli /var/db/repos/pypi --no-timestamps

# Increase cache TTL
python -m portage_pip_fuse.cli /var/db/repos/pypi --cache-ttl 7200

# Use specific filters
python -m portage_pip_fuse.cli /var/db/repos/pypi --filter=curated
```

## Unmounting

To unmount the filesystem:

```bash
# Unmount
fusermount -u /var/db/repos/pypi

# Or kill the process
pkill -f portage_pip_fuse
```