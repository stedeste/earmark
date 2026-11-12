#!/usr/bin/env python3
"""Earmark plugin contract v1 - live helper for CLAP (wraps clap_server.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clap_server

if __name__ == "__main__":
    clap_server.main()
