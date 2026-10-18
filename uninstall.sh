#!/bin/bash
# Removes Overdrive and its shortcuts (leaves the optional LED udev rule unless --all)
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
rm -rf "$HOME/.local/share/overdrive"
rm -f "$HOME/.local/share/applications/overdrive.desktop" "$DESK/overdrive.desktop"
if [ "$1" = "--all" ]; then
  sudo rm -f /etc/udev/rules.d/99-overdrive-leds.rules && sudo udevadm control --reload-rules
fi
echo "Overdrive removed."
