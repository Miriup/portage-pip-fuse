#!/usr/bin/env python3
"""
Command-line interface for portage-pip-fuse.

This module provides the main CLI entry point for mounting and managing
the PyPI-to-Gentoo FUSE filesystem.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import argparse
import logging
import os
import sys
import signal
from pathlib import Path

from portage_pip_fuse.filesystem import mount_filesystem, PortagePipFS
from portage_pip_fuse.package_filter import FilterRegistry
from portage_pip_fuse.sqlite_metadata import SQLiteMetadataBackend
from portage_pip_fuse.constants import REPO_NAME, REPO_LOCATION, find_cache_dir


def signal_handler(signum, frame):
    """Handle termination signals gracefully."""
    print(f"\nReceived signal {signum}, shutting down...")
    # Exit cleanly - FUSE will handle unmounting
    os._exit(0)


def validate_mountpoint(path: str) -> Path:
    """Validate mountpoint (must exist, will not be created)."""
    mountpoint = Path(path).resolve()

    # Check if mountpoint exists - do NOT create it
    if not mountpoint.exists():
        print(f"Error: Mountpoint does not exist: {mountpoint}")
        print(f"Create it first with:")
        print(f"  sudo mkdir -p {mountpoint}")
        print(f"  sudo chown $(id -u):$(id -g) {mountpoint}")
        sys.exit(1)

    # Check if it's a directory
    if not mountpoint.is_dir():
        print(f"Error: {mountpoint} is not a directory")
        sys.exit(1)
    
    # Check if directory is empty
    try:
        if any(mountpoint.iterdir()):
            print(f"Warning: {mountpoint} is not empty")
            response = input("Continue anyway? [y/N]: ")
            if response.lower() not in ['y', 'yes']:
                sys.exit(0)
    except PermissionError:
        print(f"Error: Permission denied accessing {mountpoint}")
        sys.exit(1)
    
    return mountpoint


def check_fuse_availability():
    """Check if FUSE is available on the system."""
    try:
        import fuse
    except ImportError:
        print("Error: Python FUSE library not found")
        print("Install with: pip install fusepy")
        sys.exit(1)
    
    # Check if /dev/fuse exists
    if not os.path.exists('/dev/fuse'):
        print("Error: FUSE not available on this system")
        print("Make sure FUSE kernel module is loaded: modprobe fuse")
        sys.exit(1)


def install_command():
    """Handle install subcommand - creates repos.conf file."""
    install_parser = argparse.ArgumentParser(
        prog='portage-pip-fuse install',
        description='Create portage repos.conf file for the FUSE overlay'
    )

    install_parser.add_argument(
        'mountpoint',
        nargs='?',
        default=REPO_LOCATION,
        help=f'Directory where the filesystem will be mounted (default: {REPO_LOCATION})'
    )

    install_parser.add_argument(
        '--priority',
        type=int,
        default=50,
        help='Repository priority (default: 50)'
    )

    # Remove 'install' from argv and parse remaining args
    install_argv = [arg for arg in sys.argv[1:] if arg != 'install']
    args = install_parser.parse_args(install_argv)

    mountpoint = Path(args.mountpoint).resolve()
    repos_conf_dir = Path('/etc/portage/repos.conf')
    conf_file = repos_conf_dir / f'{REPO_NAME}.conf'

    conf_content = f"""[{REPO_NAME}]
location = {mountpoint}
sync-type =
auto-sync = no
priority = {args.priority}
"""

    # Check if repos.conf directory exists
    if not repos_conf_dir.exists():
        print(f"Error: {repos_conf_dir} does not exist")
        return 1

    # Check if file already exists
    if conf_file.exists():
        print(f"Warning: {conf_file} already exists")
        response = input("Overwrite? [y/N]: ")
        if response.lower() not in ['y', 'yes']:
            print("Aborted")
            return 0

    try:
        conf_file.write_text(conf_content)
        print(f"Created {conf_file}")
        print(f"\nTo use the overlay:")
        print(f"  1. Mount the filesystem: portage-pip-fuse {mountpoint}")
        print(f"  2. Emerge packages: emerge -av dev-python/requests")
        return 0
    except PermissionError:
        print(f"Error: Permission denied writing to {conf_file}")
        print("Try running with sudo")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


def sync_command():
    """Handle sync subcommand."""
    sync_parser = argparse.ArgumentParser(
        prog='portage-pip-fuse sync',
        description='Sync PyPI metadata database with latest data'
    )
    
    sync_parser.add_argument(
        '--cache-dir',
        type=str,
        help='Cache directory for PyPI metadata database'
    )
    
    sync_parser.add_argument(
        '-d', '--debug',
        action='store_true',
        help='Enable debug output'
    )
    
    # Remove 'sync' from argv and parse remaining args
    sync_argv = [arg for arg in sys.argv[1:] if arg != 'sync']
    args = sync_parser.parse_args(sync_argv)
    
    # Set up logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level, format='%(levelname)s: %(message)s')
    
    # Create SQLite backend and sync
    try:
        cache_dir = find_cache_dir(args.cache_dir)
        backend = SQLiteMetadataBackend(cache_dir=cache_dir)
        
        success = backend.sync_database()
        if success:
            print("✓ Database sync completed successfully")
            return 0
        else:
            print("✗ Database sync failed")
            return 1
            
    except Exception as e:
        print(f"✗ Error during sync: {e}")
        return 1


def unsync_command():
    """Handle unsync subcommand - delete the database."""
    unsync_parser = argparse.ArgumentParser(
        prog='portage-pip-fuse unsync',
        description='Delete the PyPI metadata database'
    )

    unsync_parser.add_argument(
        '--cache-dir',
        type=str,
        help='Cache directory for PyPI metadata database'
    )

    unsync_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Delete without confirmation'
    )

    # Remove 'unsync' from argv and parse remaining args
    unsync_argv = [arg for arg in sys.argv[1:] if arg != 'unsync']
    args = unsync_parser.parse_args(unsync_argv)

    # Determine cache directory
    cache_dir = find_cache_dir(args.cache_dir)

    db_path = cache_dir / 'pypi-data.sqlite'
    gz_path = cache_dir / 'pypi-data.sqlite.gz'
    download_path = cache_dir / 'pypi-data.sqlite.gz.__download__'

    # Check what exists
    files_to_delete = []
    if db_path.exists():
        files_to_delete.append(db_path)
    if gz_path.exists():
        files_to_delete.append(gz_path)
    if download_path.exists():
        files_to_delete.append(download_path)

    if not files_to_delete:
        print("No database files found to delete")
        return 0

    # Show what will be deleted
    print("The following files will be deleted:")
    total_size = 0
    for f in files_to_delete:
        size = f.stat().st_size
        total_size += size
        print(f"  {f} ({size / (1024*1024):.1f} MB)")
    print(f"Total: {total_size / (1024*1024):.1f} MB")

    # Confirm deletion
    if not args.force:
        response = input("Delete these files? [y/N]: ")
        if response.lower() not in ['y', 'yes']:
            print("Aborted")
            return 0

    # Delete files
    for f in files_to_delete:
        try:
            f.unlink()
            print(f"✓ Deleted {f}")
        except Exception as e:
            print(f"✗ Failed to delete {f}: {e}")
            return 1

    print("✓ Database deleted successfully")
    return 0


def unmount_command():
    """Handle unmount subcommand."""
    unmount_parser = argparse.ArgumentParser(
        prog='portage-pip-fuse unmount',
        description='Unmount the PyPI FUSE filesystem'
    )

    unmount_parser.add_argument(
        'mountpoint',
        nargs='?',
        default=REPO_LOCATION,
        help=f'Directory where the filesystem is mounted (default: {REPO_LOCATION})'
    )

    unmount_parser.add_argument(
        '--pid-file',
        type=str,
        help='PID file to read process ID from (sends SIGINT instead of using fusermount)'
    )

    # Remove 'unmount' from argv and parse remaining args
    unmount_argv = [arg for arg in sys.argv[1:] if arg != 'unmount']
    args = unmount_parser.parse_args(unmount_argv)

    mountpoint = Path(args.mountpoint).resolve()

    # Check if it's mounted
    if not mountpoint.exists():
        print(f"Error: {mountpoint} does not exist")
        return 1

    # If PID file specified, use SIGINT
    if args.pid_file:
        pid_path = Path(args.pid_file)
        if not pid_path.exists():
            print(f"Error: PID file not found: {pid_path}")
            return 1

        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGINT)
            print(f"✓ Sent SIGINT to process {pid}")
            # Clean up PID file
            pid_path.unlink(missing_ok=True)
            return 0
        except ValueError:
            print(f"Error: Invalid PID in {pid_path}")
            return 1
        except ProcessLookupError:
            print(f"Error: Process {pid} not found (stale PID file?)")
            pid_path.unlink(missing_ok=True)
            return 1
        except PermissionError:
            print(f"Error: Permission denied sending signal to process {pid}")
            return 1
        except Exception as e:
            print(f"Error: {e}")
            return 1

    # Fall back to fusermount
    import subprocess
    try:
        result = subprocess.run(
            ['fusermount', '-u', str(mountpoint)],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print(f"✓ Unmounted {mountpoint}")
            return 0
        else:
            # Check if it's not mounted
            if 'not mounted' in result.stderr or 'not found' in result.stderr:
                print(f"Error: {mountpoint} is not mounted")
            else:
                print(f"Error: {result.stderr.strip()}")
            return 1
    except FileNotFoundError:
        print("Error: fusermount not found. Install fuse-utils.")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


def mount_command():
    """Handle mount subcommand."""
    mount_parser = argparse.ArgumentParser(
        prog='portage-pip-fuse mount',
        description='Mount the PyPI FUSE filesystem',
        epilog=f'''
Examples:
  %(prog)s                                     # Mount at default location ({REPO_LOCATION})
  %(prog)s /mnt/pypi                           # Mount at custom location
  %(prog)s -f                                  # Mount in foreground
  %(prog)s -f -d                               # Mount with debug output

After mounting, you can:
  ls {REPO_LOCATION}/dev-python/requests
  cat {REPO_LOCATION}/dev-python/requests/requests-2.28.1.ebuild

To unmount:
  fusermount -u {REPO_LOCATION}
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    mount_parser.add_argument(
        'mountpoint',
        nargs='?',
        default=REPO_LOCATION,
        help=f'Directory where the filesystem will be mounted (default: {REPO_LOCATION})'
    )
    
    mount_parser.add_argument(
        '-f', '--foreground',
        action='store_true',
        help='Run in foreground instead of daemonizing'
    )

    mount_parser.add_argument(
        '-d', '--debug',
        action='store_true',
        help='Enable debug output'
    )

    mount_parser.add_argument(
        '--logfile',
        type=str,
        help='Log file path for debug output (default: stderr)'
    )

    mount_parser.add_argument(
        '--cache-ttl',
        type=int,
        default=3600,
        help='Cache time-to-live in seconds (default: 3600)'
    )

    mount_parser.add_argument(
        '--cache-dir',
        type=str,
        help='Cache directory for PyPI metadata (default: /tmp/portage-pip-fuse-cache)'
    )

    mount_parser.add_argument(
        '--pid-file',
        type=str,
        help='Write process ID to this file (for use with unmount --pid-file)'
    )

    # Get available filters from registry
    available_filters = list(FilterRegistry.get_all_filters().keys())

    # Filter configuration arguments
    mount_parser.add_argument(
        '--filter',
        type=str,
        action='append',
        choices=available_filters,
        help=f'Add package filter (available: {", ".join(available_filters)}). Can be used multiple times.'
    )

    mount_parser.add_argument(
        '--no-filter',
        type=str,
        action='append',
        choices=available_filters,
        help=f'Disable specific filter (available: {", ".join(available_filters)}). Can be used multiple times.'
    )

    mount_parser.add_argument(
        '--deps-for',
        type=str,
        action='append',
        help='Show dependency tree for specified packages (use with --filter=deps)'
    )

    mount_parser.add_argument(
        '--use-flags',
        type=str,
        help='Comma-separated Python extras/USE flags for dependency resolution'
    )

    mount_parser.add_argument(
        '--filter-days',
        type=int,
        default=30,
        help='Days to look back for recent packages (default: 30)'
    )

    mount_parser.add_argument(
        '--filter-count',
        type=int,
        default=100,
        help='Number of newest packages to show (default: 100)'
    )

    mount_parser.add_argument(
        '--no-timestamps',
        action='store_true',
        help='Disable PyPI timestamp lookup for faster performance (uses current time for all files)'
    )

    mount_parser.add_argument(
        '--test',
        action='store_true',
        help='Run filesystem tests without mounting'
    )

    mount_parser.add_argument(
        '--use-sqlite',
        action='store_true',
        default=True,
        help='Use SQLite backend with PyPI JSON API fallback (default: enabled)'
    )

    mount_parser.add_argument(
        '--no-sqlite',
        action='store_true',
        help='Disable SQLite backend and use only PyPI JSON API'
    )

    # Remove 'mount' from argv and parse remaining args
    mount_argv = [arg for arg in sys.argv[1:] if arg != 'mount']
    args = mount_parser.parse_args(mount_argv)

    # Resolve cache directory
    cache_dir = find_cache_dir(args.cache_dir)

    # Build active filter list
    active_filters = set(FilterRegistry.get_default_filters())
    
    # Add explicitly requested filters
    if args.filter:
        active_filters.update(args.filter)
    
    # Remove explicitly disabled filters
    if args.no_filter:
        active_filters.difference_update(args.no_filter)
    
    # Validate filter configuration
    if 'deps' in active_filters and not args.deps_for:
        mount_parser.error("Filter 'deps' requires --deps-for to specify packages")
    
    # Parse USE flags if provided
    use_flags = []
    if args.use_flags:
        use_flags = [flag.strip() for flag in args.use_flags.split(',')]
    
    # Set up logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    # Configure logging with optional file output
    if args.logfile:
        # Validate logfile path
        logfile_path = Path(args.logfile).resolve()
        
        # Create log directory if needed
        try:
            logfile_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            print(f"Error: Cannot create log directory {logfile_path.parent}")
            return 1
        
        # Set up file logging
        logging.basicConfig(
            level=log_level,
            format=log_format,
            filename=str(logfile_path),
            filemode='a'  # Append mode
        )
        
        # Also add console output for important messages
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.WARNING)
        console_formatter = logging.Formatter('%(levelname)s: %(message)s')
        console_handler.setFormatter(console_formatter)
        logging.getLogger().addHandler(console_handler)
        
        print(f"Logging to file: {logfile_path}")
        
    else:
        # Standard logging to stderr
        logging.basicConfig(
            level=log_level,
            format=log_format
        )
    
    logger = logging.getLogger(__name__)
    
    # Determine backend configuration
    use_sqlite = args.use_sqlite and not args.no_sqlite
    
    # Build filter configuration dictionary
    filter_config = {
        'active_filters': list(active_filters),
        'deps_for': args.deps_for or [],
        'use_flags': use_flags,
        'days': args.filter_days,
        'count': args.filter_count,
        'no_timestamps': args.no_timestamps,
        'use_sqlite': use_sqlite
    }
    
    if args.test:
        # Run tests
        print("Running portage-pip-fuse tests...")
        try:
            fs = PortagePipFS(cache_ttl=args.cache_ttl, cache_dir=str(cache_dir), filter_config=filter_config)
            print("✓ Filesystem initialization successful")
            
            # Test path parsing
            test_paths = [
                "/dev-python/requests",
                "/dev-python/requests/requests-2.28.1.ebuild",
                "/profiles/repo_name"
            ]
            
            for path in test_paths:
                parsed = fs._parse_path(path)
                print(f"✓ Path parsing: {path} -> {parsed['type']}")
            
            print("All tests passed!")
            return 0
            
        except Exception as e:
            print(f"✗ Test failed: {e}")
            return 1
    
    # Check system requirements only if not testing
    if not args.test:
        check_fuse_availability()
        
        # Validate mountpoint
        mountpoint = validate_mountpoint(args.mountpoint)
    else:
        mountpoint = None
    
    # Set up signal handlers
    pid_file_path = Path(args.pid_file) if args.pid_file else None

    def cleanup_pid_file():
        if pid_file_path and pid_file_path.exists():
            try:
                pid_file_path.unlink()
            except Exception:
                pass

    def signal_handler_with_cleanup(signum, frame):
        cleanup_pid_file()
        signal_handler(signum, frame)

    signal.signal(signal.SIGINT, signal_handler_with_cleanup)
    signal.signal(signal.SIGTERM, signal_handler_with_cleanup)

    # Write PID file if requested
    if pid_file_path:
        try:
            pid_file_path.parent.mkdir(parents=True, exist_ok=True)
            pid_file_path.write_text(str(os.getpid()))
        except Exception as e:
            print(f"Error: Failed to write PID file: {e}")
            return 1

    print(f"Mounting portage-pip FUSE filesystem at {mountpoint}")
    print(f"Cache directory: {cache_dir}")
    print(f"Cache TTL: {args.cache_ttl} seconds")
    print(f"Backend: {'SQLite + JSON API fallback' if use_sqlite else 'JSON API only'}")
    print(f"Active filters: {', '.join(active_filters) if active_filters else 'none'}")

    if 'deps' in active_filters and args.deps_for:
        print(f"Showing dependencies for: {', '.join(args.deps_for)}")
        if use_flags:
            print(f"With USE flags: {', '.join(use_flags)}")

    if args.no_timestamps:
        print("Timestamps disabled for faster performance")

    if pid_file_path:
        print(f"PID file: {pid_file_path}")

    if args.foreground:
        print("Running in foreground (Ctrl+C to unmount)")
    else:
        print("Running in background")
        if pid_file_path:
            print(f"To unmount: portage-pip-fuse unmount --pid-file {pid_file_path}")
        else:
            print(f"To unmount: fusermount -u {mountpoint}")
    
    try:
        mount_filesystem(
            str(mountpoint),
            foreground=args.foreground,
            debug=args.debug,
            cache_ttl=args.cache_ttl,
            cache_dir=str(cache_dir),
            filter_config=filter_config
        )
    except KeyboardInterrupt:
        print("\nUnmounting...")
    except PermissionError:
        print("Error: Permission denied")
        print("Try running with sudo or check FUSE permissions")
        cleanup_pid_file()
        return 1
    except Exception as e:
        logger.error(f"Mount failed: {e}")
        cleanup_pid_file()
        return 1

    cleanup_pid_file()
    return 0


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog='portage-pip-fuse',
        description='FUSE filesystem that bridges PyPI packages to Gentoo portage',
        epilog=f'''
Subcommands:
  mount     Mount the PyPI FUSE filesystem
  unmount   Unmount the PyPI FUSE filesystem
  install   Create /etc/portage/repos.conf entry for the overlay
  sync      Sync PyPI metadata database with latest data
  unsync    Delete the PyPI metadata database

Examples:
  %(prog)s mount                               # Mount at default location ({REPO_LOCATION})
  %(prog)s mount /mnt/pypi                     # Mount at custom location
  %(prog)s unmount                             # Unmount from default location
  %(prog)s install                             # Create repos.conf file
  %(prog)s sync                                # Sync PyPI database
  %(prog)s unsync                              # Delete the database

For subcommand help:
  %(prog)s <subcommand> --help
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        'subcommand',
        nargs='?',
        choices=['mount', 'unmount', 'install', 'sync', 'unsync'],
        help='Subcommand to run'
    )

    parser.add_argument(
        '--version',
        action='version',
        version='%(prog)s 0.1.0'
    )

    # Parse only the first argument to determine subcommand
    # If no args or help requested, show help
    if len(sys.argv) < 2 or sys.argv[1] in ['-h', '--help']:
        parser.print_help()
        return 0

    if sys.argv[1] == '--version':
        print('portage-pip-fuse 0.1.0')
        return 0

    subcommand = sys.argv[1]

    if subcommand == 'mount':
        return mount_command()
    elif subcommand == 'unmount':
        return unmount_command()
    elif subcommand == 'install':
        return install_command()
    elif subcommand == 'sync':
        return sync_command()
    elif subcommand == 'unsync':
        return unsync_command()
    else:
        print(f"Unknown subcommand: {subcommand}")
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())