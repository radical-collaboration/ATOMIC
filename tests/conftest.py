
import os
import sys

# run the tests against the source tree, not against whatever version of
# atomic-wm happens to be installed in the venv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
