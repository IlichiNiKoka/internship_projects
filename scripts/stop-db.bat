@echo off
rem ============================================================
rem  Thin wrapper: stop MySQL + HDFS + Redis (Docker).
rem  Real logic lives in the root stop-infra.bat.
rem ============================================================
call "%~dp0..\stop-infra.bat"
