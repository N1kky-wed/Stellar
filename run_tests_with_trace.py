import os
import sys
import time

# Bootstrap Angel tracing
import AngelTrace
project_root = os.path.dirname(os.path.abspath(__file__))
sys.meta_path.insert(0, AngelTrace.AngelFinder(project_root))

# Synchronously apply monkeypatches for Flask and requests
import flask
AngelTrace.patch_flask()
AngelTrace.patch_requests()

# Run pytest with stdout/stderr capture disabled (-s)
import pytest
sys.exit(pytest.main(["tests/test_streams.py", "-s"]))
