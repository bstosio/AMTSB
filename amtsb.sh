#!/usr/bin/env bash
# AMTSB – Automatic Movie Translator SuBtitles (macOS / Linux)
# Pierwsze uruchomienie tworzy środowisko w ~/.amt (wspólne z AMT) i instaluje biblioteki.
# Uruchomienie:  bash amtsb.sh
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AMT_DIR="${AMT_HOME:-$HOME/.amt}"
PY="$AMT_DIR/venv/bin/python"

if [ ! -x "$PY" ]; then
  PYTHON="$(command -v python3.12 || command -v python3.11 || command -v python3 || true)"
  if [ -z "$PYTHON" ]; then
    echo "Nie znaleziono Pythona 3. Zainstaluj Python 3.11 lub 3.12:"
    echo "  macOS: https://www.python.org/downloads/  (albo: brew install python@3.12 python-tk@3.12)"
    echo "  Linux: sudo apt install python3 python3-venv python3-tk"
    exit 1
  fi
  if ! "$PYTHON" -c "import tkinter" 2>/dev/null; then
    echo "Brak modułu tkinter (okienka). Linux: sudo apt install python3-tk   macOS/Homebrew: brew install python-tk"
    exit 1
  fi
  echo "Pierwsze uruchomienie – przygotowuję środowisko w $AMT_DIR …"
  mkdir -p "$AMT_DIR"
  "$PYTHON" -m venv "$AMT_DIR/venv"
fi

if [ "$(uname)" = "Linux" ] && ! command -v tesseract >/dev/null 2>&1; then
  echo "Uwaga: do trybu „Napisy z ekranu” potrzebny jest Tesseract:  sudo apt install tesseract-ocr"
fi

STAMP="$AMT_DIR/.amtsb-deps-$(cksum < "$DIR/requirements.txt" | cut -d' ' -f1)"
if [ ! -f "$STAMP" ]; then
  if [ ! -t 1 ] && [ "$(uname)" = "Linux" ]; then   # uruchomione z menu – instalację pokaż w terminalu
    for t in gnome-terminal konsole xfce4-terminal x-terminal-emulator xterm; do
      command -v "$t" >/dev/null 2>&1 || continue
      case "$t" in
        gnome-terminal) exec "$t" -- bash "$DIR/amtsb.sh" "$@" ;;
        xfce4-terminal) exec "$t" -x bash "$DIR/amtsb.sh" "$@" ;;
        *) exec "$t" -e bash "$DIR/amtsb.sh" "$@" ;;
      esac
    done
  fi
  echo "Instaluję biblioteki (kilka minut, tylko raz)…"
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r "$DIR/requirements.txt"
  touch "$STAMP"
fi

if [ "$(uname)" = "Linux" ]; then   # skrót z ikoną w menu aplikacji (i obok skryptu)
  APPS="$HOME/.local/share/applications"
  mkdir -p "$APPS"
  cat > "$APPS/amtsb.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AMTSB – Automatic Movie Translator SuBtitles
Comment=Tłumaczenie napisów filmu na żywo
Exec=bash "$DIR/amtsb.sh"
Icon=$DIR/assets/amtsb_256.png
Terminal=false
Categories=AudioVideo;Video;
EOF
  cp "$APPS/amtsb.desktop" "$DIR/AMTSB.desktop" 2>/dev/null && chmod +x "$DIR/AMTSB.desktop" 2>/dev/null || true
fi

exec "$PY" "$DIR/amtsb.py" "$@"
