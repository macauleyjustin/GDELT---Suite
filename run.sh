#!/bin/bash

echo "========================================="
echo "      Booting GDELT Analysis Suite       "
echo "========================================="

# Start backend in background
echo ">>> Starting Python FastAPI Backend on port 8000..."
cd backend
source venv/bin/activate
uvicorn main:app --reload --port 8000 &
BACKEND_PID=$!
cd ..

# Start frontend in background
echo ">>> Starting Next.js Frontend on port 3000..."
cd frontend
npm run dev &
FRONTEND_PID=$!
cd ..

echo ""
echo "🚀 Suite is running! Access the UI at: http://localhost:3000"
echo "Press Ctrl+C to shut down both servers."

# Trap Ctrl+C to kill both processes
trap "echo '\nShutting down...'; kill $BACKEND_PID; kill $FRONTEND_PID; exit" INT

wait $FRONTEND_PID
wait $BACKEND_PID
