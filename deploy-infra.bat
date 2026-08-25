@echo off
rem ============================================================
rem  One-click infrastructure deployment (Docker)
rem  Deploys: MySQL 8.0 + HDFS (pseudo-distributed) + Redis
rem
rem  USAGE:
rem    1. Install & start Docker Desktop
rem    2. Put your MySQL data file at PROJECT ROOT:
rem         sparcs_discharge_2021.ibd      (InnoDB tablespace)
rem    3. Double-click this file (or run from cmd)
rem
rem  The script is idempotent:
rem    - starts containers defined in deploy/docker-compose.yml
rem    - imports sparcs_discharge_2021.ibd into MySQL on FIRST run
rem    - uploads local Parquet snapshot to HDFS if present
rem    - skips every step that was already done before
rem ============================================================

set "PROJECT_ROOT=%~dp0"
set "COMPOSE_DIR=%PROJECT_ROOT%deploy"
set "SQL_DIR=%COMPOSE_DIR%\sql"
set "IBD_FILE=%PROJECT_ROOT%sparcs_discharge_2021.ibd"
set "PARQUET_DIR=%PROJECT_ROOT%data_process\processed\sparcs_snapshot.parquet"

echo ============================================================
echo  Medical Platform - Docker Infrastructure Deployment
echo ============================================================

rem ---- 0. Preflight ----
docker info >nul 2>&1
if not %errorlevel%==0 (
  echo [FAIL] Docker is not running. Install/start Docker Desktop first.
  echo        Download: https://www.docker.com/products/docker-desktop/
  goto :end
)

if not exist "%COMPOSE_DIR%\docker-compose.yml" (
  echo [FAIL] deploy\docker-compose.yml not found next to this script.
  goto :end
)

pushd "%COMPOSE_DIR%"

rem ---- 1. Start MySQL (must be healthy before data import) ----
echo [1/5] Starting MySQL container...
docker compose up -d medical-mysql
if not %errorlevel%==0 (
  echo [FAIL] Failed to start medical-mysql. See output above.
  goto :pop
)
call :wait_healthy medical-mysql 600
if not "%HEALTHY%"=="1" (
  echo [FAIL] medical-mysql did not become healthy in time.
  echo        Check logs: docker logs medical-mysql
  goto :pop
)

rem ---- 2. Import .ibd data file (first run only) ----
echo [2/5] Checking MySQL data...
set "ROWCNT="
docker exec -i medical-mysql sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" --skip-column-names 2>/dev/null" < "%SQL_DIR%\check_tables.sql" > "%TEMP%\medcnt.txt"
set /p ROWCNT=<"%TEMP%\medcnt.txt"
set "ROWCNT=%ROWCNT: =%"
if "%ROWCNT%"=="" set "ROWCNT=0"

if not exist "%IBD_FILE%" (
  echo [WARN] Data file not found: %IBD_FILE%
  echo        Place "sparcs_discharge_2021.ibd" at project root and re-run,
  echo        or load data yourself via: python load_to_db.py
  goto :hdfs
)

if not "%ROWCNT%"=="0" (
  echo [OK]   Database already initialized - skipping .ibd import.
  goto :hdfs
)

echo        Creating schema from deploy\schema_sparcs.sql ...
docker exec -i medical-mysql sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" 2>/dev/null" < "%COMPOSE_DIR%\schema_sparcs.sql"
if errorlevel 1 (
  echo [FAIL] Schema creation failed.
  goto :pop
)

echo        Detaching empty tablespace...
docker exec -i medical-mysql sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" 2>/dev/null" < "%SQL_DIR%\discard_tablespace.sql"
if errorlevel 1 (
  echo [FAIL] DISCARD TABLESPACE failed.
  goto :pop
)

echo        Copying sparcs_discharge_2021.ibd into container ^(~1.5 GB, be patient^) ...
docker cp "%IBD_FILE%" medical-mysql:/var/lib/mysql/sparcs_discharge_2021/sparcs_discharge_2021.ibd
if errorlevel 1 (
  echo [FAIL] docker cp failed.
  goto :pop
)
docker exec medical-mysql sh -c "chown mysql:mysql /var/lib/mysql/sparcs_discharge_2021/sparcs_discharge_2021.ibd && chmod 640 /var/lib/mysql/sparcs_discharge_2021/sparcs_discharge_2021.ibd"

echo        Importing tablespace...
docker exec -i medical-mysql sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" 2>/dev/null" < "%SQL_DIR%\import_tablespace.sql"
if errorlevel 1 (
  echo [FAIL] IMPORT TABLESPACE failed. A MySQL version mismatch is the usual cause.
  goto :pop
)

set "FINALCNT="
docker exec -i medical-mysql sh -c "mysql -uroot -p\"\$MYSQL_ROOT_PASSWORD\" --skip-column-names 2>/dev/null" < "%SQL_DIR%\count_rows.sql" > "%TEMP%\medcnt.txt"
set /p FINALCNT=<"%TEMP%\medcnt.txt"
echo [OK]   Data import done, row count = %FINALCNT%

:hdfs
rem ---- 3. Build & start HDFS + Redis ----
echo [3/5] Building/starting HDFS + Redis containers...
docker compose up -d --build medical-hdfs medical-redis
if errorlevel 1 (
  echo [FAIL] Failed to start medical-hdfs / medical-redis.
  goto :pop
)
call :wait_healthy medical-hdfs 300

rem ---- 4. Upload Parquet snapshot to HDFS (optional, idempotent) ----
echo [4/5] Checking HDFS data...
if not exist "%PARQUET_DIR%" (
  echo [SKIP] No local Parquet snapshot - backend will generate it from MySQL automatically.
  goto :summary
)
docker exec medical-hdfs bash -c "hdfs dfs -test -e /data/sparcs_snapshot.parquet" >nul 2>&1
if not errorlevel 1 (
  echo [OK]   /data/sparcs_snapshot.parquet already on HDFS - skipping upload.
  goto :summary
)
echo        Uploading Parquet snapshot to HDFS /data/ ...
docker cp "%PARQUET_DIR%" medical-hdfs:/tmp/snapshot_parquet
docker exec medical-hdfs bash -c "hdfs dfs -mkdir -p /data && hdfs dfs -put /tmp/snapshot_parquet /data/sparcs_snapshot.parquet && rm -rf /tmp/snapshot_parquet"
if errorlevel 1 (
  echo [WARN] HDFS upload failed - re-run this script later to retry.
) else (
  echo [OK]   Uploaded to /data/sparcs_snapshot.parquet
)

rem ---- 5. Generate machine-specific .env (LLM: local Ollama or online API) ----
echo [5/5] Generating backend .env config...
if "%PY_CMD%"=="" (
  python --version >nul 2>&1 && set "PY_CMD=python"
)
if "%PY_CMD%"=="" (
  py -3 --version >nul 2>&1 && set "PY_CMD=py -3"
)
if "%PY_CMD%"=="" (
  echo [WARN] Python not found - install Python 3.12 first, then run:
  echo        python %PROJECT_ROOT%data_process\scripts\generate_env.py
) else (
  pushd "%PROJECT_ROOT%data_process"
  %PY_CMD% scripts\generate_env.py
  popd
)

:summary
echo.
echo ============================================================
echo  Deployment summary
echo ============================================================
docker compose ps
echo.
echo   MySQL  : 127.0.0.1:3306  (db: sparcs_discharge_2021, root / see deploy\.env)
echo   HDFS   : hdfs://127.0.0.1:8020  Web UI: http://localhost:9870
echo   Redis  : 127.0.0.1:6379
echo.
echo   Stop all  : stop-infra.bat  (or: cd deploy ^&^& docker compose down^)
echo   Wipe data : cd deploy ^&^& docker compose down -v   ^(deletes volumes!^)
echo ============================================================

:pop
popd
:end
pause
exit /b 0

rem ------------------------------------------------------------
rem Helper: wait until container reports healthy
rem   %1 = container name, %2 = max seconds; sets HEALTHY=1/0
rem ------------------------------------------------------------
:wait_healthy
set "HEALTHY=0"
set /a HEALTH_LEFT=%~2
:wait_loop
set "HSTATUS="
for /f "usebackq delims=" %%h in (`docker inspect -f "{{.State.Health.Status}}" %~1 2^>nul`) do set "HSTATUS=%%h"
if /i "%HSTATUS%"=="healthy" (
  set "HEALTHY=1"
  echo [OK]   %~1 is healthy
  goto :eof
)
if %HEALTH_LEFT% LEQ 0 (
  echo [TIMEOUT] %~1 still not healthy
  goto :eof
)
ping -n 6 127.0.0.1 >nul
set /a HEALTH_LEFT-=5
goto :wait_loop
