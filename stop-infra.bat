@echo off
rem ============================================================
rem  One-click stop for all infrastructure containers.
rem  Graceful: containers get SIGTERM, data volumes are KEPT.
rem
rem  To wipe data completely:
rem    cd deploy && docker compose down -v
rem ============================================================
set "COMPOSE_DIR=%~dp0deploy"

docker info >nul 2>&1
if not %errorlevel%==0 (
  echo [FAIL] Docker is not running.
  goto :end
)

pushd "%COMPOSE_DIR%"
echo Stopping medical-mysql / medical-hdfs / medical-redis ...
docker compose down
echo.
echo All containers stopped. Data volumes are preserved.
popd
:end
pause
exit /b 0
