#!/bin/bash
# Dwuklik w Finderze uruchamia skaner na tym Macu i otwiera panel w przeglądarce.
cd "$(dirname "$0")"
(sleep 2; open "http://localhost:8080") &
python3 skaner.py
