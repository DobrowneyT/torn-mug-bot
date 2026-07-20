import os
import sys

# Ensure `import config`, `from bot import ...`, `from server import ...` resolve when
# pytest is run from anywhere within the project.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
