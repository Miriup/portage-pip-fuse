# Coding Style Guide for portage-pip-fuse

This document outlines the coding style and development practices for the portage-pip-fuse project.

## Project Goals and Non-Goals

### Primary Goals
- Provide FUSE filesystem interface for PyPI packages to Gentoo portage
- Filter packages by Python compatibility with the system (PYTHON_TARGETS)
- Filter packages to only those with source distributions available
- Support dependency resolution for specific packages with USE flags
- Maintain high performance with comprehensive caching

### Non-Goals  
- **Curated package lists**: The curated filter is NOT intended for production use. We need automatic filtering based on system compatibility, not manual curation.
- **Supporting all PyPI packages**: Only packages compatible with system Python and having source distributions should be visible.

## Key Design Decisions

### Default Filters  
The following filters MUST work properly by default:
1. **source-dist**: Filters packages to those with source distributions (required for Gentoo's build-from-source philosophy)
2. **python-compat**: MUST be implemented efficiently to filter packages by system PYTHON_TARGETS
   - Current implementation needs work - checking all 746k packages is impractical
   - Should work on cached/indexed packages or use a more efficient approach
   - This is ESSENTIAL for manageable package counts

### Python Compatibility Enforcement
Python compatibility is enforced at TWO levels:
1. **Pre-filtering**: The python-compat filter reduces visible packages to compatible ones
2. **Ebuild level**: PYTHON_COMPAT generation ensures accurate compatibility declarations

### Bug Fixing Philosophy
- **Never disable features to "fix" bugs** - properly implement them instead
- **Filters are essential** - the python-compat filter is critical because PyPI has too many packages to handle without filtering
- **Performance matters** - filters must be efficient enough to be practical
- **Default behavior must be sensible** - showing incompatible packages wastes resources and confuses users

## General Principles

1. **Use Original Code When Possible**: Whenever possible, utilize original pip or portage code from the reference repositories instead of reimplementing functionality.

2. **Documentation First**: All modules, classes, and methods should have comprehensive documentation including doctests.

3. **Extensibility**: Design interfaces that allow for future extensions (e.g., caching, preloading).

## Code Style

### Python Version

- Target Python 3.8+ (minimum version available in Gentoo main repository)
- Use type hints for all function signatures
- Avoid features only available in newer Python versions

### Formatting

- **Line Length**: Maximum 100 characters (configured in pyproject.toml)
- **Indentation**: 4 spaces (no tabs)
- **String Quotes**: Use double quotes for docstrings, single quotes for other strings
- **Imports**: Group in order: standard library, third-party, local imports

### Tools

```bash
# Format code
black portage_pip_fuse --line-length 100

# Sort imports
isort portage_pip_fuse --profile black --line-length 100

# Type checking
mypy portage_pip_fuse

# Linting
flake8 portage_pip_fuse --max-line-length 100
```

## Documentation Standards

### Module Documentation

Every module should start with:
```python
"""
Brief description of the module.

Longer description explaining the purpose, design decisions,
and any important context.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""
```

### Function/Method Documentation

All functions and methods must have docstrings with:
- Brief description
- Args section (if applicable)
- Returns section (if applicable)
- Raises section (if applicable)
- Examples section with doctests

Example:
```python
def translate_name(self, name: str) -> str:
    """
    Brief description of what the function does.
    
    Longer explanation if needed.
    
    Args:
        name: Description of the parameter
        
    Returns:
        Description of the return value
        
    Raises:
        ValueError: When and why this is raised
        
    Examples:
        >>> obj = MyClass()
        >>> obj.translate_name("example")
        'result'
    """
```

### Doctests

- All public methods should include doctests in their docstrings
- Doctests should cover typical use cases and edge cases
- Use realistic examples from actual PyPI/Gentoo packages
- Run doctests with: `python -m doctest portage_pip_fuse/*.py -v`

## Design Patterns

### Abstract Base Classes

Use ABC for interfaces that will have multiple implementations:

```python
from abc import ABC, abstractmethod

class TranslatorBase(ABC):
    @abstractmethod
    def translate(self, name: str) -> str:
        pass
```

### Caching and Performance

- Design interfaces that allow caching to be added later
- Use simple implementations first, optimize when needed
- Document performance characteristics in docstrings

### Error Handling

- Use strict_mode parameter for toggling between exceptions and best-effort
- Provide clear error messages that help users understand the problem
- Document all exceptions that can be raised

## Testing

### Unit Tests

- Test files go in `tests/` directory
- Name test files as `test_*.py`
- Use pytest for testing framework
- Include both positive and negative test cases

### Test Coverage

- Aim for high test coverage (>80%)
- Test edge cases and error conditions
- Use real-world data when possible (e.g., actual PyPI package names)

### Integration Tests

- Test with actual pip and portage infrastructure when available
- Use the reference repositories for validation
- Document any external dependencies needed for tests

## Import Strategy

### From Reference Repositories

When using code from reference implementations:

```python
# Try to import from pip's vendored libraries first
try:
    from pip._vendor.packaging.utils import canonicalize_name
except ImportError:
    # Fallback to our own implementation
    def canonicalize_name(name: str) -> str:
        # Implementation based on the standard
        pass
```

### Conditional Imports

Always provide fallbacks for optional dependencies:

```python
try:
    import optional_module
    HAS_OPTIONAL = True
except ImportError:
    HAS_OPTIONAL = False
```

## Commit Messages

Follow conventional commit format:

```
type: brief description

Longer explanation of the change, why it was needed,
and any important context.

- Bullet points for specific changes
- Another change
```

Types: feat, fix, docs, style, refactor, test, chore

## File Headers

All Python files should include copyright and license information:

```python
"""
Module description.

Copyright (C) 2026 Dirk Tilger <dirk@systemication.com>
Licensed under GPL-2.0
"""
```

## Naming Conventions

### Variables and Functions
- Use snake_case for variables and functions
- Be descriptive but concise
- Avoid abbreviations unless widely understood

### Classes
- Use PascalCase for class names
- Suffix with Base for abstract classes
- Suffix with Mixin for mixin classes

### Constants
- Use UPPER_SNAKE_CASE for module-level constants
- Group related constants together

### Private Members
- Prefix with single underscore for internal use
- Double underscore only when name mangling is specifically needed

## Package Organization

```
portage_pip_fuse/
├── __init__.py          # Package initialization, version info
├── name_translator.py   # Name translation logic
├── filesystem.py        # FUSE filesystem implementation
├── cli.py              # Command-line interface
├── cache.py            # Caching utilities (future)
├── metadata.py         # Package metadata handling (future)
└── utils.py            # Shared utilities
```

## Performance Considerations

- Profile before optimizing
- Document algorithmic complexity in docstrings
- Consider memory usage for large-scale operations
- Use generators for large datasets when possible

## Security

- Never log or expose sensitive information
- Validate all user input
- Use safe file operations
- Follow principle of least privilege

## Compatibility

- Maintain compatibility with both GPL-2.0 (portage) and MIT (pip) code
- Document any compatibility constraints
- Test on Gentoo Linux systems
- Ensure FUSE compatibility across kernel versions