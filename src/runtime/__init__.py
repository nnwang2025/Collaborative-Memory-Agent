"""Persistent, cancellable run/session runtime for the V2 investigation API.

Import concrete services from ``src.runtime.service``.  Keeping this package
initializer free of eager imports avoids a runtime <-> agent import cycle.
"""
