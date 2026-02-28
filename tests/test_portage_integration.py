"""
End-to-end integration test for portage resolver with FUSE filesystem.

This test creates an isolated portage configuration where:
- PYTHON_TARGETS is set to a single specific value
- Gentoo repo's dev-python/* packages are masked
- Our FUSE repo is the sole source of dev-python packages
- Other categories (dev-lang/python, virtual/*, etc.) come from gentoo
- No installed packages are considered (empty vartree)

If the entire dependency tree of open-webui (112+ direct dependencies)
resolves successfully, the FUSE implementation is working correctly.
Any resolution failures indicate implementation bugs, not system
misconfiguration.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import configparser
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Optional

# Check if portage is available
try:
    import portage
    PORTAGE_AVAILABLE = True
except ImportError:
    PORTAGE_AVAILABLE = False


# =============================================================================
# Test Constants (configurable)
# =============================================================================

# Package with massive dependency tree (112 direct deps)
TEST_PACKAGE_NAME = "open-webui"
TEST_PACKAGE_VERSION = "0.8.5"  # Optional: specific version

# Single Python target for isolation
TEST_PYTHON_TARGET = "python3_12"

# Show spinner during dependency resolution (True = show, False = quiet)
TEST_SHOW_SPINNER = True

# Re-run with debug output on failure (True = re-run, False = fail immediately)
TEST_DEBUG_ON_FAILURE = True

# Repository names
OVERLAY_REPO_NAME = "portage-pip-fuse"
DEFAULT_FUSE_LOCATION = Path("/var/db/repos/pypi")
GENTOO_LOCATION = Path("/var/db/repos/gentoo")


# =============================================================================
# Test Class
# =============================================================================

@unittest.skipUnless(PORTAGE_AVAILABLE, "Portage not available")
class TestPortageIntegration(unittest.TestCase):
    """
    End-to-end integration test for portage resolver with FUSE filesystem.

    This test creates an isolated portage configuration where:
    - PYTHON_TARGETS is set to a single specific value
    - Gentoo repo's dev-python/* packages are masked
    - Our FUSE repo is the sole source of dev-python packages
    - Other categories (dev-lang/python, virtual/*, etc.) come from gentoo
    - No installed packages are considered (empty vartree)

    If the entire dependency tree of open-webui (112+ direct dependencies)
    resolves successfully, the FUSE implementation is working correctly.
    Any resolution failures indicate implementation bugs, not system
    misconfiguration.
    """

    @classmethod
    def setUpClass(cls):
        """Check portage availability once for all tests."""
        if not PORTAGE_AVAILABLE:
            raise unittest.SkipTest("Portage not available")

    def setUp(self):
        """
        Set up isolated portage configuration.

        Creates a temporary directory structure with:
        - Custom make.conf with single PYTHON_TARGET
        - repos.conf loading gentoo (for eclasses) + FUSE repo
        - package.mask masking all gentoo packages
        - package.license whitelisting FUSE overlay licenses
        - Empty var/db/pkg for no installed packages
        """
        # 1. Create temp directory structure
        self.temp_dir = tempfile.mkdtemp(prefix="portage-pip-fuse-test-")
        self.config_root = Path(self.temp_dir)

        self.etc_portage = self.config_root / "etc" / "portage"
        self.etc_portage.mkdir(parents=True)

        # Empty var/db/pkg for no installed packages
        (self.config_root / "var" / "db" / "pkg").mkdir(parents=True)

        # 2. Find FUSE mount location from real repos.conf
        self.fuse_location = self._find_fuse_location()
        if not (self.fuse_location / "profiles" / "repo_name").exists():
            self.skipTest(f"FUSE not mounted at {self.fuse_location}")

        # 3. Create make.profile symlink to host profile
        host_profile = Path("/etc/portage/make.profile")
        if host_profile.is_symlink():
            host_profile = host_profile.resolve()
        elif host_profile.exists():
            # It's a real directory, use it directly
            pass
        else:
            self.skipTest("No make.profile found on host system")

        (self.etc_portage / "make.profile").symlink_to(host_profile)

        # 4. Create make.conf with single PYTHON_TARGET
        self._write_make_conf()

        # 5. Create repos.conf with gentoo (for eclasses) + FUSE
        self._write_repos_conf()

        # 6. Create package.mask to mask dev-python from gentoo
        # We only mask dev-python/* since that's what our FUSE overlay provides.
        # Other categories (dev-lang/python, virtual/*, etc.) come from gentoo.
        mask_dir = self.etc_portage / "package.mask"
        mask_dir.mkdir()
        (mask_dir / "gentoo-mask").write_text("dev-python/*::gentoo\n")

        # 6b. Unmask Gentoo infrastructure packages that aren't PyPI packages
        # These are Gentoo-specific packages that dev-lang/python and others depend on
        unmask_dir = self.etc_portage / "package.unmask"
        unmask_dir.mkdir()
        unmask_content = """# Gentoo infrastructure packages (not from PyPI)
dev-python/gentoo-common::gentoo
dev-python/gpep517::gentoo
dev-python/installer::gentoo
dev-python/flit-core::gentoo
dev-python/setuptools::gentoo
dev-python/wheel::gentoo
"""
        (unmask_dir / "gentoo-infra").write_text(unmask_content)

        # 7. Create package.license whitelist
        license_dir = self.etc_portage / "package.license"
        license_dir.mkdir()
        self._write_package_license(license_dir / "fuse-overlay")

        # 8. Create empty env directory and package.env to prevent host env file loading
        # The host's /etc/portage/package.env may reference files like pax-mprotect.conf
        # that cause parsing errors. We override with empty ones.
        (self.etc_portage / "env").mkdir()
        (self.etc_portage / "package.env").write_text("# Empty - override host config\n")

        # 8b. Create other empty config files/dirs to fully isolate from host
        # These prevent any host config from leaking through
        (self.etc_portage / "package.use").write_text("# Empty - override host config\n")
        (self.etc_portage / "package.accept_keywords").write_text("# Empty - override host config\n")

        # 9. Load portage with custom config
        # Use config_root as target_root too, so the vartree is empty
        # (the var/db/pkg we created is empty)
        self.trees = portage.create_trees(
            config_root=str(self.config_root),
            target_root=str(self.config_root)
        )
        self.eroot = list(self.trees.keys())[0]
        self.settings = self.trees[self.eroot]["vartree"].settings

        # 10. Initialize root_config for each tree (required by depgraph)
        self._init_root_configs()

    def tearDown(self):
        """Clean up temporary directory."""
        if hasattr(self, 'temp_dir') and self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _init_root_configs(self):
        """
        Initialize root_config for each tree.

        This is required by the depgraph resolver. The process mirrors what
        emerge does in _emerge/actions.py load_emerge_config().
        """
        from portage._sets import load_default_config
        from _emerge.RootConfig import RootConfig

        for root_trees in self.trees.values():
            settings = root_trees["vartree"].settings
            settings._init_dirs()
            setconfig = load_default_config(settings, root_trees)
            root_config = RootConfig(settings, root_trees, setconfig)
            root_trees["root_config"] = root_config

    def _find_fuse_location(self) -> Path:
        """
        Read real repos.conf to find FUSE mount location.

        Searches for a repo that:
        1. Has section name matching OVERLAY_REPO_NAME, or
        2. Has 'pip-fuse' in section name, or
        3. Has repo_name file containing OVERLAY_REPO_NAME

        Returns:
            Path to the FUSE repository location
        """
        repos_conf = Path("/etc/portage/repos.conf")
        config = configparser.ConfigParser()

        if repos_conf.is_dir():
            for f in repos_conf.glob("*.conf"):
                config.read(f)
        elif repos_conf.exists():
            config.read(repos_conf)

        # First, look for exact section name match
        if OVERLAY_REPO_NAME in config.sections():
            loc = config[OVERLAY_REPO_NAME].get('location', '')
            if loc:
                return Path(loc)

        # Second, look for 'pip-fuse' in section name
        for section in config.sections():
            if 'pip-fuse' in section.lower():
                loc = config[section].get('location', '')
                if loc:
                    return Path(loc)

        # Third, check all locations for repo_name file matching ours
        for section in config.sections():
            loc = config[section].get('location', '')
            if loc:
                path = Path(loc)
                repo_name_file = path / "profiles" / "repo_name"
                if repo_name_file.exists():
                    try:
                        if repo_name_file.read_text().strip() == OVERLAY_REPO_NAME:
                            return path
                    except Exception:
                        pass

        return DEFAULT_FUSE_LOCATION

    def _write_make_conf(self):
        """Write make.conf with single PYTHON_TARGET."""
        content = f'''# Generated for portage-pip-fuse integration test
PYTHON_TARGETS="{TEST_PYTHON_TARGET}"
PYTHON_SINGLE_TARGET="{TEST_PYTHON_TARGET}"
ACCEPT_KEYWORDS="amd64"
FEATURES="-news"
'''
        (self.etc_portage / "make.conf").write_text(content)

    def _write_repos_conf(self):
        """Write repos.conf with gentoo (for eclasses) + FUSE overlay."""
        content = f'''[DEFAULT]
main-repo = {OVERLAY_REPO_NAME}

[gentoo]
location = {GENTOO_LOCATION}
auto-sync = no

[{OVERLAY_REPO_NAME}]
location = {self.fuse_location}
auto-sync = no
priority = 100
eclass-overrides = gentoo
'''
        (self.etc_portage / "repos.conf").write_text(content)

    def _write_package_license(self, path: Path):
        """
        Write package.license whitelist for FUSE overlay.

        This allows all common licenses used by PyPI packages.
        """
        content = f'''# Whitelist all common licenses for our FUSE overlay
dev-python/*::{OVERLAY_REPO_NAME} all-rights-reserved
dev-python/*::{OVERLAY_REPO_NAME} MIT BSD BSD-2 Apache-2.0
dev-python/*::{OVERLAY_REPO_NAME} GPL-2 GPL-2+ GPL-3 GPL-3+
dev-python/*::{OVERLAY_REPO_NAME} LGPL-2.1 LGPL-3 PSF-2 PSF-2.4
dev-python/*::{OVERLAY_REPO_NAME} public-domain ISC MPL-2.0 Unlicense CC0-1.0
dev-python/*::{OVERLAY_REPO_NAME} HPND WTFPL Artistic-2 ZPL
'''
        path.write_text(content)

    def _translate_name(self, pypi_name: str) -> str:
        """
        Translate PyPI package name to Gentoo package name.

        Args:
            pypi_name: The PyPI package name

        Returns:
            The Gentoo package name
        """
        from portage_pip_fuse.name_translator import pypi_to_gentoo
        return pypi_to_gentoo(pypi_name)

    # =========================================================================
    # Test Methods
    # =========================================================================

    def test_fuse_mounted(self):
        """Verify FUSE filesystem is accessible."""
        self.assertTrue(
            self.fuse_location.exists(),
            f"FUSE location does not exist: {self.fuse_location}"
        )

        repo_name_file = self.fuse_location / "profiles" / "repo_name"
        self.assertTrue(
            repo_name_file.exists(),
            f"repo_name file not found: {repo_name_file}"
        )

        repo_name = repo_name_file.read_text().strip()
        self.assertEqual(
            repo_name, OVERLAY_REPO_NAME,
            f"Unexpected repo name: {repo_name}"
        )

    def test_repositories_configured(self):
        """Verify both repos are loaded correctly."""
        porttree = self.trees[self.eroot]["porttree"]
        repos = porttree.dbapi.repositories

        # Check gentoo repo is loaded (for eclasses)
        self.assertIn(
            "gentoo", repos,
            "Gentoo repo not loaded - eclasses won't be available"
        )

        # Check our FUSE repo is loaded
        self.assertIn(
            OVERLAY_REPO_NAME, repos,
            f"FUSE repo '{OVERLAY_REPO_NAME}' not loaded"
        )

        # Verify our repo is the main repo
        main_repo = repos.mainRepo()
        self.assertIsNotNone(main_repo, "No main repo configured")
        self.assertEqual(
            main_repo.name, OVERLAY_REPO_NAME,
            f"Main repo is '{main_repo.name}', expected '{OVERLAY_REPO_NAME}'"
        )

    def test_gentoo_packages_masked(self):
        """Verify dev-python packages from gentoo are masked."""
        portdb = self.trees[self.eroot]["porttree"].dbapi

        # Test with a common dev-python package that exists in gentoo
        # This should only find packages from our FUSE repo, not gentoo
        matches = portdb.match("dev-python/requests")

        for match in matches:
            repo = portdb.aux_get(match, ["repository"])[0]
            self.assertNotEqual(
                repo, "gentoo",
                f"Found unmasked gentoo package: {match}"
            )

    def test_python_targets_configured(self):
        """Verify PYTHON_TARGETS is set correctly."""
        python_targets = self.settings.get("PYTHON_TARGETS", "")
        self.assertEqual(
            python_targets, TEST_PYTHON_TARGET,
            f"PYTHON_TARGETS mismatch: got '{python_targets}', "
            f"expected '{TEST_PYTHON_TARGET}'"
        )

        python_single_target = self.settings.get("PYTHON_SINGLE_TARGET", "")
        self.assertEqual(
            python_single_target, TEST_PYTHON_TARGET,
            f"PYTHON_SINGLE_TARGET mismatch: got '{python_single_target}', "
            f"expected '{TEST_PYTHON_TARGET}'"
        )

    def test_test_package_exists(self):
        """Verify the test package exists in our FUSE repo."""
        portdb = self.trees[self.eroot]["porttree"].dbapi

        gentoo_name = self._translate_name(TEST_PACKAGE_NAME)
        atom = f"dev-python/{gentoo_name}"

        matches = portdb.match(atom)
        self.assertTrue(
            len(matches) > 0,
            f"Test package not found: {atom}"
        )

        # Verify it comes from our repo
        for match in matches:
            repo = portdb.aux_get(match, ["repository"])[0]
            self.assertEqual(
                repo, OVERLAY_REPO_NAME,
                f"Package {match} from unexpected repo: {repo}"
            )

    def test_resolve_dependency_tree(self):
        """
        Test full dependency tree resolution using ONLY our FUSE repo.

        This is the main integration test. If this passes, the FUSE
        implementation is serving valid ebuilds that portage can resolve.
        """
        # Import emerge components
        try:
            from _emerge.create_depgraph_params import create_depgraph_params
            from _emerge.depgraph import backtrack_depgraph
            from _emerge.stdout_spinner import stdout_spinner
        except ImportError as e:
            self.skipTest(f"Emerge components not available: {e}")

        myopts = {
            '--ignore-world': True,
            '--deep': True,
            '--pretend': True,
            '--verbose': True,
        }

        myparams = create_depgraph_params(myopts, myaction=None)
        myparams['ignore_world'] = True

        gentoo_name = self._translate_name(TEST_PACKAGE_NAME)
        atom = f"dev-python/{gentoo_name}"

        # Create spinner based on TEST_SHOW_SPINNER setting
        spinner = stdout_spinner()
        if not TEST_SHOW_SPINNER:
            spinner.update = lambda: None

        try:
            success, mydepgraph, favorites = backtrack_depgraph(
                self.settings,
                self.trees,
                myopts,
                myparams,
                myaction=None,
                myfiles=[atom],
                spinner=spinner
            )

            if not success:
                # Get diagnostic information
                diag_msg = f"Dependency resolution failed for {atom}."

                # Try to get more details from the depgraph
                if mydepgraph:
                    try:
                        # Check for masked packages
                        dynamic_config = mydepgraph._dynamic_config
                        if hasattr(dynamic_config, '_unsatisfied_deps_for_display'):
                            unsatisfied = dynamic_config._unsatisfied_deps_for_display
                            if unsatisfied:
                                diag_msg += f"\nUnsatisfied deps: {unsatisfied}"
                    except Exception:
                        pass

                # Optionally re-run with debug for more diagnostics
                if TEST_DEBUG_ON_FAILURE:
                    myopts['--debug'] = True
                    myparams = create_depgraph_params(myopts, myaction=None)
                    myparams['ignore_world'] = True

                    success_retry, mydepgraph_retry, _ = backtrack_depgraph(
                        self.settings,
                        self.trees,
                        myopts,
                        myparams,
                        myaction=None,
                        myfiles=[atom],
                        spinner=spinner
                    )
                    diag_msg += f"\nRetry with debug also {'succeeded' if success_retry else 'failed'}."

                self.fail(
                    f"{diag_msg}\n"
                    f"All dev-python packages should come from FUSE overlay."
                )

            # Count resolved packages
            if mydepgraph:
                try:
                    pkgs = list(
                        mydepgraph._dynamic_config._package_tracker.all_pkgs()
                    )
                    print(f"\nSuccessfully resolved {len(pkgs)} packages for {atom}")

                    # Verify all packages from our repo
                    for pkg in pkgs:
                        self.assertEqual(
                            pkg.repo, OVERLAY_REPO_NAME,
                            f"Package {pkg.cpv} from unexpected repo: {pkg.repo}"
                        )

                    # Print some stats
                    if len(pkgs) > 0:
                        print(f"First few packages: {[str(p.cpv) for p in pkgs[:5]]}")
                        if len(pkgs) > 5:
                            print(f"  ... and {len(pkgs) - 5} more")

                except Exception as e:
                    # Package counting failed but resolution succeeded
                    print(f"Warning: Could not count packages: {e}")

        except Exception as e:
            self.fail(f"Resolver exception: {e}")


# =============================================================================
# Additional Test Cases
# =============================================================================

@unittest.skipUnless(PORTAGE_AVAILABLE, "Portage not available")
class TestPortageBasicOperations(unittest.TestCase):
    """
    Basic portage operations test without full isolation.

    These tests verify the FUSE filesystem provides basic required
    functionality without the overhead of full isolation setup.
    """

    def setUp(self):
        """Find FUSE location."""
        self.fuse_location = self._find_fuse_location()
        if not (self.fuse_location / "profiles" / "repo_name").exists():
            self.skipTest(f"FUSE not mounted at {self.fuse_location}")

    def _find_fuse_location(self) -> Path:
        """Read real repos.conf to find FUSE mount location."""
        repos_conf = Path("/etc/portage/repos.conf")
        config = configparser.ConfigParser()

        if repos_conf.is_dir():
            for f in repos_conf.glob("*.conf"):
                config.read(f)
        elif repos_conf.exists():
            config.read(repos_conf)

        # First, look for exact section name match
        if OVERLAY_REPO_NAME in config.sections():
            loc = config[OVERLAY_REPO_NAME].get('location', '')
            if loc:
                return Path(loc)

        # Second, look for 'pip-fuse' in section name
        for section in config.sections():
            if 'pip-fuse' in section.lower():
                loc = config[section].get('location', '')
                if loc:
                    return Path(loc)

        # Third, check all locations for repo_name file matching ours
        for section in config.sections():
            loc = config[section].get('location', '')
            if loc:
                path = Path(loc)
                repo_name_file = path / "profiles" / "repo_name"
                if repo_name_file.exists():
                    try:
                        if repo_name_file.read_text().strip() == OVERLAY_REPO_NAME:
                            return path
                    except Exception:
                        pass

        return DEFAULT_FUSE_LOCATION

    def test_profiles_directory_structure(self):
        """Verify profiles directory has required structure."""
        profiles = self.fuse_location / "profiles"

        self.assertTrue(profiles.exists(), "profiles directory missing")
        self.assertTrue(
            (profiles / "repo_name").exists(),
            "profiles/repo_name missing"
        )

        # Verify repo_name content
        repo_name = (profiles / "repo_name").read_text().strip()
        self.assertEqual(
            repo_name, OVERLAY_REPO_NAME,
            f"Unexpected repo name: {repo_name}"
        )

        # Note: categories file is optional for overlays with eclass-overrides

    def test_dev_python_category_exists(self):
        """Verify dev-python category directory exists."""
        dev_python = self.fuse_location / "dev-python"
        self.assertTrue(
            dev_python.exists(),
            "dev-python category directory missing"
        )
        self.assertTrue(
            dev_python.is_dir(),
            "dev-python is not a directory"
        )

    def test_can_list_packages(self):
        """Verify we can list packages in dev-python."""
        dev_python = self.fuse_location / "dev-python"

        # Should be able to list directory
        try:
            entries = list(dev_python.iterdir())
            # Should have at least some packages
            self.assertTrue(
                len(entries) > 0,
                "No packages found in dev-python"
            )
        except Exception as e:
            self.fail(f"Failed to list dev-python: {e}")


if __name__ == "__main__":
    unittest.main()
