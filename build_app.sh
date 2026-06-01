#!/usr/bin/env bash
set -euo pipefail
source .venv/bin/activate
python setup.py py2app
echo "Built: dist/ID5 Batch Processor.app"
