VIRTUGHAN_VERSION = "1.0.2"
PIP_VERSION = "26.0.1"
RASTERIO_VERSION = "1.4.3"
NUMPY_VERSION = "1.26.4"


def runtime_package_specs() -> list[str]:
    return [
        f"virtughan=={VIRTUGHAN_VERSION}",
        f"numpy=={NUMPY_VERSION}",
        f"rasterio=={RASTERIO_VERSION}",
    ]


def pip_package_spec() -> str:
    return f"pip=={PIP_VERSION}"
