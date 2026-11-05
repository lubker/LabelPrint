@echo off
rem Starts the 4Power label printer. Requires Python 3.10+ from python.org
rem (tick "tcl/tk and IDLE" in the installer - it is on by default).
cd /d "%~dp0"
if not exist config.json (
  echo config.json not found - copying config.example.json. Edit it before printing.
  copy config.example.json config.json >nul
)
pyw -3 label_printer.py 2>nul || py -3 label_printer.py || python label_printer.py
