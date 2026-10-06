"""Sphinx configuration. https://www.sphinx-doc.org/en/master/usage/configuration.html."""

import sys
from pathlib import Path

# Import the package from this checkout's src/; autodoc still needs its runtime
# dependencies installed.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

project = "sonde"
author = "Jartan LLC"
project_copyright = "2026, Jartan LLC"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",  # parse google-style docstrings
    "sphinx.ext.viewcode",
    "myst_parser",  # author docs in Markdown
]

napoleon_google_docstring = True
napoleon_numpy_docstring = False
# Attributes sections as a field list: as attribute entries they'd duplicate the
# :undoc-members: fields and fail -W.
napoleon_use_ivar = True

myst_enable_extensions = ["colon_fence", "deflist", "tasklist"]
myst_heading_anchors = 3  # `#section` links, as GitHub resolves them

exclude_patterns = ["_build"]

html_theme = "furo"
