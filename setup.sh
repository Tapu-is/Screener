#!/usr/bin/env sh
# One-shot setup for Termux (also works on normal Linux/macOS).
# Run from inside the project folder:  sh setup.sh
set -e

echo "==> stk setup"

if command -v pkg >/dev/null 2>&1; then
  echo "==> Termux detected, installing Python"
  pkg update -y
  pkg install -y python git
fi

echo "==> Installing Python packages"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

DIR="$(pwd)"
chmod +x "$DIR/bin/stk"

# Make `stk` available from any folder
if [ -n "$PREFIX" ] && [ -d "$PREFIX/bin" ]; then
  ln -sf "$DIR/bin/stk" "$PREFIX/bin/stk"
  echo "==> Installed command: stk"
else
  mkdir -p "$HOME/.local/bin"
  ln -sf "$DIR/bin/stk" "$HOME/.local/bin/stk"
  echo "==> Installed command: stk (add \$HOME/.local/bin to PATH if needed)"
fi

echo
echo "Done. Try:"
echo "  stk doctor"
echo "  stk quote RELIANCE"
