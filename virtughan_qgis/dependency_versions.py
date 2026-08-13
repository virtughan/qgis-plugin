VIRTUGHAN_VERSION = "1.1.1"
PIP_VERSION = "26.0.1"
RASTERIO_VERSION = "1.4.3"


def runtime_package_specs() -> list[str]:
    return [
        f"virtughan=={VIRTUGHAN_VERSION}",
        f"rasterio=={RASTERIO_VERSION}",
        "aiocache>=0.12.3",
        "fastapi>=0.115.6",
        "uvicorn>=0.34.0",
    ]


def pip_package_spec() -> str:
    return f"pip=={PIP_VERSION}"
