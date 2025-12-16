# Changelog

All notable changes to mailcore-imapclient will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Folder cache corruption on SELECT failure** (Story 3.11.3)
  - `_select_folder()` now invalidates cache when IMAP SELECT command fails
  - Prevents stale cache state causing "No mailbox selected" errors on subsequent operations
  - Failed SELECT on non-existent folder no longer corrupts adapter state
  - One bad folder access no longer breaks all subsequent folder operations

### Changed - BREAKING

- **IMAPClientAdapter now raises FolderNotFoundError for missing folders** (Story 3.11.3)
  - When IMAP SELECT fails with "nonexistent namespace", "does not exist", or "no such mailbox" errors, adapter wraps in `mailcore.FolderNotFoundError`
  - Exception includes clear error message: `"Folder '{folder}' does not exist"`
  - Original IMAP exception preserved in exception chain (accessible via `__cause__`)
  - **Breaking Change**: Code catching generic `Exception` for folder operations should catch `FolderNotFoundError` instead
  - Example:
    ```python
    from mailcore import FolderNotFoundError
    
    try:
        messages = await mailbox.folders["NONEXISTENT"].list()
    except FolderNotFoundError as e:
        print(f"Folder not found: {e.folder}")
    ```

### Changed - BREAKING

- **IMAPClientAdapter._parse_flags() signature changed** (Story 3.11.1)
  - Return type changed from `list[str]` to `tuple[set[MessageFlag], set[str]]`
  - Standard IMAP flags (\\Seen, \\Flagged, etc.) → MessageFlag enum (first element)
  - Custom IMAP keywords ($Forwarded, $MDNSent, etc.) → strings (second element)
  - Added `_imap_to_flag()` helper method to map IMAP flag strings to MessageFlag enum
  - This change was made before v1.0.0 release to avoid semver break

- **IMAPClientAdapter._parse_message() updated** (Story 3.11.1)
  - Passes both `flags` and `custom_flags` to Message constructor
  - Maintains compatibility with mailcore Message API changes

### Migration Guide

This is an internal API change. If you've subclassed `IMAPClientAdapter` and overridden `_parse_flags()`:

**Before (v0.x):**
```python
def _parse_flags(self, flags: tuple[bytes, ...]) -> list[str]:
    return [flag.decode() for flag in flags]
```

**After (v1.0+):**
```python
def _parse_flags(self, flags: tuple[bytes, ...]) -> tuple[set[MessageFlag], set[str]]:
    standard_flags: set[MessageFlag] = set()
    custom_flags: set[str] = set()
    
    for flag in flags:
        flag_str = flag.decode()
        message_flag = self._imap_to_flag(flag_str)
        if message_flag is not None:
            standard_flags.add(message_flag)
        else:
            custom_flags.add(flag_str)
    
    return (standard_flags, custom_flags)
```

**Rationale:**

This change aligns IMAPClientAdapter with the mailcore Message API update (Story 3.11.1). Adapters are responsible for translating protocol representations (IMAP flag bytes) to domain types (MessageFlag enum), ensuring type consistency across the entire mailcore ecosystem.

## [1.0.0] - TBD

Initial release (in development).
