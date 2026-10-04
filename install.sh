#!/usr/bin/env bash
# NotiPower installer for SteamOS / Steam Frame
#   curl -fsSL https://raw.githubusercontent.com/USER/notipower/main/install.sh | bash -s -- USER/notipower
set -euo pipefail

REPO="${1:-${NOTIPOWER_REPO:-YOUR_USERNAME/notipower}}"   # your GitHub  user/repo
BRANCH="${NOTIPOWER_BRANCH:-main}"
RAW="${NOTIPOWER_RAW_BASE:-https://raw.githubusercontent.com}"
DEST="$HOME/.local/share/notipower"
mkdir -p "$DEST"

command -v python3 >/dev/null || { echo "python3 not found."; exit 1; }

src="${BASH_SOURCE[0]:-}"; here=""
[ -n "$src" ] && [ -f "$src" ] && here="$(cd "$(dirname "$src")" && pwd)"
if [ -n "$here" ] && [ -f "$here/notipower.py" ]; then
  cp "$here/notipower.py" "$DEST/notipower.py"            # running from a downloaded copy of the repo
else
  case "$REPO" in
    *YOUR_USERNAME*) echo "Tell me your GitHub repo, e.g.:  ... | bash -s -- yourname/notipower"; exit 1;;
  esac
  url="$RAW/$REPO/$BRANCH/notipower.py"
  echo "Downloading $url"
  if command -v curl >/dev/null; then curl -fsSL "$url" -o "$DEST/notipower.py"
  else wget -qO "$DEST/notipower.py" "$url"; fi
fi

head -n1 "$DEST/notipower.py" | grep -q python || { echo "Download failed."; exit 1; }
chmod +x "$DEST/notipower.py"
python3 "$DEST/notipower.py" --install

# remember where updates come from (used by the "Update available" button)
case "$REPO" in
  *YOUR_USERNAME*) ;;
  *) mkdir -p "$HOME/.config/notipower"; echo "$REPO@$BRANCH" > "$HOME/.config/notipower/repo" ;;
esac

if ! command -v chromium >/dev/null && ! command -v firefox >/dev/null && \
   ! command -v flatpak >/dev/null; then
  echo "Note: no web browser found. NotiPower's window opens in a browser (Firefox/Chromium)."
  echo "      Background mode works without one."
fi
echo "Done. Open NotiPower from the app menu, or in Steam: + > Add a Non-Steam Game > NotiPower."
