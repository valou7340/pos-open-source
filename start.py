#!/usr/bin/env python3
import subprocess, sys, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
subprocess.run([sys.executable, "-m", "uvicorn", "main:app",
                "--host", "0.0.0.0", "--port", "8000"], check=True)
