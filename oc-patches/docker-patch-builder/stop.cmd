@echo off
docker stop patch-builder >nul 2>nul && docker rm patch-builder >nul 2>nul
echo Stopped.

