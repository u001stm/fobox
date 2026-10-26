# LUMIO release preservation

- The approved version v4.20.1 is frozen. Never rewrite or move its tag or replace its published assets.
- Preserve all approved UI and workflow behavior documented in README.md and RELEASE_LOCK.md.
- New changes require a new version number and release; work in a separate branch and keep the frozen snapshot recoverable.
- Never commit photo-session catalogs, RAW/JPEG photos, exported orders, credentials, OAuth tokens, or local settings.
- XMP generation is intentionally removed. Do not restore it.
- Face matching searches the complete shoot. Do not reintroduce fixed frame-number cutoffs or mix sessions that reuse a memory-card path.
