"""Persistence layer.

Exposes narrow repositories. D1 SQL, JsProxy conversion and batch details stay
behind this package — no caller outside it may see a `JsProxy` or write SQL.
"""
