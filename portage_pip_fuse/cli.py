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


def signal_handler(signum, frame):
    """Handle termination signals gracefully."""
    print(f"\nReceived signal {signum}, shutting down...")
    # Exit cleanly - FUSE will handle unmounting
    os._exit(0)


def validate_mountpoint(path: str) -> Path:
    """Validate and prepare mountpoint."""
    mountpoint = Path(path).resolve()
    
    # Check if mountpoint exists
    if not mountpoint.exists():
        print(f"Creating mountpoint: {mountpoint}")
        try:
            mountpoint.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            print(f"Error: Permission denied creating {mountpoint}")
            print("Try running with sudo or choose a different location")
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
        cache_dir = Path(args.cache_dir) if args.cache_dir else None
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


def main():
    """Main CLI entry point."""
    # Handle subcommands - check for sync anywhere in the args
    if 'sync' in sys.argv:
        return sync_command()
        
    parser = argparse.ArgumentParser(
        prog='portage-pip-fuse',
        description='Mount a FUSE filesystem that bridges PyPI packages to Gentoo portage',
        epilog='''
Examples:
  %(prog)s /mnt/pypi                           # Mount with defaults
  %(prog)s /mnt/pypi -f                        # Mount in foreground
  %(prog)s /mnt/pypi -f -d                     # Mount with debug output
  %(prog)s /mnt/pypi -d --logfile /var/log/pypi.log  # Debug to logfile
  %(prog)s /mnt/pypi --cache-ttl 600           # Mount with 10-minute cache
  %(prog)s sync                                # Sync PyPI database
  %(prog)s sync --cache-dir ~/.cache/pypi      # Sync to custom directory
  
After mounting, you can:
  ls /mnt/pypi/dev-python/requests
  cat /mnt/pypi/dev-python/requests/requests-2.28.1.ebuild
  
To unmount:
  fusermount -u /mnt/pypi
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument(
        'mountpoint',
        nargs='?',  # Make mountpoint optional for test mode
        help='Directory where the filesystem will be mounted'
    )
    
    parser.add_argument(
        '-f', '--foreground',
        action='store_true',
        help='Run in foreground instead of daemonizing'
    )
    
    parser.add_argument(
        '-d', '--debug',
        action='store_true',
        help='Enable debug output'
    )
    
    parser.add_argument(
        '--logfile',
        type=str,
        help='Log file path for debug output (default: stderr)'
    )
    
    parser.add_argument(
        '--cache-ttl',
        type=int,
        default=3600,
        help='Cache time-to-live in seconds (default: 3600)'
    )
    
    parser.add_argument(
        '--cache-dir',
        type=str,
        help='Cache directory for PyPI metadata (default: /tmp/portage-pip-fuse-cache)'
    )
    
    # Get available filters from registry
    available_filters = list(FilterRegistry.get_all_filters().keys())
    default_filters = FilterRegistry.get_default_filters()
    
    # Filter configuration arguments
    parser.add_argument(
        '--filter',
        type=str,
        action='append',
        choices=available_filters,
        help=f'Add package filter (available: {", ".join(available_filters)}). Can be used multiple times.'
    )
    
    parser.add_argument(
        '--no-filter',
        type=str,
        action='append',
        choices=available_filters,
        help=f'Disable specific filter (available: {", ".join(available_filters)}). Can be used multiple times.'
    )
    
    parser.add_argument(
        '--deps-for',
        type=str,
        action='append',
        help='Show dependency tree for specified packages (use with --filter=deps)'
    )
    
    parser.add_argument(
        '--use-flags',
        type=str,
        help='Comma-separated Python extras/USE flags for dependency resolution'
    )
    
    parser.add_argument(
        '--filter-days',
        type=int,
        default=30,
        help='Days to look back for recent packages (default: 30)'
    )
    
    parser.add_argument(
        '--filter-count',
        type=int,
        default=100,
        help='Number of newest packages to show (default: 100)'
    )
    
    parser.add_argument(
        '--no-timestamps',
        action='store_true',
        help='Disable PyPI timestamp lookup for faster performance (uses current time for all files)'
    )
    
    parser.add_argument(
        '--test',
        action='store_true',
        help='Run filesystem tests without mounting'
    )
    
    parser.add_argument(
        '--use-sqlite',
        action='store_true',
        default=True,
        help='Use SQLite backend with PyPI JSON API fallback (default: enabled)'
    )
    
    parser.add_argument(
        '--no-sqlite',
        action='store_true',
        help='Disable SQLite backend and use only PyPI JSON API'
    )
    
    parser.add_argument(
        '--version',
        action='version',
        version='%(prog)s 0.1.0'
    )
    
    args = parser.parse_args()
    
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
        parser.error("Filter 'deps' requires --deps-for to specify packages")
    
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
            fs = PortagePipFS(cache_ttl=args.cache_ttl, cache_dir=args.cache_dir, filter_config=filter_config)
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
        if not args.mountpoint:
            parser.error("mountpoint is required when not using --test")
            
        check_fuse_availability()
        
        # Validate mountpoint
        mountpoint = validate_mountpoint(args.mountpoint)
    else:
        mountpoint = None
    
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    print(f"Mounting portage-pip FUSE filesystem at {mountpoint}")
    print(f"Cache TTL: {args.cache_ttl} seconds")
    print(f"Backend: {'SQLite + JSON API fallback' if use_sqlite else 'JSON API only'}")
    print(f"Active filters: {', '.join(active_filters) if active_filters else 'none'}")
    
    if 'deps' in active_filters and args.deps_for:
        print(f"Showing dependencies for: {', '.join(args.deps_for)}")
        if use_flags:
            print(f"With USE flags: {', '.join(use_flags)}")
    
    if args.no_timestamps:
        print("Timestamps disabled for faster performance")
    
    if args.foreground:
        print("Running in foreground (Ctrl+C to unmount)")
    else:
        print("Running in background")
        print(f"To unmount: fusermount -u {mountpoint}")
    
    try:
        mount_filesystem(
            str(mountpoint),
            foreground=args.foreground,
            debug=args.debug,
            cache_ttl=args.cache_ttl,
            cache_dir=args.cache_dir,
            filter_config=filter_config
        )
    except KeyboardInterrupt:
        print("\nUnmounting...")
    except PermissionError:
        print("Error: Permission denied")
        print("Try running with sudo or check FUSE permissions")
        return 1
    except Exception as e:
        logger.error(f"Mount failed: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    main()