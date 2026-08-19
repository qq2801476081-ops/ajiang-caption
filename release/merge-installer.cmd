@echo off
setlocal
cd /d "%~dp0"

set "PART1=Ajiang-Caption-0.9.3-Installer.exe.part1"
set "PART2=Ajiang-Caption-0.9.3-Installer.exe.part2"
set "OUTPUT=Ajiang-Caption-0.9.3-Installer.exe"
set "EXPECTED=82255F1D71CD8720285817D6201A40A520B32EDFF834DB39545B2CDF5D312335"

if not exist "%PART1%" goto missing
if not exist "%PART2%" goto missing

echo Merging installer parts...
copy /b "%PART1%"+"%PART2%" "%OUTPUT%" >nul
if errorlevel 1 goto failed

for /f "skip=1 tokens=*" %%H in ('certutil -hashfile "%OUTPUT%" SHA256 ^| findstr /v /c:"CertUtil"') do if not defined ACTUAL set "ACTUAL=%%H"
set "ACTUAL=%ACTUAL: =%"
if /i not "%ACTUAL%"=="%EXPECTED%" goto badhash

echo.
echo Success: %OUTPUT%
echo SHA256: %ACTUAL%
echo You can now run the installer.
pause
exit /b 0

:missing
echo Both %PART1% and %PART2% must be in this folder.
pause
exit /b 1

:failed
echo Failed to merge installer parts.
pause
exit /b 1

:badhash
echo SHA256 verification failed. Download both parts again.
echo Expected: %EXPECTED%
echo Actual:   %ACTUAL%
del /q "%OUTPUT%" >nul 2>nul
pause
exit /b 1
