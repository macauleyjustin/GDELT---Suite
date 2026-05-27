#!/bin/bash
set -e

echo "========================================="
echo "   GDELT Analysis Suite Installer        "
echo "========================================="
echo "Installing dependencies for Mac / Linux..."
echo ""

# Check for Node.js
if ! command -v node &> /dev/null; then
    echo "❌ Node.js is not installed."
    echo "Please install Node.js (https://nodejs.org) and re-run this script."
    exit 1
fi
echo "✅ Node.js detected."

# Check for Python 3
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed."
    echo "Please install Python 3 and re-run this script."
    exit 1
fi
echo "✅ Python 3 detected."

echo ""
echo ">>> Setting up Python Backend..."
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cd ..

echo ""
echo ">>> Setting up Next.js Frontend..."
cd frontend
npm install
cd ..

echo ""
echo "========================================="
echo "           INSTALLATION COMPLETE         "
echo "========================================="
echo ""
echo "IMPORTANT: You MUST authenticate with Google Cloud to query BigQuery."
echo "If you haven't already, install the gcloud CLI and run:"
echo "    gcloud auth application-default login"
echo ""
echo "Once authenticated, start the suite by running:"
echo "    ./run.sh"
echo ""
