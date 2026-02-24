"""
SQLite-based PyPI metadata backend using pypi-data/pypi-json-data.

This module provides a high-performance alternative to individual PyPI JSON API calls
by using the bulk SQLite database from pypi-data/pypi-json-data, which is updated daily
and contains release and download data for all PyPI packages.

The SQLite approach replaces 746k+ individual HTTP requests with a single database
download plus local queries, dramatically improving performance.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import gzip
import hashlib
import json
import logging
import os
import sqlite3
import struct
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Tuple
from urllib.error import URLError

# Try to import tqdm for fancy progress bars
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

logger = logging.getLogger(__name__)

# Default database URL - updated daily
DEFAULT_PYPI_DATA_URL = "https://github.com/pypi-data/pypi-json-data/releases/download/latest/pypi-data.sqlite.gz"

# GitHub API URL for release metadata (provides SHA256 and file size)
GITHUB_RELEASES_API_URL = "https://api.github.com/repos/pypi-data/pypi-json-data/releases/latest"

# Import cache directory constant
from portage_pip_fuse.constants import DEFAULT_CACHE_DIR

# Maximum age before database is considered stale (7 days)
DEFAULT_MAX_AGE_DAYS = 7

# Expected gzip compression ratio for SQLite databases (used to estimate uncompressed size)
EXPECTED_GZIP_RATIO = 6.0


class SQLiteMetadataBackend:
    """
    SQLite-based PyPI metadata backend using pypi-data bulk database.
    
    This backend downloads and uses the daily-updated SQLite database from
    pypi-data/pypi-json-data instead of making individual PyPI JSON API calls.
    
    Features:
    - Single ~1GB database download vs 746k+ API calls
    - Daily updates with automatic staleness detection
    - Local SQLite queries for fast metadata access
    - Fallback to PyPI JSON API for missing packages
    - Comprehensive size and staleness warnings
    """
    
    def __init__(self,
                 cache_dir: Optional[Path] = None,
                 database_url: str = DEFAULT_PYPI_DATA_URL,
                 max_age_days: int = DEFAULT_MAX_AGE_DAYS):
        """
        Initialize SQLite metadata backend.

        Args:
            cache_dir: Directory for caching SQLite database (Path or str)
            database_url: URL to download SQLite database from
            max_age_days: Maximum age in days before database is stale
        """
        # Convert str to Path if needed
        if cache_dir is not None and isinstance(cache_dir, str):
            cache_dir = Path(cache_dir)
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.database_url = database_url
        self.max_age_days = max_age_days
        
        # Ensure cache directory exists
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # SQLite database file path
        self.db_path = self.cache_dir / 'pypi-data.sqlite'
        
        # Connection to SQLite database
        self._conn: Optional[sqlite3.Connection] = None
        
        # Fallback to PyPI JSON API for missing packages
        self._fallback_session: Optional[Any] = None
        
    def _get_database_age_days(self) -> Optional[float]:
        """
        Get age of cached database in days.
        
        Returns:
            Age in days, or None if database doesn't exist
        """
        if not self.db_path.exists():
            return None
            
        mtime = self.db_path.stat().st_mtime
        age_seconds = time.time() - mtime
        return age_seconds / (24 * 3600)
        
    def _is_database_stale(self) -> bool:
        """
        Check if cached database is stale.
        
        Returns:
            True if database is stale or missing
        """
        age_days = self._get_database_age_days()
        return age_days is None or age_days > self.max_age_days
        
    def _format_size(self, size_bytes: float) -> str:
        """Format size in bytes to human readable string."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024
        return f"{size_bytes:.1f} TB"
        
    def _format_time(self, seconds: float) -> str:
        """Format time in seconds to human readable string (MM:SS or HH:MM:SS)."""
        if seconds <= 0 or seconds == float('inf'):
            return "--:--"

        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        if hours > 0:
            return f"{hours:d}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes:2d}:{secs:02d}"

    def _fetch_release_metadata(self) -> Optional[Dict[str, Any]]:
        """
        Fetch release metadata from GitHub API.

        Returns:
            Dict with 'size' (compressed), 'sha256', and 'download_url', or None on error
        """
        try:
            req = urllib.request.Request(GITHUB_RELEASES_API_URL)
            req.add_header('Accept', 'application/vnd.github.v3+json')
            req.add_header('User-Agent', 'portage-pip-fuse')

            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode('utf-8'))

            # Find the sqlite.gz asset
            for asset in data.get('assets', []):
                if asset.get('name') == 'pypi-data.sqlite.gz':
                    digest = asset.get('digest', '')
                    sha256 = None
                    if digest.startswith('sha256:'):
                        sha256 = digest[7:]

                    return {
                        'size': asset.get('size', 0),
                        'sha256': sha256,
                        'download_url': asset.get('browser_download_url', self.database_url)
                    }

            logger.warning("Could not find pypi-data.sqlite.gz in release assets")
            return None

        except Exception as e:
            logger.warning(f"Failed to fetch release metadata from GitHub API: {e}")
            return None

    def _fetch_gzip_isize(self, url: str, compressed_size: int) -> Optional[int]:
        """
        Fetch the ISIZE field from the last 4 bytes of a remote gzip file.

        The ISIZE is the uncompressed size modulo 2^32.

        Args:
            url: URL of the gzip file
            compressed_size: Total size of compressed file (for Range header)

        Returns:
            ISIZE value (mod 2^32), or None on error
        """
        try:
            req = urllib.request.Request(url)
            req.add_header('Range', f'bytes={compressed_size - 4}-{compressed_size - 1}')
            req.add_header('User-Agent', 'portage-pip-fuse')

            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status == 206:  # Partial Content
                    data = response.read(4)
                    if len(data) == 4:
                        return struct.unpack('<I', data)[0]

            return None

        except Exception as e:
            logger.warning(f"Failed to fetch gzip ISIZE: {e}")
            return None

    def _estimate_uncompressed_size(self, compressed_size: int, isize: Optional[int] = None) -> int:
        """
        Estimate the uncompressed size of a gzip file.

        Uses the compression ratio to estimate the approximate size, then
        refines it using the gzip ISIZE field (which is mod 2^32) to get
        the exact size.

        Args:
            compressed_size: Size of compressed gzip file
            isize: ISIZE from gzip trailer (mod 2^32), or None

        Returns:
            Estimated uncompressed size in bytes
        """
        # Initial estimate based on compression ratio
        estimated = int(compressed_size * EXPECTED_GZIP_RATIO)

        if isize is None:
            return estimated

        # Use ISIZE to get exact size
        # ISIZE = actual_size mod 2^32
        # So actual_size = ISIZE + N * 2^32 for some N >= 0
        modulo = 2 ** 32

        # Calculate N based on our estimate
        n = round((estimated - isize) / modulo)
        if n < 0:
            n = 0

        actual_size = isize + n * modulo

        logger.debug(f"Size estimation: compressed={compressed_size}, estimated={estimated}, "
                    f"isize={isize}, n={n}, actual={actual_size}")

        return actual_size

    def _verify_sha256(self, file_path: Path, expected_sha256: str) -> bool:
        """
        Verify SHA256 checksum of a file.

        Args:
            file_path: Path to file to verify
            expected_sha256: Expected SHA256 hash (hex string)

        Returns:
            True if checksum matches, False otherwise
        """
        sha256 = hashlib.sha256()

        try:
            with open(file_path, 'rb') as f:
                while True:
                    chunk = f.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    sha256.update(chunk)

            actual = sha256.hexdigest()
            if actual == expected_sha256:
                logger.info(f"SHA256 verification successful: {actual}")
                return True
            else:
                logger.error(f"SHA256 mismatch! Expected: {expected_sha256}, Got: {actual}")
                return False

        except Exception as e:
            logger.error(f"Failed to verify SHA256: {e}")
            return False

    def _download_database(self, force: bool = False) -> bool:
        """
        Download SQLite database from pypi-data with resume support.

        Uses Gentoo-style resumable downloads: downloads to file.__download__
        and renames when complete. Supports HTTP range requests for resuming.
        Verifies SHA256 after download and estimates uncompressed size for
        accurate decompression progress.

        Args:
            force: Force download even if database exists and is fresh

        Returns:
            True if download successful, False otherwise
        """
        if not force and not self._is_database_stale():
            logger.info("Database is fresh, skipping download")
            return True

        logger.info(f"Downloading PyPI metadata database from {self.database_url}")

        # Use Gentoo-style download naming
        download_path = Path(str(self.db_path) + '.gz.__download__')
        final_gz_path = Path(str(self.db_path) + '.gz')

        # Fetch release metadata from GitHub API (SHA256, size)
        release_meta = self._fetch_release_metadata()
        expected_sha256 = None
        expected_compressed_size = 0
        uncompressed_size = 0

        if release_meta:
            expected_sha256 = release_meta.get('sha256')
            expected_compressed_size = release_meta.get('size', 0)
            if expected_sha256:
                logger.info(f"Expected SHA256: {expected_sha256}")
            if expected_compressed_size > 0:
                # Fetch gzip ISIZE to estimate uncompressed size accurately
                isize = self._fetch_gzip_isize(self.database_url, expected_compressed_size)
                uncompressed_size = self._estimate_uncompressed_size(expected_compressed_size, isize)
                logger.info(f"Estimated uncompressed size: {self._format_size(uncompressed_size)}")

        try:
            # Check if partial download exists
            resume_pos = 0
            if download_path.exists():
                resume_pos = download_path.stat().st_size
                print(f"🔄 Resuming download from {self._format_size(resume_pos)}")
            
            # Prepare HTTP request with range header for resume
            req = urllib.request.Request(self.database_url)
            if resume_pos > 0:
                req.add_header('Range', f'bytes={resume_pos}-')
            
            # Open file in append mode for resume or write mode for fresh download
            file_mode = 'ab' if resume_pos > 0 else 'wb'
            
            with open(download_path, file_mode) as download_file:
                # Download with progress indication
                with urllib.request.urlopen(req) as response:
                    # Handle HTTP 206 (Partial Content) for resumed downloads
                    if response.status == 206:
                        # Parse content-range header: "bytes start-end/total"
                        content_range = response.headers.get('content-range', '')
                        if content_range.startswith('bytes '):
                            total_size = int(content_range.split('/')[-1])
                        else:
                            total_size = 0
                    else:
                        # Fresh download
                        total_size = int(response.headers.get('content-length', 0))
                        if resume_pos > 0:
                            # Server doesn't support resume, start fresh
                            print("⚠️  Server doesn't support resume, starting fresh download")
                            download_file.close()
                            download_path.unlink()
                            download_file = open(download_path, 'wb')
                            resume_pos = 0
                    
                    if total_size > 0:
                        remaining_size = total_size - resume_pos
                        if resume_pos > 0:
                            print(f"📥 Resuming {self._format_size(remaining_size)} remaining of {self._format_size(total_size)} PyPI database...")
                        else:
                            print(f"📥 Downloading {self._format_size(total_size)} PyPI database...")
                        print("🔄 This is a one-time download that will be cached locally.")
                    else:
                        print("📥 Downloading PyPI database...")
                        
                    downloaded = 0  # Downloaded this session
                    total_downloaded = resume_pos  # Total including previous sessions
                    chunk_size = 65536  # 64KB chunks for better performance
                    
                    # Use tqdm for fancy progress bar if available
                    if HAS_TQDM and total_size > 0:
                        with tqdm(total=total_size, initial=resume_pos, unit='B', unit_scale=True, unit_divisor=1024,
                                desc="🚀 PyPI DB", ncols=80, bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]') as pbar:
                            while True:
                                chunk = response.read(chunk_size)
                                if not chunk:
                                    break
                                download_file.write(chunk)
                                downloaded += len(chunk)
                                total_downloaded += len(chunk)
                                pbar.update(len(chunk))
                                
                                # Flush every 1MB to ensure data is written
                                if downloaded % (1024 * 1024) == 0:
                                    download_file.flush()
                    else:
                        # Fallback to custom progress indication with ETA
                        last_percent = -1
                        start_time = time.time()
                        while True:
                            chunk = response.read(chunk_size)
                            if not chunk:
                                break
                            download_file.write(chunk)
                            downloaded += len(chunk)
                            total_downloaded += len(chunk)
                            
                            if total_size > 0:
                                percent = int((total_downloaded / total_size) * 100)
                                if percent != last_percent and percent % 5 == 0:  # Show every 5%
                                    # Calculate ETA
                                    elapsed = time.time() - start_time
                                    if total_downloaded > resume_pos:  # Avoid division by zero
                                        speed = (total_downloaded - resume_pos) / elapsed
                                        remaining_bytes = total_size - total_downloaded
                                        eta_seconds = remaining_bytes / speed if speed > 0 else 0
                                        eta_str = self._format_time(eta_seconds)
                                        speed_str = f"{self._format_size(speed)}/s"
                                    else:
                                        eta_str = "--:--"
                                        speed_str = "-- MB/s"
                                    
                                    bar_width = 30  # Smaller bar to fit ETA
                                    filled_width = int(bar_width * total_downloaded / total_size)
                                    bar = '█' * filled_width + '░' * (bar_width - filled_width)
                                    print(f"\r🚀 [{bar}] {percent}% {self._format_size(total_downloaded)}/{self._format_size(total_size)} ETA: {eta_str} @ {speed_str}", end='', flush=True)
                                    last_percent = percent
                            elif downloaded % (10 * 1024 * 1024) == 0:  # Show every 10MB
                                print(f"\r📥 Downloaded: {self._format_size(total_downloaded)}", end='', flush=True)
                            
                            # Flush every 1MB to ensure data is written
                            if downloaded % (1024 * 1024) == 0:
                                download_file.flush()
                        
                        if total_size > 0:
                            print()  # New line after progress bar
            
            # Rename downloaded file to final name (atomic operation)
            download_path.rename(final_gz_path)

            # Verify SHA256 checksum if available
            if expected_sha256:
                print("🔐 Verifying SHA256 checksum...")
                if not self._verify_sha256(final_gz_path, expected_sha256):
                    print("❌ SHA256 verification failed! Removing corrupted file.")
                    final_gz_path.unlink()
                    return False
                print("✅ SHA256 verified")

            # Decompress gzipped SQLite file
            print("🗜️  Decompressing database...")

            # Use .__decompress__ suffix for in-progress decompression (like download)
            decompress_path = Path(str(self.db_path) + '.__decompress__')

            # Clean up any previous partial decompression
            if decompress_path.exists():
                print("🔄 Removing partial decompression from previous run...")
                decompress_path.unlink()

            # Get compressed file size for progress
            compressed_size = final_gz_path.stat().st_size

            # Use estimated uncompressed size if available, otherwise fall back to compressed
            progress_total = uncompressed_size if uncompressed_size > 0 else compressed_size

            if HAS_TQDM:
                with tqdm(total=progress_total, unit='B', unit_scale=True, unit_divisor=1024,
                        desc="🗜️  Decompress", ncols=80, bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]') as pbar:
                    with gzip.open(final_gz_path, 'rb') as gz_file:
                        with open(decompress_path, 'wb') as db_file:
                            processed = 0
                            while True:
                                chunk = gz_file.read(1024 * 1024)  # 1MB chunks
                                if not chunk:
                                    break
                                db_file.write(chunk)
                                processed += len(chunk)
                                pbar.update(len(chunk))

                                # Flush every 10MB to ensure data is written
                                if processed % (10 * 1024 * 1024) == 0:
                                    db_file.flush()
            else:
                # Simple decompression without detailed progress
                with gzip.open(final_gz_path, 'rb') as gz_file:
                    with open(decompress_path, 'wb') as db_file:
                        processed = 0
                        start_time = time.time()
                        while True:
                            chunk = gz_file.read(1024 * 1024)  # 1MB chunks
                            if not chunk:
                                break
                            db_file.write(chunk)
                            processed += len(chunk)
                            if processed % (10 * 1024 * 1024) == 0:  # Every 10MB
                                if progress_total > 0:
                                    percent = int((processed / progress_total) * 100)
                                    elapsed = time.time() - start_time
                                    if elapsed > 0:
                                        speed = processed / elapsed
                                        remaining = (progress_total - processed) / speed if speed > 0 else 0
                                        eta_str = self._format_time(remaining)
                                        speed_str = f"{self._format_size(speed)}/s"
                                    else:
                                        eta_str = "--:--"
                                        speed_str = "-- MB/s"
                                    bar_width = 30
                                    filled = int(bar_width * processed / progress_total)
                                    bar = '█' * filled + '░' * (bar_width - filled)
                                    print(f"\r🗜️  [{bar}] {percent}% {self._format_size(processed)}/{self._format_size(progress_total)} ETA: {eta_str} @ {speed_str}", end='', flush=True)
                                else:
                                    print(f"\r🗜️  Processed: {self._format_size(processed)}", end='', flush=True)
                                db_file.flush()
                print()  # New line after decompression

            # Rename decompressed file to final name (atomic operation)
            decompress_path.rename(self.db_path)

            # Clean up compressed file after successful decompression
            final_gz_path.unlink()
            
            # Log final size with success message
            final_size = self.db_path.stat().st_size
            print(f"✅ Database downloaded successfully!")
            print(f"📊 Final size: {self._format_size(final_size)}")
            print(f"📁 Location: {self.db_path}")
            
            # Create indexes for faster queries
            self._create_indexes()

            return True

        except URLError as e:
            logger.error(f"Failed to download database: {e}")
            return False
        except Exception as e:
            logger.error(f"Error downloading database: {e}")
            return False

    def _create_indexes(self, quiet: bool = False) -> bool:
        """
        Create indexes on the database for faster queries.

        This is idempotent - indexes are created with IF NOT EXISTS.
        Called after decompression and on connect for existing databases.

        Args:
            quiet: If True, don't print progress messages

        Returns:
            True if indexes created/verified, False on error
        """
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = conn.cursor()

            # Check if indexes already exist
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_projects_name'")
            if cursor.fetchone():
                conn.close()
                logger.debug("Indexes already exist")
                return True

            if not quiet:
                print("📇 Creating indexes for faster queries...")

            # Index on projects.name for package lookups
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_name ON projects(name)")

            # Index on projects.name+version for version-specific lookups
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_name_version ON projects(name, version)")

            # Index on urls.project_id for joining
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_urls_project_id ON urls(project_id)")

            conn.commit()
            conn.close()

            if not quiet:
                print("✅ Indexes created")
            else:
                logger.info("Created database indexes for faster queries")

            return True
        except sqlite3.Error as e:
            logger.error(f"Failed to create indexes: {e}")
            return False

    def _connect_database(self) -> bool:
        """
        Connect to SQLite database.

        Returns:
            True if connection successful, False otherwise
        """
        if self._conn is not None:
            return True

        if not self.db_path.exists():
            logger.error("Database file does not exist")
            return False

        try:
            # check_same_thread=False allows connection to be used from FUSE threads
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row  # Enable dict-like access
            logger.info("Connected to SQLite database")

            # Ensure indexes exist (creates them if missing, fast if present)
            self._create_indexes(quiet=True)

            return True

        except sqlite3.Error as e:
            logger.error(f"Failed to connect to database: {e}")
            return False
            
    def ensure_database(self, force_download: bool = False) -> bool:
        """
        Ensure database is available and up-to-date.
        
        Args:
            force_download: Force download even if database exists
            
        Returns:
            True if database is ready, False otherwise
        """
        # Check if database is stale
        if self._is_database_stale():
            age_days = self._get_database_age_days()
            if age_days is None:
                logger.warning("No cached PyPI database found - downloading for first time")
            else:
                logger.warning(f"PyPI database is {age_days:.1f} days old (stale after {self.max_age_days} days)")
                logger.warning("Newer package data may be available - consider running sync command")
                
        # Download if necessary
        if force_download or self._is_database_stale():
            if not self._download_database(force_download):
                return False
                
        # Connect to database
        return self._connect_database()
        
    def sync_database(self) -> bool:
        """
        Sync database with latest data from pypi-data.
        
        This command should be run periodically to update the local cache
        with the latest PyPI package data.
        
        Returns:
            True if sync successful, False otherwise
        """
        logger.info("Syncing PyPI database with latest data...")
        
        # Show current database status
        age_days = self._get_database_age_days()
        if age_days is not None:
            logger.info(f"Current database is {age_days:.1f} days old")
            
        # Force download of latest database
        success = self._download_database(force=True)
        
        if success:
            logger.info("Database sync completed successfully")
            
            # Reconnect to new database
            if self._conn:
                self._conn.close()
                self._conn = None
            self._connect_database()
        else:
            logger.error("Database sync failed")
            
        return success
        
    def get_package_metadata(self, package_name: str) -> Optional[Dict[str, Any]]:
        """
        Get package metadata from SQLite database.
        
        Args:
            package_name: Name of PyPI package
            
        Returns:
            Package metadata dict, or None if not found
        """
        if not self.ensure_database():
            logger.error("Database not available")
            return None
            
        try:
            cursor = self._conn.cursor()
            
            # Query package information from projects table
            # The pypi-data database has one row per package+version in 'projects'
            cursor.execute("""
                SELECT name, summary, author, author_email, home_page, license,
                       requires_python, version, classifiers, requires_dist
                FROM projects
                WHERE name = ?
                ORDER BY id DESC
                LIMIT 1
            """, (package_name,))
            
            row = cursor.fetchone()
            if row:
                return dict(row)
            else:
                logger.debug(f"Package {package_name} not found in database")
                return None
                
        except sqlite3.Error as e:
            logger.error(f"Database error getting package metadata: {e}")
            return None
            
    def get_package_versions(self, package_name: str) -> List[str]:
        """
        Get all versions for a package from SQLite database.
        
        Args:
            package_name: Name of PyPI package
            
        Returns:
            List of version strings
        """
        if not self.ensure_database():
            logger.error("Database not available")
            return []
            
        try:
            cursor = self._conn.cursor()
            
            # Query versions from projects table (one row per package+version)
            cursor.execute("""
                SELECT DISTINCT version
                FROM projects
                WHERE name = ?
                ORDER BY id DESC
            """, (package_name,))
            
            return [row[0] for row in cursor.fetchall()]
            
        except sqlite3.Error as e:
            logger.error(f"Database error getting package versions: {e}")
            return []
            
    def get_package_releases(self, package_name: str, version: str) -> List[Dict[str, Any]]:
        """
        Get release information for a specific package version.
        
        Args:
            package_name: Name of PyPI package
            version: Package version
            
        Returns:
            List of release file dictionaries
        """
        if not self.ensure_database():
            logger.error("Database not available")
            return []
            
        try:
            cursor = self._conn.cursor()
            
            # Query release files by joining projects and urls tables
            cursor.execute("""
                SELECT u.url, u.upload_time, u.package_type as packagetype,
                       u.python_version, u.requires_python, u.size,
                       u.yanked, u.yanked_reason,
                       p.name, p.version
                FROM urls u
                JOIN projects p ON u.project_id = p.id
                WHERE p.name = ? AND p.version = ?
            """, (package_name, version))

            releases = []
            for row in cursor.fetchall():
                release = dict(row)
                # Extract filename from URL
                url = release.get('url', '')
                release['filename'] = url.split('/')[-1] if url else ''
                # Add empty digests (not available in this schema)
                release['digests'] = {}
                releases.append(release)
                
            return releases
            
        except sqlite3.Error as e:
            logger.error(f"Database error getting package releases: {e}")
            return []

    def get_all_package_releases(self, package_name: str) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get all releases for all versions of a package in a single query.

        This is much more efficient than calling get_package_releases() for
        each version, as it executes only one SQL query instead of N queries.

        Args:
            package_name: Name of PyPI package

        Returns:
            Dict mapping version strings to lists of release file dictionaries
        """
        if not self.ensure_database():
            logger.error("Database not available")
            return {}

        try:
            cursor = self._conn.cursor()

            # Query all release files for this package in one query
            cursor.execute("""
                SELECT u.url, u.upload_time, u.package_type as packagetype,
                       u.python_version, u.requires_python, u.size,
                       u.yanked, u.yanked_reason,
                       p.name, p.version
                FROM urls u
                JOIN projects p ON u.project_id = p.id
                WHERE p.name = ?
            """, (package_name,))

            # Group results by version
            releases_by_version: Dict[str, List[Dict[str, Any]]] = {}
            for row in cursor.fetchall():
                release = dict(row)
                version = release.pop('version')
                # Also remove 'name' as it's redundant
                release.pop('name', None)
                # Extract filename from URL
                url = release.get('url', '')
                release['filename'] = url.split('/')[-1] if url else ''
                # Add empty digests (not available in this schema)
                release['digests'] = {}
                releases_by_version.setdefault(version, []).append(release)

            return releases_by_version

        except sqlite3.Error as e:
            logger.error(f"Database error getting all package releases: {e}")
            return {}

    def close(self):
        """Close database connection."""
        if self._conn:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                # May happen if called from different thread, connection already invalid
                pass
            self._conn = None
            
    def __enter__(self):
        """Context manager entry."""
        self.ensure_database()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()