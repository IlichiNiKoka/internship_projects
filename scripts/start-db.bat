@echo off
rem ============================================================
rem  One-click start: MySQL + Redis
rem  USAGE:
rem    1. Edit the 3 paths below to match YOUR local machine
rem    2. Double-click this file, or run it from cmd/PowerShell
rem  NOTE: ASCII-only comments (cmd/GBK safe). If your paths are
rem  pure ASCII you do NOT need the old "subst Z:" trick anymore.
rem ============================================================

rem ================= EDIT THESE =================
set "MYSQL_BASEDIR=D:\Project_env\mysql-8.0.46-winx64"
set "MYSQL_DATADIR=D:\Project_env\mysql-8.0.46-winx64\data"
set "REDIS_HOME=D:\Project_env\Redis-8.6.3-Windows-x64-cygwin-with-Service"
rem ==============================================

set "MYSQL_BIN=%MYSQL_BASEDIR%\bin"

rem ---- 1. Start Redis if not running ----
tasklist | findstr /i "redis-server" >nul 2>&1
if %errorlevel%==0 (
  echo [OK]   Redis already running
) else (
  start "Redis" /min "%REDIS_HOME%\redis-server.exe"
  echo [OK]   Redis starting...
)

rem ---- 2. Start MySQL if port 3306 not listening ----
netstat -ano | findstr ":3306" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo [OK]   MySQL already running
) else (
  start "MySQL" /min "%MYSQL_BIN%\mysqld.exe" --basedir="%MYSQL_BASEDIR%" --datadir="%MYSQL_DATADIR%"
  echo [OK]   MySQL starting...
)

rem ---- 3. Wait and verify ----
timeout /t 10 >nul
echo.
echo ---- Redis check ----
"%REDIS_HOME%\redis-cli.exe" ping
echo ---- MySQL check ----
"%MYSQL_BIN%\mysql.exe" -u root -e "SELECT VERSION();"
echo.
echo Done. If MySQL version printed above, services are ready.
pause
