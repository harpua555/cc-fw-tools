@echo off
setlocal

where docker >nul 2>nul || (
  echo Please install Docker Desktop first: https://www.docker.com/products/docker-desktop
  pause
  exit /b 1
)

set IMAGE=%IMAGE%
if "%IMAGE%"=="" set IMAGE=patch-builder:latest

set "OUTDIR=%USERPROFILE%\Downloads\OpenCentauri\outputs"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

echo Pulling %IMAGE% (this may take a moment)...
docker pull %IMAGE% >nul 2>nul

echo Stopping any previous container...
docker rm -f patch-builder >nul 2>nul

echo Starting container on http://localhost:8080 (with privileges for bootlogo patch)
docker run -d --name patch-builder --privileged -p 8080:8080 -v "%OUTDIR%:/app/artifacts" %IMAGE%

start "" http://localhost:8080
echo Running. Use stop.cmd to stop the container.
endlocal
