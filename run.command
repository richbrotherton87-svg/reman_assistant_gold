#!/bin/bash

cd "$(dirname "$0")"

# Activate venv
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
else
    echo "❌ No venv found. Create it with: python3 -m venv venv"
    exit 1
fi

# Kill anything on 5000 or 5001
kill -9 $(lsof -ti :5000) 2>/dev/null
kill -9 $(lsof -ti :5001) 2>/dev/null

# Set Flask environment
export FLASK_APP=app.py
export FLASK_ENV=development

# Start Flask
python3 -m flask run --port=5001