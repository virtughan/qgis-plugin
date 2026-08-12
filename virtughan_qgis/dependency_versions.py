VIRTUGHAN_VERSION = "1.1.1"
PIP_VERSION = "26.0.1"
RASTERIO_VERSION = "1.4.3"


def runtime_package_specs() -> list[str]:
    return [
        f"virtughan[api]=={VIRTUGHAN_VERSION}",
        f"rasterio=={RASTERIO_VERSION}",
    ]


def pip_package_spec() -> str:
    return f"pip=={PIP_VERSION}"
