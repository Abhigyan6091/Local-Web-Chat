@echo off
title SSH Tunnel: localhost:8000 -> Sys1 Load Balancer
color 0E

echo ============================================================
echo   SSH Tunnel: localhost:8000 - Sys1 (172.17.0.38:8000)
echo   Keep this window OPEN while using the chat app.
echo ============================================================
echo.
echo Press Ctrl+C to close the tunnel.
echo.

ssh -p 2237 -L 8000:localhost:8000 -N -o StrictHostKeyChecking=no -o ServerAliveInterval=30 student@10.1.75.79
