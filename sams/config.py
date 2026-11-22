"""Deployment switches.

DEMO_MODE (default on) is for the public demo: it seeds fictional data, rebuilds
it each day so the history stays recent, shows one-click demo sign-ins, and
protects the shared demo accounts from being locked out by a visitor. Set the
environment variable SAMS_DEMO=0 for a real deployment.
"""
import os

DEMO_MODE = os.environ.get("SAMS_DEMO", "1") != "0"
DEMO_PASSWORD = "Demo@1234"
DEMO_ACCOUNTS = {
    "admin": "admin",
    "teacher": "kavita.rao",
    "student": "aarav.sharma",
    "parent": "parent.aarav.sharma",
}
