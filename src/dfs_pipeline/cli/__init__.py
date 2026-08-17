"""Command-line entry points.

Each module here exposes a ``main()`` that :file:`pyproject.toml` wires to a
console script. Keeping the CLI layer thin and separate from the library
means the pipeline stays importable and testable without going through
argument parsing.
"""
