#!/bin/bash
# Overdrive installer for Raspberry Pi OS (Pi 500 / Pi 5 family)
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/overdrive"

echo "⚡ Installing Overdrive to $APP"
mkdir -p "$APP" "$HOME/.local/share/applications"
cp "$SRC/overdrive.py" "$SRC/overdrive.html" "$SRC/overdrive.svg" "$APP/"
chmod +x "$APP/overdrive.py"

cat > "$HOME/.local/share/applications/overdrive.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Overdrive
Comment=Show off every part of your Raspberry Pi 500
Exec=python3 $APP/overdrive.py
Icon=$APP/overdrive.svg
Terminal=false
Categories=System;Utility;
EOF
chmod +x "$HOME/.local/share/applications/overdrive.desktop"

DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESK" ]; then
  cp "$HOME/.local/share/applications/overdrive.desktop" "$DESK/"
  chmod +x "$DESK/overdrive.desktop"
  gio set "$DESK/overdrive.desktop" metadata::trusted true 2>/dev/null || true
  echo "✓ Desktop shortcut added"
fi
echo "✓ Added to the menu under System Tools"

# Optional: let Overdrive flash the LEDs (keyboard lock lights + activity LED)
if [ "$1" != "--no-sudo" ]; then
  echo
  read -r -p "Let Overdrive control the Pi's LEDs for the light show? Needs sudo once. [Y/n] " a
  if [ "${a,,}" != "n" ]; then
    echo 'SUBSYSTEM=="leds", ACTION=="add", RUN+="/bin/chmod a+w /sys%p/brightness /sys%p/trigger"' \
      | sudo tee /etc/udev/rules.d/99-overdrive-leds.rules >/dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=leds --action=add
    echo "✓ LED access enabled"
  fi
  if ! id -nG "$USER" | grep -qw gpio; then
    sudo usermod -aG gpio "$USER" && echo "✓ Added you to the gpio group (log out and back in)"
  fi
fi

echo
echo "Done! Launch Overdrive from the desktop icon or the menu."
