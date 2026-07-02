"""Legacy compatibility wrapper.

The productive FlowShift runtime is `tray.py`.
This module exists so old scripts and tests can still import the shared helpers.
"""
from tray import *  # noqa: F401,F403


if __name__ == "__main__":
    run()
