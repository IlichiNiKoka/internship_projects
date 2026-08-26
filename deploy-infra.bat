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
rem  The script is idempotent AND self-healing:
rem    - starts containers defined in deploy/docker-compose.yml
rem    - import decision is based on ROW COUNT of the data table
rem      (NOT on table existence), so a previously interrupted /
rem      failed .ibd import is automatically retried next run
rem    - uploads local Parquet snapshot to HDFS if present
rem    - skips every step that was already done before
rem ============================================================

set "PROJECT_ROOT=%~dp0"
set "COMPOSE_DIR=%PROJECT_ROOT%deploy"
set "SQL_DIR=%COMPOSE_DIR%\sql"
set "IBD_FILE=%PROJECT_ROOT%sparcs_discharge_2021.ibd"
set "PARQUET_DIR=%PROJECT_ROOT%data_process\processed\sparcs_snapshot.parquet"
set "TMP_OUT=%TEMP%\medsql.out"

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

rem Fetch root password once (avoids fragile multi-layer quoting of $VAR)
call :load_dbpw
if not defined DBPW (
  echo [FAIL] Cannot read MYSQL_ROOT_PASSWORD from container environment.
  echo        Check: docker inspect medical-mysql --format "{{.Config.Env}}"
  goto :pop
)

rem ---- 2. Import .ibd data file (row-count based, self-healing) ----
echo [2/5] Checking MySQL data...

rem 2a. Count tables: 0 = fresh volume, N = schema exists (maybe broken)
call :mysql_scalar "%SQL_DIR%\check_tables.sql" TABCNT
if "%TABCNT%"=="ERR" (
  echo [FAIL] Cannot query MySQL ^(wrong credentials?^). Check: docker logs medical-mysql
  goto :pop
)
if "%TABCNT%"=="" set "TABCNT=0"

set "ROWCNT=0"
set "NEED_IMPORT=0"

if "%TABCNT%"=="0" (
  echo        Schema not found - full import needed.
  set "NEED_IMPORT=1"
)

rem 2b. Schema exists: decide by actual row count, not by table presence
if not "%TABCNT%"=="0" call :decide_by_rows

rem 2c. Nothing to do -> skip ahead
if "%NEED_IMPORT%"=="0" (
  echo [OK]   Database already contains %ROWCNT% rows - skipping .ibd import.
  goto :hdfs
)

if not exist "%IBD_FILE%" (
  echo [WARN] Database is empty but data file not found: %IBD_FILE%
  echo        Place "sparcs_discharge_2021.ibd" at project root and re-run,
  echo        or load data yourself via: python load_to_db.py
  goto :hdfs
)

echo        Creating schema from deploy\schema_sparcs.sql ...
docker exec -i medical-mysql sh -c "mysql -uroot -p'%DBPW%' 2>/dev/null" < "%COMPOSE_DIR%\schema_sparcs.sql"
if errorlevel 1 (
  echo [FAIL] Schema creation failed.
  goto :pop
)

echo        Detaching empty tablespace...
docker exec -i medical-mysql sh -c "mysql -uroot -p'%DBPW%' 2>/dev/null" < "%SQL_DIR%\discard_tablespace.sql"
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
docker exec -i medical-mysql sh -c "mysql -uroot -p'%DBPW%' 2>/dev/null" < "%SQL_DIR%\import_tablespace.sql"
if errorlevel 1 (
  echo [FAIL] IMPORT TABLESPACE failed. A MySQL version mismatch is the usual cause.
  goto :pop
)

call :mysql_scalar "%SQL_DIR%\count_rows.sql" FINALCNT
if "%FINALCNT%"=="ERR" set "FINALCNT=?"
if "%FINALCNT%"=="" set "FINALCNT=0"
echo [OK]   Data import done, row count = %FINALCNT%
if "%FINALCNT%"=="0" echo [WARN] Imported table reports 0 rows - .ibd may come from a different MySQL version.

:hdfs
rem ---- 3. Build & start HDFS + Redis ----
echo [3/5] Building/starting HDFS + Redis containers...
docker compose up -d --build medical-hdfs medical-redis
if errorlevel 1 (
  echo [FAIL] Failed to build/start medical-hdfs / medical-redis.
  echo        Common causes: base image pull blocked by proxy/network.
  echo        Retry build alone:  cd deploy ^&^& docker compose build medical-hdfs
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
rem Helper: run a SQL file, fetch its single scalar result
rem   %1 = sql file, %2 = output variable name
rem   Sets %2 to: number | empty (query returned nothing) | ERR
rem ------------------------------------------------------------
:mysql_scalar
set "%~2="
set "MSCALAR="
docker exec -i medical-mysql sh -c "mysql -uroot -p'%DBPW%' --skip-column-names 2>/dev/null" < "%~1" > "%TMP_OUT%" 2>nul
if errorlevel 1 (
  set "%~2=ERR"
  goto :eof
)
for /f "usebackq delims=" %%v in ("%TMP_OUT%") do if not defined MSCALAR set "MSCALAR=%%v"
if not defined MSCALAR goto :eof
set "%~2=%MSCALAR: =%"
goto :eof

rem ------------------------------------------------------------
rem Helper: read root password from container env into DBPW
rem ------------------------------------------------------------
:load_dbpw
set "DBPW="
for /f "usebackq delims=" %%p in (`docker exec medical-mysql printenv MYSQL_ROOT_PASSWORD 2^>nul`) do set "DBPW=%%p"
goto :eof

rem ------------------------------------------------------------
rem Helper: schema exists -> check row count, flag repair needed
rem ------------------------------------------------------------
:decide_by_rows
call :mysql_scalar "%SQL_DIR%\count_rows.sql" ROWCNT
if "%ROWCNT%"=="ERR" set "ROWCNT=0"
if "%ROWCNT%"=="" set "ROWCNT=0"
echo        Found %TABCNT% table^(s^), %ROWCNT% row(s) in sparcs_discharge_2021.
if "%ROWCNT%"=="0" (
  echo        Table exists but is empty - previous import failed/incomplete, rebuilding...
  set "NEED_IMPORT=1"
)
goto :eof

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
