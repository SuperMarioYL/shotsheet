"""ShotSheet: turn a reference short video into a shootable shot list."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("shotsheet")
except PackageNotFoundError:  # source tree without an installed package
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
