"""Transports: how a tick's output leaves the simulator.

All transports implement the small ``Transport`` protocol in ``base``;
the sim core never imports a transport, so the pure logic stays testable
with a stub.
"""
from .base import Transport, StdoutTransport
from .supabase import SupabaseTransport

__all__ = ["Transport", "StdoutTransport", "SupabaseTransport"]
