"""Built-in strategies.

Empty by design. The 2026-08 engine cleanup deleted every DSL strategy that
shipped here — none of them had traded, and leaving the sources behind would
have let `pmstrat transpile --all` resurrect the Rust on the next run. New
strategies drop a module in this package and re-export its `on_tick` below.
"""

__all__: list[str] = []
