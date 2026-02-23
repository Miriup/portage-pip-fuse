"""
Tests for the prefetcher module.

Tests repository scanning, PyPI name extraction, and integration
with the name translator.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portage_pip_fuse.prefetcher import (
    RepositoryScanner,
    PyPIPrefetcher,
    create_prefetched_translator,
)
from portage_pip_fuse.name_translator import CachedNameTranslator


class TestRepositoryScanner(unittest.TestCase):
    """Test the RepositoryScanner class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.scanner = RepositoryScanner()
        self.temp_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_init(self):
        """Test scanner initialization."""
        scanner = RepositoryScanner()
        self.assertEqual(scanner.repos_conf, "/etc/portage/repos.conf")
        self.assertIsInstance(scanner.repositories, dict)
        
        # Test with custom repos.conf
        scanner = RepositoryScanner("/custom/path")
        self.assertEqual(scanner.repos_conf, "/custom/path")
    
    def test_expand_variables(self):
        """Test bash variable expansion in PYPI_PN values."""
        test_cases = [
            ("${PN}", "my-package", "my-package"),
            ("${PN/-/.}", "google-cloud", "google.cloud"),
            ("${PN/./-}", "zope.interface", "zope-interface"),
            ("${PN/-/_}", "my-package", "my_package"),
            ("${PN/_/-}", "my_package", "my-package"),
            ("${PN^^}", "django", "DJANGO"),
            ("${PN^}", "django", "Django"),
            ("prefix-${PN}", "test", "prefix-test"),
            ("${PN}-suffix", "test", "test-suffix"),
            ("plain-text", "ignored", "plain-text"),
        ]
        
        for template, package_name, expected in test_cases:
            with self.subTest(template=template, package=package_name):
                result = self.scanner._expand_variables(template, package_name)
                self.assertEqual(result, expected)
    
    def test_scan_dev_python_packages(self):
        """Test scanning dev-python packages in a repository."""
        # Create mock repository structure
        repo_path = os.path.join(self.temp_dir, "test-repo")
        dev_python_path = os.path.join(repo_path, "dev-python")
        os.makedirs(dev_python_path)
        
        # Create some mock packages
        packages = ["django", "flask", "requests", "numpy"]
        for pkg in packages:
            os.makedirs(os.path.join(dev_python_path, pkg))
        
        # Add metadata directory (should be skipped)
        os.makedirs(os.path.join(dev_python_path, "metadata"))
        
        # Scan packages
        found_packages = self.scanner.scan_dev_python_packages(repo_path)
        
        # Check results
        self.assertEqual(len(found_packages), 4)
        package_names = [name for name, _ in found_packages]
        for pkg in packages:
            self.assertIn(pkg, package_names)
        self.assertNotIn("metadata", package_names)
    
    def test_scan_nonexistent_repo(self):
        """Test scanning a non-existent repository."""
        packages = self.scanner.scan_dev_python_packages("/nonexistent/repo")
        self.assertEqual(packages, [])
    
    def test_check_pypi_inheritance(self):
        """Test checking if a package inherits pypi eclass."""
        # Create mock package with ebuild
        pkg_path = os.path.join(self.temp_dir, "test-package")
        os.makedirs(pkg_path)
        
        # Create ebuild that inherits pypi
        ebuild_content = """
# Copyright 2024 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

DISTUTILS_USE_PEP517=setuptools
PYTHON_COMPAT=( python3_{10..12} )

inherit distutils-r1 pypi

DESCRIPTION="Test package"
HOMEPAGE="https://example.com"

LICENSE="MIT"
SLOT="0"
KEYWORDS="~amd64 ~x86"
"""
        ebuild_path = os.path.join(pkg_path, "test-1.0.ebuild")
        with open(ebuild_path, 'w') as f:
            f.write(ebuild_content)
        
        # Test detection
        self.assertTrue(self.scanner.check_pypi_inheritance(pkg_path))
        
        # Create ebuild without pypi
        no_pypi_content = """
EAPI=8
inherit distutils-r1
DESCRIPTION="Test package without pypi"
"""
        no_pypi_path = os.path.join(self.temp_dir, "no-pypi")
        os.makedirs(no_pypi_path)
        with open(os.path.join(no_pypi_path, "test-1.0.ebuild"), 'w') as f:
            f.write(no_pypi_content)
        
        self.assertFalse(self.scanner.check_pypi_inheritance(no_pypi_path))
    
    def test_extract_pypi_name(self):
        """Test extracting PyPI name from ebuilds."""
        # Create mock package directory
        pkg_path = os.path.join(self.temp_dir, "test-package")
        os.makedirs(pkg_path)
        
        # Test with explicit PYPI_PN
        ebuild_with_pypi_pn = """
EAPI=8
PYPI_PN="TestPackage"
inherit pypi distutils-r1

DESCRIPTION="Test package"
"""
        with open(os.path.join(pkg_path, "test-1.0.ebuild"), 'w') as f:
            f.write(ebuild_with_pypi_pn)
        
        pypi_name = self.scanner.extract_pypi_name(pkg_path, "test-package")
        self.assertEqual(pypi_name, "TestPackage")
        
        # Test with variable substitution
        ebuild_with_substitution = """
EAPI=8
PYPI_PN="${PN/-/.}"
inherit pypi
"""
        pkg2_path = os.path.join(self.temp_dir, "google-cloud")
        os.makedirs(pkg2_path)
        with open(os.path.join(pkg2_path, "google-cloud-1.0.ebuild"), 'w') as f:
            f.write(ebuild_with_substitution)
        
        pypi_name = self.scanner.extract_pypi_name(pkg2_path, "google-cloud")
        self.assertEqual(pypi_name, "google.cloud")
        
        # Test without PYPI_PN (should return None)
        ebuild_without_pypi_pn = """
EAPI=8
inherit pypi
DESCRIPTION="Package using default name"
"""
        pkg3_path = os.path.join(self.temp_dir, "default-pkg")
        os.makedirs(pkg3_path)
        with open(os.path.join(pkg3_path, "default-1.0.ebuild"), 'w') as f:
            f.write(ebuild_without_pypi_pn)
        
        pypi_name = self.scanner.extract_pypi_name(pkg3_path, "default-pkg")
        self.assertIsNone(pypi_name)


class TestPyPIPrefetcher(unittest.TestCase):
    """Test the PyPIPrefetcher class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.prefetcher = PyPIPrefetcher()
        self.temp_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_init(self):
        """Test prefetcher initialization."""
        # Default initialization
        prefetcher = PyPIPrefetcher()
        self.assertIsInstance(prefetcher.translator, CachedNameTranslator)
        self.assertIsInstance(prefetcher.scanner, RepositoryScanner)
        self.assertEqual(len(prefetcher.masters), 0)
        self.assertEqual(len(prefetcher.mappings), 0)
        
        # With custom translator
        translator = CachedNameTranslator()
        prefetcher = PyPIPrefetcher(translator)
        self.assertIs(prefetcher.translator, translator)
    
    def test_guess_pypi_names(self):
        """Test guessing PyPI names from Gentoo names."""
        test_cases = [
            ("django", ["Django"]),
            ("google-cloud", ["google.cloud", "google_cloud"]),
            ("beautifulsoup4", ["Beautifulsoup4"]),
            ("pyyaml", ["PyYAML"]),  # Special py prefix case
            ("my-package", ["my.package", "my_package", "MyPackage"]),
        ]
        
        for gentoo_name, expected_patterns in test_cases:
            with self.subTest(gentoo_name=gentoo_name):
                guesses = self.prefetcher._guess_pypi_names(gentoo_name)
                for pattern in expected_patterns:
                    self.assertIn(pattern, guesses, 
                                 f"{pattern} not in guesses for {gentoo_name}")
    
    def test_get_masters(self):
        """Test getting master repositories."""
        self.assertEqual(self.prefetcher.get_masters(), set())
        
        # Add some masters
        self.prefetcher.masters.add("gentoo")
        self.prefetcher.masters.add("local")
        
        masters = self.prefetcher.get_masters()
        self.assertEqual(masters, {"gentoo", "local"})
    
    def test_get_translator(self):
        """Test getting the translator instance."""
        translator = self.prefetcher.get_translator()
        self.assertIsInstance(translator, CachedNameTranslator)
        self.assertIs(translator, self.prefetcher.translator)
    
    @patch('portage_pip_fuse.prefetcher.RepositoryScanner.discover_repositories')
    @patch('portage_pip_fuse.prefetcher.RepositoryScanner.scan_dev_python_packages')
    @patch('portage_pip_fuse.prefetcher.RepositoryScanner.check_pypi_inheritance')
    @patch('portage_pip_fuse.prefetcher.RepositoryScanner.extract_pypi_name')
    def test_load_from_repositories(self, mock_extract, mock_check, 
                                   mock_scan, mock_discover):
        """Test loading mappings from repositories."""
        # Setup mocks
        mock_discover.return_value = {"test-repo": "/path/to/repo"}
        mock_scan.return_value = [
            ("django", "/path/to/repo/dev-python/django"),
            ("flask", "/path/to/repo/dev-python/flask"),
            ("google-cloud", "/path/to/repo/dev-python/google-cloud"),
        ]
        
        # django uses default name, flask has custom name, google-cloud has substitution
        mock_check.side_effect = [True, True, True]  # All inherit pypi
        mock_extract.side_effect = [None, "Flask", "google.cloud"]
        
        # Load mappings
        mappings = self.prefetcher.load_from_repositories()
        
        # Verify calls
        mock_discover.assert_called_once()
        mock_scan.assert_called_once_with("/path/to/repo")
        self.assertEqual(mock_check.call_count, 3)
        self.assertEqual(mock_extract.call_count, 3)
        
        # Check mappings were loaded
        self.assertIn("Flask", mappings)
        self.assertEqual(mappings["Flask"], "flask")
        self.assertIn("google.cloud", mappings)
        self.assertEqual(mappings["google.cloud"], "google-cloud")
        
        # Check masters
        self.assertIn("test-repo", self.prefetcher.masters)


class TestIntegration(unittest.TestCase):
    """Integration tests for the complete prefetcher system."""
    
    def test_create_prefetched_translator(self):
        """Test the convenience function for creating a prefetched translator."""
        with patch('portage_pip_fuse.prefetcher.RepositoryScanner.discover_repositories') as mock:
            mock.return_value = {}  # No repositories
            
            translator = create_prefetched_translator()
            self.assertIsInstance(translator, CachedNameTranslator)
            mock.assert_called_once()
    
    def test_real_system_scan(self):
        """Test scanning on a real system (if available)."""
        scanner = RepositoryScanner()
        repos = scanner.discover_repositories()
        
        if not repos:
            self.skipTest("No Gentoo repositories found on this system")
        
        # Just verify we can scan without errors
        for repo_name, repo_path in repos.items():
            packages = scanner.scan_dev_python_packages(repo_path)
            # We should find at least some packages in gentoo repo
            if repo_name == "gentoo" and os.path.exists(repo_path):
                self.assertGreater(len(packages), 0, 
                                 f"Expected packages in {repo_path}")


def run_doctests():
    """Run doctests from the prefetcher module."""
    import doctest
    from portage_pip_fuse import prefetcher
    
    results = doctest.testmod(prefetcher, verbose=True)
    return results.failed == 0


if __name__ == "__main__":
    # Run doctests first
    print("Running doctests...")
    if run_doctests():
        print("All doctests passed!")
    else:
        print("Some doctests failed!")
    
    print("\nRunning unit tests...")
    unittest.main(verbosity=2)