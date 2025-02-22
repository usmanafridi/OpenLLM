"""
This type stub file was generated by pyright.
"""

"""Code for converting notebooks to and from v3."""

def upgrade(nb, from_version=..., from_minor=...):
    """Convert a notebook to latest v4.

    Parameters
    ----------
    nb : NotebookNode
        The Python representation of the notebook to convert.
    from_version : int
        The original version of the notebook to convert.
    from_minor : int
        The original minor version of the notebook to convert (only relevant for v >= 3).
    """
    ...

def upgrade_cell(cell):
    """upgrade a cell from v3 to v4

    heading cell:
        - -> markdown heading
    code cell:
        - remove language metadata
        - cell.input -> cell.source
        - cell.prompt_number -> cell.execution_count
        - update outputs
    """
    ...

def downgrade_cell(cell):
    """downgrade a cell from v4 to v3

    code cell:
        - set cell.language
        - cell.input <- cell.source
        - cell.prompt_number <- cell.execution_count
        - update outputs
    markdown cell:
        - single-line heading -> heading cell
    """
    ...

_mime_map = ...

def to_mime_key(d):
    """convert dict with v3 aliases to plain mime-type keys"""
    ...

def from_mime_key(d):  # -> dict[Unknown, Unknown]:
    """convert dict with mime-type keys to v3 aliases"""
    ...

def upgrade_output(output):
    """upgrade a single code cell output from v3 to v4

    - pyout -> execute_result
    - pyerr -> error
    - output.type -> output.data.mime/type
    - mime-type keys
    - stream.stream -> stream.name
    """
    ...

def downgrade_output(output):
    """downgrade a single code cell output to v3 from v4

    - pyout <- execute_result
    - pyerr <- error
    - output.data.mime/type -> output.type
    - un-mime-type keys
    - stream.stream <- stream.name
    """
    ...

def upgrade_outputs(outputs):  # -> list[Unknown]:
    """upgrade outputs of a code cell from v3 to v4"""
    ...

def downgrade_outputs(outputs):  # -> list[Unknown]:
    """downgrade outputs of a code cell to v3 from v4"""
    ...

def downgrade(nb):
    """Convert a v4 notebook to v3.

    Parameters
    ----------
    nb : NotebookNode
        The Python representation of the notebook to convert.
    """
    ...
