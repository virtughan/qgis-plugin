import configparser
import zipfile
from pathlib import Path


def test_metadata_content():
    project_root = Path(__file__).parent.parent
    metadata_file = project_root / "virtughan_qgis" / "metadata.txt"
    
    if metadata_file.exists():
        config = configparser.ConfigParser()
        config.read(metadata_file)
        
        assert config.has_section('general')
        general = config['general']
        
        assert 'name' in general
        assert 'version' in general
        assert 'description' in general
        assert 'qgisMinimumVersion' in general
        assert 'author' in general
        assert 'email' in general
        
        assert general['name'] == 'VirtuGhan - Satellite Data Tools'
        assert general['qgisMinimumVersion'] == '3.22'
        
        version = general['version']
        assert len(version.split('.')) >= 2


def test_plugin_package_structure():
    project_root = Path(__file__).parent.parent
    zip_file = project_root / "dist" / "virtughan-qgis-plugin.zip"
    
    if zip_file.exists():
        with zipfile.ZipFile(zip_file, 'r') as zf:
            files = zf.namelist()
            
            required_files = [
                "virtughan_qgis/__init__.py",
                "virtughan_qgis/main_plugin.py",
                "virtughan_qgis/bootstrap.py",
                "virtughan_qgis/processing_provider.py",
            ]
            
            for required_file in required_files:
                assert required_file in files, f"Required file {required_file} missing from package"
            
            required_dirs = [
                "virtughan_qgis/common/",
                "virtughan_qgis/engine/",
                "virtughan_qgis/extractor/",
                "virtughan_qgis/tiler/",
                # "virtughan_qgis/utils/"
            ]
            
            for required_dir in required_dirs:
                dir_files = [f for f in files if f.startswith(required_dir)]
                assert len(dir_files) > 0, f"Required directory {required_dir} missing or empty"


def test_no_pycache_in_package():
    project_root = Path(__file__).parent.parent
    zip_file = project_root / "dist" / "virtughan-qgis-plugin.zip"
    
    if zip_file.exists():
        with zipfile.ZipFile(zip_file, 'r') as zf:
            files = zf.namelist()
            
            pycache_files = [f for f in files if '__pycache__' in f]
            assert len(pycache_files) == 0, f"Found __pycache__ files in package: {pycache_files}"
            
            pyc_files = [f for f in files if f.endswith('.pyc')]
            assert len(pyc_files) == 0, f"Found .pyc files in package: {pyc_files}"


def test_ui_files_in_package():
    project_root = Path(__file__).parent.parent
    zip_file = project_root / "dist" / "virtughan-qgis-plugin.zip"
    
    if zip_file.exists():
        with zipfile.ZipFile(zip_file, 'r') as zf:
            files = zf.namelist()
            
            ui_files = [f for f in files if f.endswith('.ui')]
            expected_ui_files = [
                "virtughan_qgis/common/common_form.ui",
                "virtughan_qgis/engine/engine_form.ui",
                "virtughan_qgis/extractor/extractor_form.ui",
                "virtughan_qgis/tiler/tiler_form.ui"
            ]
            
            for expected_ui in expected_ui_files:
                assert expected_ui in files, f"UI file {expected_ui} missing from package"


def test_license_in_package():
    project_root = Path(__file__).parent.parent
    zip_file = project_root / "dist" / "virtughan-qgis-plugin.zip"
    
    if zip_file.exists():
        with zipfile.ZipFile(zip_file, 'r') as zf:
            files = zf.namelist()
            
            license_files = [f for f in files if 'LICENSE' in f.upper()]
            assert len(license_files) > 0, "No LICENSE file found in package"
