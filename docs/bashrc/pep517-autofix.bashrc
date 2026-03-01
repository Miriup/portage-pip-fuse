#!/bin/bash
# Auto-fix PEP517 backend mismatches for portage-pip-fuse
#
# This hook detects when a portage-pip-fuse package fails to build due to
# a PEP517 backend mismatch and either auto-patches the backend or provides
# instructions for manual fixing.
#
# Installation:
#   Copy to /etc/portage/bashrc
#   OR source from your existing bashrc:
#     source /path/to/pep517-autofix.bashrc
#
# Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
# Licensed under GPL-2.0

# Map pyproject.toml build-backend names to DISTUTILS_USE_PEP517 values
_pep517_backend_map() {
    local backend="$1"
    case "$backend" in
        flit_core.*|flit.*)           echo "flit" ;;
        hatchling.*)                  echo "hatchling" ;;
        poetry.core.*|poetry_core.*)  echo "poetry" ;;
        pdm.backend.*|pdm.pep517.*)   echo "pdm-backend" ;;
        setuptools.*)                 echo "setuptools" ;;
        maturin.*)                    echo "maturin" ;;
        mesonpy.*|meson_python.*)     echo "meson-python" ;;
        scikit_build_core.*)          echo "scikit-build-core" ;;
        sipbuild.*)                   echo "sip" ;;
        *)                            echo "" ;;
    esac
}

_pep517_die_hook() {
    # Only process dev-python packages from portage-pip-fuse
    [[ "${CATEGORY}" == "dev-python" ]] || return
    [[ "${PORTAGE_REPO_NAME}" == "portage-pip-fuse" ]] || return
    [[ "${EBUILD_PHASE}" == "compile" ]] || return

    local build_log="/var/tmp/portage/${CATEGORY}/${PF}/temp/build.log"
    [[ -f "${build_log}" ]] || return

    # Check for PEP517 mismatch error patterns
    # Pattern 1: distutils-r1.eclass explicit error
    local has_mismatch=""
    if grep -q "DISTUTILS_USE_PEP517 value incorrect" "${build_log}" 2>/dev/null; then
        has_mismatch="explicit"
    fi
    # Pattern 2: ModuleNotFoundError for build backend
    if grep -q "ModuleNotFoundError: No module named" "${build_log}" 2>/dev/null; then
        # Check if it's a backend module
        if grep -E "No module named '(flit|hatch|poetry|pdm|maturin|mesonpy|sip)" "${build_log}" 2>/dev/null; then
            has_mismatch="missing_module"
        fi
    fi

    [[ -n "${has_mismatch}" ]] || return

    # Try to extract the actual backend from pyproject.toml in the build directory
    local workdir="/var/tmp/portage/${CATEGORY}/${PF}/work"
    local pyproject=""
    local actual_backend=""

    # Find pyproject.toml
    if [[ -d "${workdir}" ]]; then
        pyproject=$(find "${workdir}" -maxdepth 2 -name "pyproject.toml" -type f 2>/dev/null | head -1)
    fi

    if [[ -f "${pyproject}" ]]; then
        # Extract build-backend from pyproject.toml
        actual_backend=$(grep -E '^build-backend\s*=' "${pyproject}" 2>/dev/null | \
            sed -E 's/^build-backend\s*=\s*"([^"]+)".*/\1/' | \
            sed -E "s/^build-backend\s*=\s*'([^']+)'.*/\1/")
    fi

    # If we couldn't find it in pyproject.toml, try to parse from build.log
    if [[ -z "${actual_backend}" ]] && [[ "${has_mismatch}" == "explicit" ]]; then
        actual_backend=$(grep "pyproject.toml:" "${build_log}" 2>/dev/null | \
            tail -1 | sed 's/.*pyproject.toml:\s*//')
    fi

    [[ -n "${actual_backend}" ]] || return

    # Map to DISTUTILS_USE_PEP517 value
    local pep517_value
    pep517_value=$(_pep517_backend_map "${actual_backend}")
    [[ -n "${pep517_value}" ]] || return

    # Determine .sys path - try common mount points
    local sys_path=""
    local mount_point=""
    for mp in "/var/db/repos/pypi" "/var/db/repos/portage-pip-fuse"; do
        if [[ -d "${mp}/.sys" ]]; then
            mount_point="${mp}"
            sys_path="${mp}/.sys/pep517/${CATEGORY}/${PN}/${PV}"
            break
        fi
    done

    [[ -n "${sys_path}" ]] || {
        ewarn "PEP517 mismatch detected but could not find portage-pip-fuse mount point"
        return
    }

    local sys_dir
    sys_dir=$(dirname "${sys_path}")

    # Try to fix (may fail if in sandbox or no write access)
    # The FUSE filesystem handles the mkdir and writes to in-memory + JSON storage
    if mkdir -p "${sys_dir}" 2>/dev/null && echo "${pep517_value}" > "${sys_path}" 2>/dev/null; then
        ewarn "=============================================="
        ewarn "PEP517 Backend Auto-Patched!"
        ewarn "=============================================="
        ewarn "Detected: ${actual_backend}"
        ewarn "Patched to: DISTUTILS_USE_PEP517=${pep517_value}"
        ewarn ""
        ewarn "Re-run emerge to build with the corrected backend:"
        ewarn "  emerge -1 ${CATEGORY}/${PN}"
        ewarn "=============================================="
    else
        # Sandbox likely blocked the write, provide manual instructions
        ewarn "=============================================="
        ewarn "PEP517 Backend Mismatch Detected!"
        ewarn "=============================================="
        ewarn "The package uses: ${actual_backend}"
        ewarn "Expected DISTUTILS_USE_PEP517=${pep517_value}"
        ewarn ""
        ewarn "To fix, run:"
        ewarn "  echo '${pep517_value}' > ${sys_path}"
        ewarn ""
        ewarn "Then re-emerge the package:"
        ewarn "  emerge -1 ${CATEGORY}/${PN}"
        ewarn "=============================================="
    fi
}

# Register the die hook
register_die_hook _pep517_die_hook
