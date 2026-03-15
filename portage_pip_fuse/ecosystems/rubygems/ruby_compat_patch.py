"""
USE_RUBY compatibility patching system for Ruby implementation support.

This module provides a virtual filesystem API for overriding auto-detected USE_RUBY
values that may be incorrect or overly restrictive.

Patch Operations:
- ADD (++): Add Ruby implementation to USE_RUBY
- REMOVE (--): Remove Ruby implementation from USE_RUBY
- SET (==): Replace entire USE_RUBY list

Patch File Format:
    ++ ruby34              # Add implementation
    -- ruby32              # Remove implementation
    == ruby32 ruby33 ruby34   # Set explicit list (replace all)

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""

import logging
import re
from typing import List

from portage_pip_fuse.compat_patch import CompatPatchStore
from .ruby_targets import get_all_ruby_impls

logger = logging.getLogger(__name__)

# Pattern for Ruby implementation names (ruby31, ruby32, ruby33, ruby34, etc.)
RUBY_IMPL_PATTERN = re.compile(r'^ruby\d+$')


class RubyCompatPatchStore(CompatPatchStore):
    """
    USE_RUBY compatibility patching for RubyGems.

    This class manages patches that override Ruby implementation compatibility,
    persisting them to JSON and applying them during ebuild generation.

    Implementation validation is dynamic, using get_all_ruby_impls() to detect
    valid Ruby implementations from ruby-utils.eclass.

    Examples:
        >>> import tempfile
        >>> with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
        ...     store = RubyCompatPatchStore(f.name)
        >>> store.add_impl('dev-ruby', 'rails', '7.1.0', 'ruby34')
        >>> len(store.get_patches('dev-ruby', 'rails', '7.1.0'))
        1
        >>> import os; os.unlink(f.name)
    """

    @property
    def json_key(self) -> str:
        """Key used in JSON storage."""
        return 'ruby_compat_patches'

    def get_valid_impls(self) -> List[str]:
        """
        Get valid Ruby implementations from ruby-utils.eclass.

        Returns:
            List of Ruby implementation names (e.g., ['ruby32', 'ruby33', 'ruby34'])
        """
        return get_all_ruby_impls()

    def is_valid_impl(self, impl: str) -> bool:
        """
        Validate Ruby implementation name.

        Uses a two-step validation:
        1. Fast pattern check (must match ruby followed by digits pattern)
        2. Verification against eclass-detected implementations

        Args:
            impl: Implementation name to validate (e.g., 'ruby34')

        Returns:
            True if valid Ruby implementation, False otherwise

        Examples:
            >>> store = RubyCompatPatchStore()
            >>> store.is_valid_impl('ruby34')
            True
            >>> store.is_valid_impl('ruby32')
            True
            >>> store.is_valid_impl('python3_13')
            False
            >>> store.is_valid_impl('invalid')
            False
        """
        # Fast path: check pattern first
        if not RUBY_IMPL_PATTERN.match(impl):
            return False
        # Then verify against eclass
        return impl in self.get_valid_impls()
