VIRTUGHAN_VERSION = "1.0.2"
PIP_VERSION = "26.0.1"
RASTERIO_VERSION = "1.4.3"
NUMPY_VERSION_PY_LT_311 = "2.2.6"
NUMPY_VERSION_PY_GTE_311 = "2.3.2"


def runtime_package_specs() -> list[str]:
    return [
        f"virtughan=={VIRTUGHAN_VERSION}",
        f"numpy=={NUMPY_VERSION_PY_LT_311}; python_version < '3.11'",
        f"numpy=={NUMPY_VERSION_PY_GTE_311}; python_version >= '3.11'",
        f"rasterio=={RASTERIO_VERSION}",
    ]


def pip_package_spec() -> str:
    return f"pip=={PIP_VERSION}"
