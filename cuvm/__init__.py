"""cuvm: run computer-use agent experiments in disposable Tart macOS VMs."""
import pathlib

__version__ = "0.1.0"

PKG = pathlib.Path(__file__).resolve().parent
DATA = PKG / "data"
SCRIPTS = PKG / "scripts"
