"""The view model: catalog items turned into tables of rows.

This package does not import Textual. A frontend requests a
:class:`~.frames.Plan` and renders the :class:`~..core.nav.Row` list it
produces, and a test can build the same frame without a terminal.
"""
