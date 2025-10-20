#!/bin/bash
set -e  

echo "Setting up Python environment..."
python3 -m pip install --upgrade pip setuptools wheel

echo "Installing dependencies..."
pip install -r requirements.txt

echo "Build environment ready"
