"""KKFM project package.

This file intentionally exists to make `kkfm` a regular Python package (not a
namespace package), so systemd/cron executions reliably import this checkout's
modules even if another `kkfm` package is installed in site-packages.
"""
