"""NFL DFS pipeline: slate capture, identity resolution, and lineup optimization.

The package is organised so that each stage of the weekly pipeline is
independently testable:

    capture  ->  normalize  ->  resolve identity  ->  adjust  ->  optimize

Only the domain-rules layer (:mod:`dfs_pipeline.contest`) exists so far.
"""

__version__ = "0.1.0"
