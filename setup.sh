#!/usr/bin/env sh
# Setup for Termux (Android), macOS and Linux. Run inside the project folder:
#   sh setup.sh          terminal app
#   sh setup.sh --web    terminal app + the web app's packages (macOS, Linux)
set -e

WEB=0
[ "$1" = "--web" ] && WEB=1

echo "==> stk setup"
DIR="$(pwd)"
if [ ! -f "$DIR/requirements.txt" ]; then
  echo "Run this from inside the stk folder (the one that contains requirements.txt)." >&2
  exit 1
fi

if command -v pkg >/dev/null 2>&1; then
  # Termux: use the system Python (do not upgrade pip, Termux forbids it)
  echo "==> Termux detected, installing Python"
  pkg update -y
  pkg install -y python git
  echo "==> Installing Python packages"
  python -m pip install -r requirements.txt
  [ "$WEB" = "1" ] && echo "Note: the web app is not set up on Termux; run it on a computer instead."
else
  PY="$(command -v python3 || command -v python || true)"
  if [ -z "$PY" ]; then
    echo "Python 3 was not found." >&2
    echo "macOS: install it with  brew install python  (or from python.org)." >&2
    echo "Linux: install python3 and python3-venv with your package manager." >&2
    exit 1
  fi
  echo "==> Creating a private Python environment in .venv"
  "$PY" -m venv "$DIR/.venv"
  echo "==> Installing Python packages"
  "$DIR/.venv/bin/python" -m pip install --upgrade pip
  "$DIR/.venv/bin/python" -m pip install -r requirements.txt
  if [ "$WEB" = "1" ]; then
    echo "==> Installing the web app (Streamlit)"
    "$DIR/.venv/bin/python" -m pip install -r requirements-web.txt
  fi
fi

chmod +x "$DIR/bin/stk"

# Make `stk` available from any folder
if [ -n "$PREFIX" ] && [ -d "$PREFIX/bin" ]; then
  ln -sf "$DIR/bin/stk" "$PREFIX/bin/stk"
  echo "==> Installed command: stk"
else
  mkdir -p "$HOME/.local/bin"
  ln -sf "$DIR/bin/stk" "$HOME/.local/bin/stk"
  echo "==> Installed command: stk"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *)
      echo "    Add this line to ~/.zshrc (macOS) or ~/.bashrc (Linux), then open a new terminal:"
      echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
      ;;
  esac
fi

echo
echo "Done. Try:"
echo "  stk doctor"
echo "  stk quote RELIANCE"
[ "$WEB" = "1" ] && echo "  stk web"
true
