@echo off
rem ============================================================
rem  One-click stop: MySQL + Redis (run this as normal user)
rem  USAGE:
rem    1. Edit the paths below to match YOUR local machine
rem    2. Double-click this file, or run it from cmd/PowerShell
rem  Full instructions / FAQ: see scripts/README.md
rem  Graceful shutdown ONLY. Never kill mysqld / redis-server
rem  via Task Manager or "taskkill /F" - that leaves redo logs
rem  and pid files behind and slows down the next startup.
rem ============================================================

rem ================= EDIT THESE =================
set "MYSQL_BASEDIR=D:\Project_env\mysql-8.0.46-winx64"
set "REDIS_HOME=D:\Project_env\Redis-8.6.3-Windows-x64-cygwin-with-Service"
rem ==============================================

set "MYSQL_BIN=%MYSQL_BASEDIR%\bin"

rem ---- 1. Stop Redis gracefully ----
tasklist | findstr /i "redis-server" >nul 2>&1
if %errorlevel%==0 (
  echo [STOP] Redis shutting down...
  rem NOTE: plain "shutdown" (with RDB save) fails on this Redis
  rem 8.6.3 cygwin build with "ERR Errors trying to SHUTDOWN",
  rem so we use "shutdown nosave" instead. Cache data is
  rem disposable (TTL 300s) and auto-rebuilds on next use.
  "%REDIS_HOME%\redis-cli.exe" shutdown nosave
) else (
  echo [OK]   Redis not running
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
