"""The view model: catalog items turned into tables of rows.

Knows nothing about Textual. A frontend asks for a :class:`~.frames.Plan` and
renders the :class:`~..core.nav.Row` list it produces; a test can build the
same frame with no terminal at all.
"""
