@echo off
title Local-Web-Chat with Load Balancer
color 0A

echo ============================================================
echo   Local-Web-Chat + Distributed Load Balancer Launcher
echo ============================================================
echo.
echo Starting 3 FastAPI backends + Load Balancer + Frontend...
echo.

:: Change to the chat app directory
cd /d "%~dp0"

:: Start Backend 1 (port 8001) — BACKEND_PORT tells database.py which chat_*.db to use
start "BACKEND-1 :8001" cmd /k "title BACKEND-1 :8001 && color 09 && set BACKEND_PORT=8001 && python -m uvicorn server.main:app --host 0.0.0.0 --port 8001"

:: Start Backend 2 (port 8002)
start "BACKEND-2 :8002" cmd /k "title BACKEND-2 :8002 && color 0B && set BACKEND_PORT=8002 && python -m uvicorn server.main:app --host 0.0.0.0 --port 8002"

:: Start Backend 3 (port 8003)
start "BACKEND-3 :8003" cmd /k "title BACKEND-3 :8003 && color 0A && set BACKEND_PORT=8003 && python -m uvicorn server.main:app --host 0.0.0.0 --port 8003"

:: Wait for backends to initialize
echo Waiting for backends to start...
timeout /t 4 /nobreak >nul

:: Start Load Balancer (port 8000)
:: Using ip_hash algorithm so each browser client is ALWAYS routed to the same backend.
:: This is critical for WebSocket chat — each backend has its own in-memory connection
:: manager, so a user must always land on the same backend instance.
start "LOAD-BALANCER :8000" cmd /k "title LOAD-BALANCER :8000 && color 0E && python load_balancer\balancer.py --host 0.0.0.0 --port 8000 --backends http://127.0.0.1:8001,http://127.0.0.1:8002,http://127.0.0.1:8003 --algorithm ip_hash"

:: Wait for LB to initialize
timeout /t 2 /nobreak >nul

:: Start Frontend (Vite :5000)
start "FRONTEND :5000" cmd /k "title FRONTEND :5000 && color 0D && npx vite --host 0.0.0.0 --port 5000"

echo.
echo ============================================================
echo   All services started!
echo.
echo   Chat App     : http://localhost:5000
echo   Load Balancer: http://localhost:8000/lb/status
echo   Backend 1    : http://localhost:8001/health
echo   Backend 2    : http://localhost:8002/health
echo   Backend 3    : http://localhost:8003/health
echo.
echo   ALGORITHM    : ip_hash (sticky sessions per client IP)
echo   DB FILES     : server/chat_8001.db, chat_8002.db, chat_8003.db
echo ============================================================
echo.
echo Close this window to keep services running,
echo or close each individual window to stop that service.
pause
