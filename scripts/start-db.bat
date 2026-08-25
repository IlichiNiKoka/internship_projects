@echo off
rem ============================================================
rem  Thin wrapper: start MySQL + HDFS + Redis via Docker.
rem  Real logic lives in the root deploy-infra.bat (idempotent).
rem ============================================================
call "%~dp0..\deploy-infra.bat"
