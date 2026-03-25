## v1.0.4 (2026-03-24)

### Feat

- **first-run-installer**: adds interactive first-time dependency installer dialog with progress indicator and live installation logs
- **smart-skip**: skips installer dialog when dependencies are already importable and marks install state automatically

### Fix

- **mac-runtime-install**: runs pip in-process first to avoid macOS QGIS executable argument parsing issues during dependency installation
- **installer-reliability**: keeps subprocess fallback with improved Python executable resolution and clearer runtime logs
- **reinstall-ux**: removes silent background reinstall path and always uses interactive dependency flow when packages are missing, with a 1-second delayed start message to avoid perceived startup hang

## v1.0.3 (2026-03-24)

### Feat

- **runtime-deps**: switches plugin packaging to bundled-pip runtime installation for first-run dependency setup
- **version-config**: centralizes runtime dependency and pip versions in a single shared configuration module

### Fix

- **bootstrap**: cleans dependency bootstrap flow and improves runtime install fallback/error handling
- **packaging**: reduces plugin artifact size for store compatibility by avoiding pre-bundled heavy dependency trees

## v1.0.2 (2026-03-24)

### Feat

- **osm-search**: adds OpenStreetMap (OSM) location search in the Places tab for quick AOI discovery and navigation
- **results-section**: adds comprehensive Results tab to display Engine outputs with Aggregate/Timeseries/Trend views, session history persistence, and layer loading to map

### Fix

- **extractor-windows-crash**: fixes "access violation" crash on second Extractor run by reloading pyproj modules to clear corrupted PROJ state

## v1.0.0 (2026-03-22)

### Feat

- **scene-preview**: adds integrated scene preview dialog for Engine and Extractor with temporary footprint rendering
- **scene-selection**: uses list selection to control preview-map footprint visibility with Select All and Deselect All
- **preview-map**: adds OSM basemap and CRS-safe footprint projection in preview window
- **aoi**: separates Engine and Extractor AOI layers with distinct names and colors
- **extractor**: adds runtime log/progress behavior parity with Engine
- **toolbar**: adds dedicated VirtuGhan toolbar with separate Engine, Extractor, and Tiler launch icons
- **navigation-icons**: adds module-specific tab/toolbar icons (gear, download, tiles) for clearer module access
- **run-ux**: moves Engine/Extractor run controls into cleaner two-row action layout

### Fix

- **drawing-tools**: prevents cross-module draw tool reactivation when switching Engine/Extractor AOI interactions
- **aoi-clear**: improves AOI clear/refresh behavior for immediate canvas update
- **basemap-detection**: ignores temporary registry-only OSM layers during startup map setup
- **preview-ux**: shows loading state while searching scenes and uses close-only preview flow
- **footprint-default**: sets "Show matching scene footprints on map" to unchecked by default
- **footprint-timing**: prevents preview step from adding footprints to main map before processing completes
- **footprint-fallback**: auto-fetches scenes from current filters when preview selection is empty at post-run render time
- **footprint-order**: inserts footprint layer below generated result rasters to avoid blocking analysis outputs
- **footprint-logging**: logs real footprint add counts and avoids false "added to map" messages when zero features are rendered
- **hub-layout**: increases hub dialog height for better initial log visibility

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
