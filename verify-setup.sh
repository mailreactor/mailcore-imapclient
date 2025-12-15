#!/usr/bin/env python3
"""Development environment verification script - works on all platforms"""

import sys
import subprocess
import shutil
from pathlib import Path

def run_command(cmd):
    """Run command and return (success, output)"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return result.returncode == 0, result.stdout.strip()
    except:
        return False, ""

def main():
    print("🔍 Verifying mailcore-imapclient development environment...")
    print()
    
    # 1. Python version (should be 3.10+)
    major, minor = sys.version_info[:2]
    if major == 3 and minor >= 10:
        print(f"✓ Python: {sys.version.split()[0]}")
    else:
        print(f"❌ Python: {sys.version.split()[0]} (need 3.10+)")
        sys.exit(1)
    
    # 2. uv available
    success, output = run_command("uv --version")
    if success:
        print(f"✓ uv: {output}")
    else:
        print("❌ uv not available")
        sys.exit(1)
    
    # 3. Check venv
    venv_path = Path(".venv")
    if venv_path.exists():
        print(f"✓ Virtual environment: {venv_path.absolute()}")
        
        # 4. Check dev tools (if in venv)
        if sys.prefix != sys.base_prefix:  # In venv
            for tool in ["pytest", "ruff", "pre-commit"]:
                if shutil.which(tool):
                    print(f"✓ {tool} available")
                else:
                    print(f"❌ {tool} missing - run: uv pip install -e \".[dev]\"")
                    sys.exit(1)
        else:
            print("❌ Not in virtual environment - run: source .venv/bin/activate")
            sys.exit(1)
    else:
        print("❌ No .venv found - run quick start steps first")
        sys.exit(1)
    
    # 5. Check pre-commit hooks installed
    hooks_path = Path(".git/hooks/pre-commit")
    if hooks_path.exists():
        print(f"✓ Pre-commit hooks installed - try with: pre-commit run --all-files --show-diff-on-failure")
    else:
        print("❌ Pre-commit hooks NOT installed - run: pre-commit install")
        sys.exit(1)
    
    print()
    print("✅ Environment verification complete!")

if __name__ == "__main__":
    main()
