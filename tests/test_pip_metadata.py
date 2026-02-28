"""
Tests for the pip_metadata module.

Tests PyPI metadata extraction, Manifest generation, and ebuild data preparation.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import os
import sys
import unittest
from unittest.mock import Mock, patch, MagicMock
from unittest import TestCase

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portage_pip_fuse.pip_metadata import (
    PyPIMetadataExtractor,
    EbuildDataExtractor,
    get_package_info,
    generate_manifest_dist,
)


class TestPyPIMetadataExtractor(TestCase):
    """Test the PyPIMetadataExtractor class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.extractor = PyPIMetadataExtractor()
    
    def test_init(self):
        """Test extractor initialization."""
        extractor = PyPIMetadataExtractor()
        self.assertEqual(extractor.timeout, 30)
        self.assertEqual(extractor.session_timeout, 30)
        self.assertEqual(extractor.user_agent, "portage-pip-fuse/0.1.0")
        
        # Test with custom parameters
        extractor = PyPIMetadataExtractor(session_timeout=60, user_agent="test/1.0")
        self.assertEqual(extractor.timeout, 60)
        self.assertEqual(extractor.user_agent, "test/1.0")
    
    def test_extract_download_info(self):
        """Test extracting download info from PyPI JSON."""
        # Mock PyPI JSON response structure
        mock_json = {
            'urls': [
                {
                    'filename': 'example-1.0.tar.gz',
                    'url': 'https://files.pythonhosted.org/example-1.0.tar.gz',
                    'size': 12345,
                    'packagetype': 'sdist',
                    'python_version': 'source',
                    'digests': {
                        'md5': 'abc123def456',
                        'sha256': 'def456ghi789',
                        'blake2b_256': 'ghi789jkl012'
                    },
                    'upload_time_iso_8601': '2024-01-15T10:30:00Z'
                },
                {
                    'filename': 'example-1.0-py3-none-any.whl',
                    'url': 'https://files.pythonhosted.org/example-1.0-py3-none-any.whl',
                    'size': 8765,
                    'packagetype': 'bdist_wheel',
                    'python_version': 'py3',
                    'digests': {
                        'md5': 'wheel123',
                        'sha256': 'wheel456'
                    }
                }
            ]
        }
        
        downloads = self.extractor.extract_download_info(mock_json)
        
        self.assertEqual(len(downloads), 2)
        
        # Check source distribution
        sdist = downloads[0]
        self.assertEqual(sdist['filename'], 'example-1.0.tar.gz')
        self.assertEqual(sdist['size'], 12345)
        self.assertEqual(sdist['packagetype'], 'sdist')
        self.assertEqual(sdist['digests']['md5'], 'abc123def456')
        self.assertEqual(sdist['digests']['sha256'], 'def456ghi789')
        
        # Check wheel
        wheel = downloads[1]
        self.assertEqual(wheel['filename'], 'example-1.0-py3-none-any.whl')
        self.assertEqual(wheel['packagetype'], 'bdist_wheel')
    
    def test_get_source_distribution(self):
        """Test finding source distribution from downloads."""
        downloads = [
            {'packagetype': 'bdist_wheel', 'filename': 'example-1.0-py3-none-any.whl'},
            {'packagetype': 'sdist', 'filename': 'example-1.0.tar.gz', 'size': 12345},
            {'packagetype': 'sdist', 'filename': 'example-1.0.zip', 'size': 13000},
        ]
        
        sdist = self.extractor.get_source_distribution(downloads)
        
        # Should prefer .tar.gz
        self.assertEqual(sdist['filename'], 'example-1.0.tar.gz')
        self.assertEqual(sdist['packagetype'], 'sdist')
        
        # Test with no .tar.gz
        downloads_no_targz = [
            {'packagetype': 'bdist_wheel', 'filename': 'example-1.0-py3-none-any.whl'},
            {'packagetype': 'sdist', 'filename': 'example-1.0.zip', 'size': 13000},
        ]
        
        sdist = self.extractor.get_source_distribution(downloads_no_targz)
        self.assertEqual(sdist['filename'], 'example-1.0.zip')
        
        # Test with no sdist
        downloads_no_sdist = [
            {'packagetype': 'bdist_wheel', 'filename': 'example-1.0-py3-none-any.whl'},
        ]
        
        sdist = self.extractor.get_source_distribution(downloads_no_sdist)
        self.assertIsNone(sdist)
    
    def test_generate_manifest_entry(self):
        """Test generating Manifest DIST entries."""
        download_info = {
            'filename': 'numpy-1.21.0.tar.gz',
            'size': 10485760,
            'digests': {
                'md5': '1234567890abcdef1234567890abcdef',
                'sha256': '3ffb289b9edc1cc4cdcb3f7b0ac5c1d8e8c2b0b1f1e0a1f1e0a1f1e0a1f1e0a1',
                'blake2b_256': '5ffb289b9edc1cc4cdcb3f7b0ac5c1d8e8c2b0b1f1e0a1f1e0a1f1e0a1f1e0a1'
            }
        }

        # Test with all available hashes
        entry = self.extractor.generate_manifest_entry(download_info)

        self.assertIn('DIST numpy-1.21.0.tar.gz 10485760', entry)
        self.assertIn('MD5 1234567890abcdef1234567890abcdef', entry)
        self.assertIn('SHA256 3ffb289b9edc1cc4cdcb3f7b0ac5c1d8e8c2b0b1f1e0a1f1e0a1f1e0a1f1e0a1', entry)

        # BLAKE2B should NOT be included - PyPI's blake2b_256 (256-bit) is incompatible
        # with Gentoo's BLAKE2B (512-bit). Different output sizes from same algorithm.
        self.assertNotIn('BLAKE2B', entry)

        # Test with specific wanted hashes
        entry_sha256_only = self.extractor.generate_manifest_entry(download_info, ['SHA256'])
        self.assertIn('SHA256', entry_sha256_only)
        self.assertNotIn('MD5', entry_sha256_only)

        # Test with missing hash (BLAKE2B can't be provided from PyPI data)
        entry_missing = self.extractor.generate_manifest_entry(download_info, ['BLAKE2B'])
        self.assertEqual(entry_missing, 'DIST numpy-1.21.0.tar.gz 10485760')  # Only filename and size
    
    def test_get_package_metadata(self):
        """Test extracting package metadata."""
        mock_json = {
            'info': {
                'name': 'example-package',
                'version': '1.0.0',
                'summary': 'An example package',
                'description': 'A longer description of the example package',
                'home_page': 'https://example.com',
                'author': 'John Doe',
                'author_email': 'john@example.com',
                'license': 'MIT',
                'keywords': 'example test demo',
                'classifiers': [
                    'Development Status :: 4 - Beta',
                    'Programming Language :: Python :: 3',
                    'Programming Language :: Python :: 3.8',
                    'Programming Language :: Python :: 3.9'
                ],
                'requires_dist': [
                    'requests>=2.0.0',
                    'click>=7.0',
                    'pytest>=6.0; extra == "test"'
                ],
                'requires_python': '>=3.8',
                'project_urls': {
                    'Bug Tracker': 'https://github.com/example/issues',
                    'Source': 'https://github.com/example/repo'
                }
            }
        }
        
        metadata = self.extractor.get_package_metadata(mock_json)
        
        self.assertEqual(metadata['name'], 'example-package')
        self.assertEqual(metadata['version'], '1.0.0')
        self.assertEqual(metadata['summary'], 'An example package')
        self.assertEqual(metadata['homepage'], 'https://example.com')
        self.assertEqual(metadata['license'], 'MIT')
        self.assertEqual(metadata['python_requires'], '>=3.8')
        self.assertEqual(len(metadata['classifiers']), 4)
        self.assertEqual(len(metadata['dependencies']), 3)
    
    def test_extract_python_versions(self):
        """Test extracting Python versions from classifiers."""
        classifiers = [
            'Development Status :: 4 - Beta',
            'Programming Language :: Python :: 3',
            'Programming Language :: Python :: 3.8',
            'Programming Language :: Python :: 3.9',
            'Programming Language :: Python :: 3.10',
            'Programming Language :: Python :: 3.11',
            'Programming Language :: Python :: Implementation :: CPython',
            'Topic :: Software Development :: Libraries :: Python Modules'
        ]
        
        versions = self.extractor.extract_python_versions(classifiers)
        
        expected_versions = ['3.8', '3.9', '3.10', '3.11']
        self.assertEqual(versions, expected_versions)
        
        # Test with no version classifiers
        no_version_classifiers = [
            'Development Status :: 4 - Beta',
            'Topic :: Software Development'
        ]
        
        versions = self.extractor.extract_python_versions(no_version_classifiers)
        self.assertEqual(versions, [])
    
    def test_parse_dependencies(self):
        """Test parsing dependencies from requires_dist."""
        requires_dist = [
            'requests>=2.0.0',
            'click>=7.0',
            'pytest>=6.0; extra == "test"',
            'sphinx>=4.0; extra == "docs"',
            'typing_extensions; python_version<"3.8"'
        ]
        
        runtime_deps, optional_deps = self.extractor.parse_dependencies(requires_dist)
        
        # Runtime dependencies (no extras, or with python_version conditions)
        self.assertIn('requests>=2.0.0', runtime_deps)
        self.assertIn('click>=7.0', runtime_deps)
        self.assertIn('typing_extensions; python_version<"3.8"', runtime_deps)
        
        # Optional dependencies (with extra conditions)
        optional_strs = ' '.join(optional_deps)
        self.assertIn('pytest', optional_strs)
        self.assertIn('sphinx', optional_strs)
        self.assertIn('extra', optional_strs)


class TestEbuildDataExtractor(TestCase):
    """Test the EbuildDataExtractor class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.extractor = EbuildDataExtractor()
    
    def test_format_python_compat(self):
        """Test formatting Python versions for PYTHON_COMPAT."""
        # Use Python versions that are in current _PYTHON_ALL_IMPLS
        versions = ['3.11', '3.12', '3.13']
        compat = self.extractor.format_python_compat(versions)

        self.assertIn('python3_11', compat)
        self.assertIn('python3_12', compat)
        self.assertIn('python3_13', compat)

        # Test with empty list (should use defaults from valid impls)
        compat_empty = self.extractor.format_python_compat([])
        self.assertIn('python3_11', compat_empty)  # Should include default versions
    
    def test_format_dependencies(self):
        """Test formatting dependencies for ebuilds."""
        dependencies = ['requests>=2.0.0', 'click>=7.0', 'numpy>=1.20.0']
        formatted = self.extractor.format_dependencies(dependencies)
        
        # Should convert PyPI names to Gentoo dev-python/ format
        dep_str = ' '.join(formatted)
        self.assertIn('dev-python/requests', dep_str)
        self.assertIn('dev-python/click', dep_str)
        self.assertIn('dev-python/numpy', dep_str)
    
    def test_prepare_ebuild_data(self):
        """Test preparing complete ebuild data."""
        package_info = {
            'metadata': {
                'name': 'example-package',
                'version': '1.0.0',
                'summary': 'An example package for testing',
                'homepage': 'https://example.com',
                'license': 'MIT'
            },
            'python_versions': ['3.11', '3.12', '3.13'],
            'runtime_dependencies': ['requests>=2.0', 'click>=7.0'],
            'source_distribution': {
                'url': 'https://pypi.org/example-1.0.0.tar.gz',
                'filename': 'example-1.0.0.tar.gz'
            }
        }

        ebuild_data = self.extractor.prepare_ebuild_data(package_info)

        self.assertEqual(ebuild_data['PN'], 'example-package')
        self.assertEqual(ebuild_data['PV'], '1.0.0')
        self.assertEqual(ebuild_data['DESCRIPTION'], 'An example package for testing')
        self.assertEqual(ebuild_data['HOMEPAGE'], 'https://example.com')
        self.assertEqual(ebuild_data['LICENSE'], 'MIT')
        self.assertEqual(ebuild_data['SRC_URI'], 'https://pypi.org/example-1.0.0.tar.gz')
        self.assertEqual(ebuild_data['KEYWORDS'], 'amd64 x86')  # Stable keywords for PyPI releases
        self.assertEqual(ebuild_data['SLOT'], '0')

        # Check Python compatibility (using current valid impls)
        self.assertIn('python3_11', ebuild_data['PYTHON_COMPAT'])
        self.assertIn('python3_12', ebuild_data['PYTHON_COMPAT'])
        
        # Check dependencies
        self.assertIsInstance(ebuild_data['DEPEND'], list)
        self.assertIsInstance(ebuild_data['RDEPEND'], list)


class TestConvenienceFunctions(TestCase):
    """Test module-level convenience functions."""
    
    @patch('portage_pip_fuse.pip_metadata.PyPIMetadataExtractor')
    def test_get_package_info(self, mock_extractor_class):
        """Test the get_package_info convenience function."""
        # Mock the extractor and its method
        mock_extractor = Mock()
        mock_extractor.get_complete_package_info.return_value = {'test': 'data'}
        mock_extractor_class.return_value = mock_extractor
        
        result = get_package_info('test-package', '1.0.0')
        
        mock_extractor_class.assert_called_once()
        mock_extractor.get_complete_package_info.assert_called_once_with('test-package', '1.0.0')
        self.assertEqual(result, {'test': 'data'})
    
    @patch('portage_pip_fuse.pip_metadata.get_package_info')
    def test_generate_manifest_dist(self, mock_get_info):
        """Test the generate_manifest_dist convenience function."""
        # Mock package info with manifest entry
        mock_get_info.return_value = {
            'manifest_entry': 'DIST test-1.0.tar.gz 1234 MD5 abc123 SHA256 def456'
        }
        
        result = generate_manifest_dist('test-package')
        
        mock_get_info.assert_called_once_with('test-package', None)
        self.assertEqual(result, 'DIST test-1.0.tar.gz 1234 MD5 abc123 SHA256 def456')
        
        # Test with no manifest entry
        mock_get_info.return_value = {'other': 'data'}
        result = generate_manifest_dist('test-package')
        self.assertIsNone(result)
        
        # Test with no package info
        mock_get_info.return_value = None
        result = generate_manifest_dist('test-package')
        self.assertIsNone(result)


def run_doctests():
    """Run doctests from the pip_metadata module."""
    import doctest
    from portage_pip_fuse import pip_metadata
    
    results = doctest.testmod(pip_metadata, verbose=True)
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