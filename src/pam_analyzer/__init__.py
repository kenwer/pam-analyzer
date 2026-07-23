from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pam-analyzer")
except PackageNotFoundError:
    __version__ = "unknown"
