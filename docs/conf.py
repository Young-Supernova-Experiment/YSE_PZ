# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
# import os
# import sys
# sys.path.insert(0, os.path.abspath('.'))
import os
import sys
import sphinx_rtd_theme
import sphinx_gallery

# -- Project information -----------------------------------------------------

project = 'YSE_PZ'
copyright = '2019, D. Coulter, D. Jones'
author = 'D. Coulter, D. Jones'


# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = ['sphinx.ext.autodoc',
              'sphinx.ext.autosummary',
              'sphinx.ext.intersphinx',
              'sphinx.ext.inheritance_diagram',
              'sphinx.ext.mathjax',
              'myst_parser'] #,
#             'sphinx.ext.linkcode',
#              'sphinx_gallery.gen_gallery']

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store', 'perf']

# This documentation is published publicly. Markdown pages are published only
# if they are listed here (an allowlist); every other docs/**/*.md file, at any
# depth, is excluded at build time, so new Markdown files default to
# unpublished. Before adding a page, check that it has no server paths, host
# names, ports, account names, admin or deploy commands, security details,
# personal names or internal planning notes.
# NOTE: docs/_static is copied verbatim into the published site; never place
# anything private there.
PUBLIC_MARKDOWN_PAGES = [
    'archive-tabs',
    'observability',
]

_docs_dir = os.path.dirname(os.path.abspath(__file__))
for _root, _dirs, _files in os.walk(_docs_dir):
    # Skip only the top-level build output directory (exact path), for speed.
    if _root == _docs_dir:
        _dirs[:] = [d for d in _dirs if d != '_build']
    for _name in _files:
        if not _name.endswith('.md'):
            continue
        _rel = os.path.relpath(os.path.join(_root, _name), _docs_dir).replace(os.sep, '/')
        if _rel[:-3] not in PUBLIC_MARKDOWN_PAGES:
            exclude_patterns.append(_rel)

pygments_style = 'sphinx'

# If true, `todo` and `todoList` produce output, else they produce nothing.
todo_include_todos = False

master_doc = 'index'

# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
#html_theme = 'alabaster'
html_theme = "sphinx_rtd_theme"
html_theme_path = [sphinx_rtd_theme.get_html_theme_path()]

html_sidebars = {
    '**': [
        'about.html',
        'navigation.html',
		'relations.html',  # needs 'show_related': True theme option to display
        'searchbox.html',
        'donate.html',
		]
	}

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ['_static']
