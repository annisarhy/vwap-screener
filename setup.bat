@echo off
setlocal enabledelayedexpansion
title VWAP Screener - Auto Setup

echo.
echo ========================================
echo   VWAP SCREENER - AUTO SETUP
echo ========================================
echo.

REM Check if we're in the right folder
if not exist "main.py" (
    echo [ERROR] main.py tidak ditemukan.
    echo Pastikan kamu jalankan script ini di dalam folder vwap-screener
    echo.
    pause
    exit /b 1
)

echo [1/4] Membuat struktur folder...

REM Create subfolders
if not exist "data"     mkdir data
if not exist "signals"  mkdir signals
if not exist "screener" mkdir screener
if not exist "notify"   mkdir notify
if not exist "backtest" mkdir backtest

echo       Folder data, signals, screener, notify dibuat.

REM Create __init__.py in each folder
echo. > data\__init__.py
echo. > signals\__init__.py
echo. > screener\__init__.py
echo. > notify\__init__.py
echo. > backtest\__init__.py

echo       File __init__.py dibuat di semua subfolder.
echo.

REM ── Move files if they exist in root ─────────────────────────────────────
echo [2/4] Memindahkan file ke subfolder yang benar...

set MOVED=0

if exist "fetcher.py" (
    move /Y fetcher.py data\fetcher.py >nul
    echo       fetcher.py  →  data\fetcher.py
    set /A MOVED+=1
)

if exist "vwap.py" (
    move /Y vwap.py signals\vwap.py >nul
    echo       vwap.py     →  signals\vwap.py
    set /A MOVED+=1
)

if exist "engine.py" (
    move /Y engine.py screener\engine.py >nul
    echo       engine.py   →  screener\engine.py
    set /A MOVED+=1
)

if exist "telegram.py" (
    move /Y telegram.py notify\telegram.py >nul
    echo       telegram.py →  notify\telegram.py
    set /A MOVED+=1
)

if !MOVED!==0 (
    echo       Semua file sudah di posisi yang benar.
) else (
    echo       !MOVED! file berhasil dipindahkan.
)
echo.

REM ── Verify structure ─────────────────────────────────────────────────────
echo [3/4] Verifikasi struktur...

set ERRORS=0

call :check_file "main.py"
call :check_file "Dockerfile"
call :check_file "docker-compose.yml"
call :check_file "requirements.txt"
call :check_file "data\fetcher.py"
call :check_file "data\__init__.py"
call :check_file "signals\vwap.py"
call :check_file "signals\__init__.py"
call :check_file "screener\engine.py"
call :check_file "screener\__init__.py"
call :check_file "notify\telegram.py"
call :check_file "notify\__init__.py"

if !ERRORS! GTR 0 (
    echo.
    echo [PERINGATAN] !ERRORS! file tidak ditemukan.
    echo Download file yang kurang dari chat Claude dan taruh di lokasi yang benar.
) else (
    echo       Semua file lengkap!
)
echo.

REM ── Git commit and push ───────────────────────────────────────────────────
echo [4/4] Push ke GitHub...

REM Check if git is available
git --version >nul 2>&1
if errorlevel 1 (
    echo [SKIP] Git tidak terdeteksi. Install dari https://git-scm.com
    echo        Setelah install, jalankan manual:
    echo        git add .
    echo        git commit -m "fix: restructure project folders"
    echo        git push
    goto :done
)

REM Check if this is a git repo
if not exist ".git" (
    echo [INFO] Belum ada git repo. Inisialisasi sekarang...
    git init
    echo.
    set /p REMOTE="Masukkan URL repo GitHub kamu (contoh: https://github.com/username/vwap-screener.git): "
    git remote add origin !REMOTE!
)

git add .
git commit -m "fix: restructure project into subfolders"
git push -u origin main 2>nul || git push -u origin master 2>nul

if errorlevel 1 (
    echo.
    echo [PERINGATAN] Push gagal. Coba jalankan manual:
    echo   git push -u origin main
) else (
    echo       Berhasil push ke GitHub!
)

:done
echo.
echo ========================================
echo   SELESAI! Struktur folder sudah benar.
echo ========================================
echo.
echo Langkah selanjutnya:
echo   1. Buka railway.app
echo   2. Deploy dari GitHub repo ini
echo   3. Set environment variables di Railway
echo   4. Start command: python main.py
echo.
pause
exit /b 0

REM ── Helper: check file exists ─────────────────────────────────────────────
:check_file
if exist %1 (
    echo   [OK] %~1
) else (
    echo   [XX] %~1  ^<-- FILE TIDAK ADA
    set /A ERRORS+=1
)
exit /b
