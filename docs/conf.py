# Copyright 2017 F5 Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# -*- coding: utf-8 -*-
#
# F5 Container Connector documentation build configuration file, created by
# sphinx-quickstart on Wed Aug 10 14:05:28 2016.
#
# This file is execfile()d with the current directory set to its
# containing dir.
#
# Note that not all possible configuration values are present in this
# autogenerated file.
#
# All configuration values have a default; values that are commented out
# serve to show the default.

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
import os
import sys

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('../'))

import f5_sphinx_theme
import recommonmark
import CommonMark
from recommonmark.parser import CommonMarkParser


# -- General configuration ------------------------------------------------

# If your documentation needs a minimal Sphinx version, state it here.
#
# needs_sphinx = '1.4.5'

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    'sphinx.ext.intersphinx',
    'sphinx.ext.todo',
    'sphinx.ext.coverage',
    'sphinx.ext.ifconfig',
    'sphinx.ext.doctest',
    'sphinxjp.themes.basicstrap',
    'sphinx.ext.extlinks',
    'cloud_sptheme.ext.table_styling'
]

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# The suffix(es) of source filenames.
# You can specify multiple suffix as a list of string:
#
# source_suffix = ['.rst', '.md']
source_suffix = ['.rst', '.md']

source_parsers = {
    '.md': CommonMarkParser,
}


# The encoding of source files.
#
# source_encoding = 'utf-8-sig'

# The master toctree document.
master_doc = 'index'

# General information about the project.
project = u'F5 BIG-IP Controller for Marathon'
copyright = u'2016,2017 F5 Networks Inc'
author = u'F5 Networks'

# The version info for the project you're documenting, acts as replacement for
# |version| and |release|, also used in various other places throughout the
# built documents.
with open('../next-version.txt') as verfile:
    v = verfile.readline().strip().split('.')
    # The short X.Y version.
    version = u'v{}.{}'.format(v[0], v[1])
    # The full version, including alpha/beta/rc tags.
    release = u'v{}.{}.{}-dev'.format(v[0], v[1], v[2])

# External links
# Lets you reference GitHub issues in release notes, e.g. :issues:`214`
extlinks = {'issues': ('https://github.com/F5Networks/marathon-bigip-ctlr/issues/%s',
                      'issue ')}

# Substitutions
rst_epilog = '''
.. |url-version| replace:: %(url_version)s
.. |release-notes| raw:: html

    <a href="http://clouddocs.f5.com/products/connectors/marathon-bigip-ctlr/%(url_version)s/RELEASE-NOTES.html">Release Notes</a>
.. |attributions| raw:: html

    <a href="http://clouddocs.f5.com/products/connectors/marathon-bigip-ctlr/%(url_version)s/_static/ATTRIBUTIONS.html">Attributions</a>
.. |mctlr| replace:: :code:`marathon-bigip-ctlr`
.. |mctlr-long| replace:: F5 BIG-IP Controller for Marathon
.. _user documentation: %(base_url)s/containers/latest/marathon/
.. _pools without virtual servers: %(base_url)s/containers/latest/marathon/mctlr-manage-bigip-objects.html#manage-pools-without-virtual-servers
.. _use an IPAM system: %(base_url)s/containers/latest/marathon/mctlr-manage-bigip-objects.html#use-ipam-to-assign-ip-addresses-to-big-ip-virtual-servers
.. _Enterprise DC/OS: https://mesosphere.com/product/
.. _Identity and Access Management API: https://docs.mesosphere.com/1.8/administration/id-and-access-mgt/oss/iam-api/
.. _Marathon: https://mesosphere.github.io/marathon/
.. _Marathon Application: https://mesosphere.github.io/marathon/docs/application-basics.html
.. _port-mapping: https://mesosphere.github.io/marathon/docs/ports.html
.. _iApps: https://devcentral.f5.com/iapps
.. _route domain: https://support.f5.com/kb/en-us/products/big-ip_ltm/manuals/product/tmos-routing-administration-12-0-0/9.html
''' % {
    'url_version': version,
    'base_url': 'http://clouddocs.f5.com'
}
# The language for content autogenerated by Sphinx. Refer to documentation
# for a list of supported languages.
#
# This is also used if you do content translation via gettext catalogs.
# Usually you set "language" from the command line for these cases.
language = None

# There are two options for replacing |today|: either, you set today to some
# non-false value, then it is used:
#
# today = ''
#
# Else, today_fmt is used as the format for a strftime call.
#
# today_fmt = '%B %d, %Y'

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This patterns also effect to html_static_path and html_extra_path
exclude_patterns = [
    '_build',
    'Thumbs.db',
    '.DS_Store',
    'venv',
    '.github',
    'Dockerfile',
    'requirements.txt',
    '*.swp',
    '*.swx',
    '*~',
    'README.rst'
    ]

# The reST default role (used for this markup: `text`) to use for all
# documents.
#
# default_role = None

# If true, '()' will be appended to :func: etc. cross-reference text.
#
# add_function_parentheses = True

# If true, the current module name will be prepended to all description
# unit titles (such as .. function::).
#
# add_module_names = True

# If true, sectionauthor and moduleauthor directives will be shown in the
# output. They are ignored by default.
#
# show_authors = False

# The name of the Pygments (syntax highlighting) style to use.
pygments_style = 'sphinx'

# A list of ignored prefixes for module index sorting.
# modindex_common_prefix = []

# If true, keep warnings as "system message" paragraphs in the built documents.
# keep_warnings = False

# If true, `todo` and `todoList` produce output, else they produce nothing.
todo_include_todos = True


# -- Options for HTML output ----------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = 'f5_sphinx_theme'
html_theme_path = f5_sphinx_theme.get_html_theme_path()
html_theme_options = {
    'next_prev_link': False
}

# Theme options are theme-specific and customize the look and feel of a theme
# further.  For a list of options available for each theme, see the
# documentation.
#
# html_theme_options = {}

# Add any paths that contain custom themes here, relative to this directory.
# html_theme_path = []

# The name for this set of Sphinx documents.
# "<project> v<release> documentation" by default.
#
html_title = "{} {}".format(project, version)

# A shorter title for the navigation bar.  Default is the same as html_title.
#
#html_short_title = 'marathon-bigip-ctlr'

# The name of an image file (relative to this directory) to place at the top
# of the sidebar.
#
html_logo = '_static/f5-logo-solid-rgb_small.png'

# The name of an image file (relative to this directory) to use as a favicon of
# the docs.  This file should be a Windows icon file (.ico) being 16x16 or 32x32
# pixels large.
#
# html_favicon = None

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ['_static/']

# Add any extra paths that contain custom files (such as robots.txt or
# .htaccess) here, relative to this directory. These files are copied
# directly to the root of the documentation.
#
# html_extra_path = []

# If not None, a 'Last updated on:' timestamp is inserted at every page
# bottom, using the given strftime format.
# The empty string is equivalent to '%b %d, %Y'.
#
html_last_updated_fmt = ''

# If true, SmartyPants will be used to convert quotes and dashes to
# typographically correct entities.
#
# html_use_smartypants = True

# Custom sidebar templates, maps document names to template names.
#
html_sidebars = {
    '**': ['searchbox.html', 'localtoc.html' ]
}

# Additional templates that should be rendered to pages, maps page names to
# template names.
#
# html_additional_pages = {}

# If false, no module index is generated.
#
html_domain_indices = True

# If false, no index is generated.
#
html_use_index = True

# If true, the index is split into individual pages for each letter.
#
# html_split_index = False

# If true, links to the reST sources are added to the pages.
#
html_show_sourcelink = True

# If true, "Created using Sphinx" is shown in the HTML footer. Default is True.
#
html_show_sphinx = False

# If true, "(C) Copyright ..." is shown in the HTML footer. Default is True.
#
html_show_copyright = True

# If true, an OpenSearch description file will be output, and all pages will
# contain a <link> tag referring to it.  The value of this option must be the
# base URL from which the finished HTML is served.
#
# html_use_opensearch = ''

# This is the file name suffix for HTML files (e.g. ".xhtml").
# html_file_suffix = None

# Language to be used for generating the HTML full-text search index.
# Sphinx supports the following languages:
#   'da', 'de', 'en', 'es', 'fi', 'fr', 'hu', 'it', 'ja'
#   'nl', 'no', 'pt', 'ro', 'ru', 'sv', 'tr', 'zh'
#
# html_search_language = 'en'

# A dictionary with options for the search language support, empty by default.
# 'ja' uses this config value.
# 'zh' user can custom change `jieba` dictionary path.
#
# html_search_options = {'type': 'default'}

# The name of a javascript file (relative to the configuration directory) that
# implements a search results scorer. If empty, the default will be used.
#
# html_search_scorer = 'scorer.js'

# Output file base name for HTML help builder.
htmlhelp_basename = 'f5-marathon-bigip-controller_doc'

# -- Options for LaTeX output ---------------------------------------------

latex_elements = {
     # The paper size ('letterpaper' or 'a4paper').
     #
     'papersize': 'letterpaper',

     # The font size ('10pt', '11pt' or '12pt').
     #
     'pointsize': '10pt',

     # Additional stuff for the LaTeX preamble.
     #
     # 'preamble': '',

     # Latex figure (float) alignment
     #
     # 'figure_align': 'htbp',
}

# Grouping the document tree into LaTeX files. List of tuples
# (source start file, target name, title,
#  author, documentclass [howto, manual, or own class]).
latex_documents = [
    (master_doc, 'f5-marathon-bigip-controller.tex',
     u'f5-marathon-bigip-controller - Documentation',
     'F5 Networks', 'manual'),
]

# The name of an image file (relative to this directory) to place at the top of
# the title page.
#
latex_logo = '_static/f5_logo.jpg'

# For "manual" documents, if this is true, then toplevel headings are parts,
# not chapters.
#
# latex_use_parts = False

# replaces latex_use_parts
# determines the topmost sectioning unit. It should be chosen from part,
#  chapter or section. The default is None; the topmost sectioning unit is
#  switched by documentclass. section is used if documentclass will be howto,
#  otherwise chapter will be used.
#
latex_toplevel_sectioning = 'section'

# If true, show page references after internal links.
#
# latex_show_pagerefs = False

# If true, show URL addresses after external links.
#
# latex_show_urls = False

# Documents to append as an appendix to all manuals.
#
# latex_appendices = []

# It false, will not define \strong, \code, 	itleref, \crossref ... but only
# \sphinxstrong, ..., \sphinxtitleref, ... To help avoid clash with user added
# packages.
#
# latex_keep_old_macro_names = True

# If false, no module index is generated.
#
# latex_domain_indices = True


# -- Options for manual page output ---------------------------------------

# One entry per manual page. List of tuples
# (source start file, name, description, authors, manual section).
man_pages = [
    (master_doc, 'f5-marathon-bigip-controller',
     u'f5-marathon-bigip-controller - Documentation',
     [author], 1)
]

# If true, show URL addresses after external links.
#
# man_show_urls = False


# -- Options for Texinfo output -------------------------------------------

# Grouping the document tree into Texinfo files. List of tuples
# (source start file, target name, title, author,
#  dir menu entry, description, category)
texinfo_documents = [
    (master_doc, 'f5-marathon-bigip-controller',
     u'f5-marathon-bigip-controller - Documentation',
     author, 'f5-marathon-bigip-controller',
     'f5-marathon-bigip-controller Marathon'),
]

# Documents to append as an appendix to all manuals.
#
# texinfo_appendices = []

# If false, no module index is generated.
#
# texinfo_domain_indices = True

# How to display URL addresses: 'footnote', 'no', or 'inline'.
#
texinfo_show_urls = 'footnote'

# If true, do not generate a @detailmenu in the "Top" node's menu.
#
# texinfo_no_detailmenu = False


# Example configuration for intersphinx: refer to the Python standard library.
#intersphinx_mapping = {'https://docs.python.org/': None}
