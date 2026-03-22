import os
import subprocess
import sys
from pathlib import Path


def test_ci_workflow_basic():
    project_root = Path(__file__).parent.parent
    
    assert (project_root / ".github" / "workflows" / "ci.yml").exists()
    assert (project_root / ".github" / "workflows" / "release.yml").exists()


def test_uv_sync_works():
    project_root = Path(__file__).parent.parent
    
    result = subprocess.run([
        "uv", "sync", "--group", "test"
    ], cwd=str(project_root), capture_output=True, text=True)
    
    assert result.returncode == 0, f"uv sync failed: {result.stderr}"


def test_metadata_generation_works():
    project_root = Path(__file__).parent.parent
    
    result = subprocess.run([
        sys.executable, "generate_metadata.py"
    ], cwd=str(project_root), capture_output=True, text=True)
    
    assert result.returncode == 0, f"Metadata generation failed: {result.stderr}"


def test_build_produces_zip():
    project_root = Path(__file__).parent.parent
    
    result = subprocess.run([
        sys.executable, "build.py"
    ], cwd=str(project_root), capture_output=True, text=True)
    
    assert result.returncode == 0, f"Build failed: {result.stderr}"
    
    zip_file = project_root / "dist" / "virtughan-qgis-plugin.zip"
    assert zip_file.exists(), "ZIP file not produced by build"
    assert zip_file.stat().st_size > 1000, "ZIP file is too small"
