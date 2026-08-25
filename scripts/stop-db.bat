@echo off
rem ============================================================
rem  One-click stop: MySQL (local) + Redis (Docker)
rem  USAGE:
rem    1. Edit the paths below to match YOUR local machine
rem    2. Double-click this file, or run it from cmd/PowerShell
rem  Graceful shutdown ONLY. Never kill mysqld via Task Manager
rem  or "taskkill /F" - that leaves redo logs behind.
rem  NOTE: Redis runs as Docker container "medical-redis".
rem ============================================================

rem ================= EDIT THESE =================
set "MYSQL_BASEDIR=D:\Project_env\mysql-8.0.46-winx64"
set "REDIS_CONTAINER=medical-redis"
rem ==============================================

set "MYSQL_BIN=%MYSQL_BASEDIR%\bin"

rem ---- 1. Stop Redis container gracefully ----
docker ps --format "{{.Names}}" | findstr /x "%REDIS_CONTAINER%" >nul 2>&1
if %errorlevel%==0 (
  echo [STOP] Redis container "%REDIS_CONTAINER%" stopping...
  rem Cache data is disposable (TTL 300s) and auto-rebuilds on next use,
  rem so a plain "docker stop" (SIGTERM, 10s grace) is enough.
  docker stop %REDIS_CONTAINER%
) else (
  echo [OK]   Redis container not running ^(or Docker Desktop is down^)
)

rem ---- 2. Stop MySQL gracefully ----
netstat -ano | findstr ":3306" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo [STOP] MySQL shutting down gracefully...
  "%MYSQL_BIN%\mysqladmin.exe" -u root shutdown
) else (
  echo [OK]   MySQL not running
)

rem ---- 3. Wait and verify ports are free ----
timeout /t 5 >nul
echo.
echo ---- Port check (3306 / 6379) ----
netstat -ano | findstr ":3306 :6379" | findstr "LISTENING"
if %errorlevel%==0 (
  echo [WARN] Some services still listening, see output above.
) else (
  echo [OK]   Ports 3306 and 6379 are free. All services stopped.
)
echo.
pause
