## v1.0.0 (2026-03-22)

### Feat

- **scene-preview**: adds integrated scene preview dialog for Engine and Extractor with temporary footprint rendering
- **scene-selection**: uses list selection to control preview-map footprint visibility with Select All and Deselect All
- **preview-map**: adds OSM basemap and CRS-safe footprint projection in preview window
- **aoi**: separates Engine and Extractor AOI layers with distinct names and colors
- **extractor**: adds runtime log/progress behavior parity with Engine

### Fix

- **drawing-tools**: prevents cross-module draw tool reactivation when switching Engine/Extractor AOI interactions
- **aoi-clear**: improves AOI clear/refresh behavior for immediate canvas update
- **basemap-detection**: ignores temporary registry-only OSM layers during startup map setup
- **preview-ux**: shows loading state while searching scenes and uses close-only preview flow

## v0.2.1 (2025-08-22)

### Fix

- **vcube**: fixes vcube in the extractor
- **windows**: quite is not avilable in windows so fixed the bug
- **metadata**: remove metadata checking in test cases as build.sh will generate this automatically
- **test**: don't lookup inside the repo as the .qrc is outside
- **test**: adds test cases and fixes the ci with branch name
- **bootstrap**: fixes the automatic installation of package
- **version**: upgrade the virtughan

### Refactor

- **extractor**: tried to remove some ai codes and make it slightly friendly to maintain
- **cleanup**: adds cleanup to duplication and cache

## v0.2.0 (2025-08-22)

### Feat

- **ci**: adds ci for release and tags

## v0.1.1 (2025-08-22)

### Fix

- **meta**: builds metadata
