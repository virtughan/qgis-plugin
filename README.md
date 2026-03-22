# VirtuGhan QGIS Plugin

A QGIS plugin that integrates [VirtuGhan](https://pypi.org/project/virtughan/) capabilities directly into QGIS for remote sensing workflows.

## Features

- **Tiler**: Real-time satellite tile visualization with custom band combinations
- **Engine**: Process and analyze satellite imagery with spectral indices
- **Extractor**: Bulk download and stack satellite data

## Quick Start

### Environment Management

This project uses [uv](https://docs.astral.sh/uv/) for Python dependency and environment management. You must install uv before setting up the project.

**Install uv first:**
Follow the installation guide at: https://docs.astral.sh/uv/getting-started/installation/

### Prerequisites

- QGIS 3.22 or higher
- Python 3.10+
- uv (for dependency management)

### Installation

1. Clone the repository:
```bash
git clone https://github.com/virtughan/qgis-plugin.git
cd qgis-plugin
```

2. Set up development environment with uv:
```bash
uv sync
```

3. Build the plugin (use **one** command):

- Any OS:
```bash
python build.py
```
OR

- Windows (PowerShell):
```powershell
.\build.ps1
```
- Linux/macOS:
```bash
./build.sh
```

4. Install in QGIS:
   - Go to `Plugins > Manage and Install Plugins > Install from ZIP`
   - Select `dist/virtughan-qgis-plugin.zip`

## Development

### Environment Setup

```bash
uv sync --group dev
source .venv/bin/activate
```

### Version Management

This project uses [Commitizen](https://commitizen-tools.github.io/commitizen/) for version management:

```bash
cz bump
cz changelog
```

### Building

Use **one** of the following commands:

- Any OS:
```bash
python build.py
```
- Windows (PowerShell):
```powershell
.\build.ps1
```
- Linux/macOS:
```bash
./build.sh
```

The build script:
- Generates `metadata.txt` from `pyproject.toml`
- Creates a clean plugin package
- Outputs `dist/virtughan-qgis-plugin.zip`

## Links

- [Live Demo](https://virtughan.com/)
- [VirtuGhan Package](https://pypi.org/project/VirtuGhan/)
- [Documentation](https://github.com/virtughan)

## License

GPL-3.0





