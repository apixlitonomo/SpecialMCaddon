"""Compatibility import surface for the safe MoLang runtime."""
try:
    from .molang_runtime import *
    from .molang_runtime import __all__
except ImportError:  # Plain-Python test and script imports.
    from molang_runtime import *
    from molang_runtime import __all__
