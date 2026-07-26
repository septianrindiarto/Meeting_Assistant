"""
Meeting Scribe — Local Meeting Assistant
Entry point for the application.
"""
import sys
import os

# Ensure src is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from src.app import run

if __name__ == "__main__":
    run()
