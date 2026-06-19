import os
import sys
import time

# Bootstrap Angel tracing
import angel_trace
project_root = os.path.dirname(os.path.abspath(__file__))
sys.meta_path.insert(0, angel_trace.AngelFinder(project_root))

# Synchronously apply monkeypatches for Flask and requests
import flask
angel_trace.patch_flask()
angel_trace.patch_requests()

# Run pytest with stdout/stderr capture disabled (-s)
import pytest
sys.exit(pytest.main(["tests/test_streams.py", "-s"]))
