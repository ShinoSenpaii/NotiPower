#!/usr/bin/env bash
# Removes NotiPower completely
systemctl --user disable --now notipower.service 2>/dev/null || true
rm -f  "$HOME/.config/systemd/user/notipower.service" \
       "$HOME/.local/share/applications/notipower.desktop" \
       "$HOME/.local/share/icons/notipower.svg"
rm -rf "$HOME/.local/share/notipower" "$HOME/.config/notipower"
systemctl --user daemon-reload 2>/dev/null || true
echo "NotiPower removed. (Remove it from your Steam library manually if you added it.)"
