# Tasks

## Upstream Issues to Report

### ai-agents gem missing license in gemspec

**Gem:** ai-agents
**Version:** 0.9.0
**Repository:** https://github.com/chatwoot/ai-agents

**Issue:** The gem's LICENSE file specifies MIT, but the gemspec doesn't declare a license. This causes package managers (like Gentoo Portage) to mark the package as having an "unknown" license.

**Fix needed in upstream:**
Add to the gemspec:
```ruby
spec.license = "MIT"
```

Or for multiple licenses:
```ruby
spec.licenses = ["MIT"]
```

**Workaround applied:** Added `unknown` license to `/etc/portage/package.license`

**Status:** Not yet reported

**Gem:**: `gmail_xoauth`
**Version:** 0.4.3

**Issue:** Same as ai-agents

## Documentation Tasks

### Add RubyGems .sys patching examples

**Files to update:**
- `README.md` - Add RubyGems examples to "Patching and Customization" section
- `docs/dependency-patching.md` - Add RubyGems dependency patching examples
- `docs/build-error-fixes.md` - Add RubyGems-specific build error fixes

**Context:** The .sys patching mechanism is currently documented with PyPI examples only. With the simplified name translation (exact gem names, no heuristic matching), users will need to use .sys patches for name mismatches between gems and existing Gentoo packages.

**Status:** Not started
