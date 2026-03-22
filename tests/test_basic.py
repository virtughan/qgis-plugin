import os
import sys
import zipfile
from pathlib import Path
import pytest


def test_project_structure():
    project_root = Path(__file__).parent.parent
    
    assert (project_root / "pyproject.toml").exists()
    assert (project_root / "README.md").exists()
    assert (project_root / "build.sh").exists()
    assert (project_root / "build.ps1").exists()
    assert (project_root / "generate_metadata.py").exists()
    assert (project_root / "virtughan_qgis").exists()


def test_plugin_directory_structure():
    project_root = Path(__file__).parent.parent
    plugin_dir = project_root / "virtughan_qgis"
    
    assert (plugin_dir / "__init__.py").exists()
    assert (plugin_dir / "main_plugin.py").exists()
    assert (plugin_dir / "bootstrap.py").exists()
    assert (plugin_dir / "processing_provider.py").exists()
    
    assert (plugin_dir / "common").exists()
    assert (plugin_dir / "engine").exists()
    assert (plugin_dir / "extractor").exists()
    assert (plugin_dir / "tiler").exists()
    # assert (plugin_dir / "utils").exists()


def test_module_imports():
    project_root = Path(__file__).parent.parent
    sys.path.insert(0, str(project_root))
    
    try:
        import virtughan_qgis
        assert hasattr(virtughan_qgis, 'classFactory')
    except ImportError as e:
        if 'qgis' in str(e).lower():
            pytest.skip("QGIS not available in test environment")
        else:
            assert False, f"Module import failed: {e}"


def test_plugin_widgets_importable():
    project_root = Path(__file__).parent.parent
    sys.path.insert(0, str(project_root))
    
    try:
        from virtughan_qgis.common.common_widget import CommonParamsWidget
        from virtughan_qgis.engine.engine_widget import EngineDockWidget
        from virtughan_qgis.extractor.extractor_widget import ExtractorDockWidget
        from virtughan_qgis.tiler.tiler_widget import TilerDockWidget
    except ImportError as e:
        if 'qgis' in str(e).lower():
            pytest.skip("QGIS not available in test environment")
        else:
            assert False, f"Widget import failed: {e}"


def test_metadata_generation():
    project_root = Path(__file__).parent.parent
    metadata_script = project_root / "generate_metadata.py"
    
    assert metadata_script.exists()
    
    import subprocess
    result = subprocess.run([
        sys.executable, str(metadata_script)
    ], cwd=str(project_root), capture_output=True, text=True)

    assert result.returncode == 0, f"Metadata generation failed: {result.stderr}"

    metadata_file = project_root / "virtughan_qgis" / "metadata.txt"
    assert metadata_file.exists()

    content = metadata_file.read_text()
    assert "name=VirtuGhan QGIS Plugin" in content
    assert "version=" in content
    assert "qgisMinimumVersion=" in content


def test_build_script():
    project_root = Path(__file__).parent.parent
    build_script = project_root / "build.sh"
    build_script_py = project_root / "build.py"
    
    assert build_script.exists()
    assert build_script_py.exists()
    
    import subprocess
    result = subprocess.run([
        sys.executable, str(build_script_py)
    ], cwd=str(project_root), capture_output=True, text=True)
    
    assert result.returncode == 0, f"Build script failed: {result.stderr}"
    
    zip_file = project_root / "dist" / "virtughan-qgis-plugin.zip"
    assert zip_file.exists(), "Plugin ZIP file was not created"
    
    with zipfile.ZipFile(zip_file, 'r') as zf:
        files = zf.namelist()
        
        assert "virtughan_qgis/__init__.py" in files
        assert "virtughan_qgis/main_plugin.py" in files
        assert "virtughan_qgis/metadata.txt" in files
        assert any(f.startswith("virtughan_qgis/common/") for f in files)
        assert any(f.startswith("virtughan_qgis/engine/") for f in files)
        assert any(f.startswith("virtughan_qgis/extractor/") for f in files)
        assert any(f.startswith("virtughan_qgis/tiler/") for f in files)


def test_plugin_widgets_importable():
    project_root = Path(__file__).parent.parent
    sys.path.insert(0, str(project_root))
    
    try:
        from virtughan_qgis.common.common_widget import CommonParamsWidget
        from virtughan_qgis.engine.engine_widget import EngineDockWidget
        from virtughan_qgis.extractor.extractor_widget import ExtractorDockWidget
        from virtughan_qgis.tiler.tiler_widget import TilerDockWidget
        
    except ImportError as e:
        if 'qgis' in str(e).lower():
            pytest.skip("QGIS not available in test environment")
        else:
            assert False, f"Widget import failed: {e}"


def test_ui_files_exist():
    project_root = Path(__file__).parent.parent
    plugin_dir = project_root / "virtughan_qgis"
    
    assert (plugin_dir / "common" / "common_form.ui").exists()
    assert (plugin_dir / "engine" / "engine_form.ui").exists()
    assert (plugin_dir / "extractor" / "extractor_form.ui").exists()
    assert (plugin_dir / "tiler" / "tiler_form.ui").exists()


def test_plugin_resources():
    project_root = Path(__file__).parent.parent
    plugin_dir = project_root / "virtughan_qgis"
    
    assert (plugin_dir).exists()
    static_dir = project_root / "static"
    assert static_dir.exists()
    assert (static_dir / "images").exists()
