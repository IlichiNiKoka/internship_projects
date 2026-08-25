@echo off
rem ============================================================
rem  One-click start: MySQL (local) + Redis (Docker)
rem  USAGE:
rem    1. Edit the MySQL paths below to match YOUR local machine
rem    2. Double-click this file, or run it from cmd/PowerShell
rem  Full instructions / FAQ: see scripts/README.md
rem  NOTE: ASCII-only comments (cmd/GBK safe).
rem        Redis now runs as a Docker container "medical-redis"
rem        (requires Docker Desktop to be running).
rem ============================================================

rem ================= EDIT THESE =================
set "MYSQL_BASEDIR=D:\Project_env\mysql-8.0.46-winx64"
set "MYSQL_DATADIR=D:\Project_env\mysql-8.0.46-winx64\data"
set "REDIS_CONTAINER=medical-redis"
set "REDIS_IMAGE=redis:7-alpine"
rem ==============================================

set "MYSQL_BIN=%MYSQL_BASEDIR%\bin"

rem ---- 0. Preflight: docker must be reachable ----
docker info >nul 2>&1
if not %errorlevel%==0 (
  echo [FAIL] Docker is not running. Start Docker Desktop first, then re-run.
  echo        Backend will fall back to in-memory cache until Redis is up.
  goto :mysql
)

rem ---- 1. Start Redis via Docker if not already running ----
docker ps --format "{{.Names}}" | findstr /x "%REDIS_CONTAINER%" >nul 2>&1
if %errorlevel%==0 (
  echo [OK]   Redis container "%REDIS_CONTAINER%" already running
) else (
  rem Image is pulled automatically on first run
  start "Redis" /min docker run --name %REDIS_CONTAINER% ^
    -p 127.0.0.1:6379:6379 ^
    --restart unless-stopped ^
    --health-cmd "redis-cli ping" --health-interval 10s --health-retries 5 ^
    %REDIS_IMAGE% redis-server --appendonly no
  echo [OK]   Redis container starting...
)

:mysql
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
docker ps --format "{{.Names}} {{.Status}}" | findstr /x "%REDIS_CONTAINER%.*healthy" >nul 2>&1
if %errorlevel%==0 (
  echo [OK]   %REDIS_CONTAINER% is healthy ^(port 6379^)
) else (
  docker exec %REDIS_CONTAINER% redis-cli ping 2>nul
  if not "%errorlevel%"=="0" echo [WARN] Redis not ready yet - it may still be booting, or Docker Desktop is down.
)
echo ---- MySQL check ----
"%MYSQL_BIN%\mysql.exe" -u root -e "SELECT VERSION();"
echo.
echo Done. If MySQL version printed above, services are ready.
pause
