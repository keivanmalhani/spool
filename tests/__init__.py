"""Test package marker.

This file exists so that ``tests`` is a real package. Without it, pytest puts
``tests/`` itself on ``sys.path`` rather than the repository root, and the
``from tests.conftest import ...`` used to share the synthetic gcode builders
fails under a bare ``pytest`` invocation while working under
``python -m pytest``. Having the documented command and the CI command behave
differently is exactly the sort of papercut that wastes a newcomer's first
half hour.
"""
