#!/bin/bash
# Remove the Video Dehydrator install from the home folder. Videos are left alone.
set -euo pipefail

DEST="${VIDEO_DEHYDRATOR_HOME:-$HOME/.local/opt/video-dehydrator}"
BIN_DIR="$HOME/.local/bin"

case "$(basename "$DEST")" in
  video-dehydrator) ;;
  *)
    echo "The install folder must be named video-dehydrator."
    echo "VIDEO_DEHYDRATOR_HOME is: $DEST"
    exit 1
    ;;
esac
case "$DEST" in
  /|/home|/root|"$HOME"|"$HOME/.local"|"$HOME/.local/opt")
    echo "Refusing to remove $DEST"
    exit 1
    ;;
esac

rm -f "$BIN_DIR/video-dehydrator"
rm -f "$HOME/.local/share/applications/video-dehydrator.desktop"
rm -f "$HOME/.local/share/icons/hicolor/256x256/apps/video-dehydrator.png"
if [[ -f "$HOME/.bashrc" ]]; then
  path_tmp="$(mktemp)"
  awk '
    $0 == "# >>> video-dehydrator path >>>" { skip=1; next }
    $0 == "# <<< video-dehydrator path <<<" { skip=0; next }
    skip { next }
    { print }
  ' "$HOME/.bashrc" > "$path_tmp"
  mv "$path_tmp" "$HOME/.bashrc"
fi
if [[ -d "$DEST" ]]; then
  rm -rf "$DEST"
  echo "Removed $DEST"
else
  echo "No install folder at $DEST"
fi
echo "The videos were left where they are."
echo "Settings that were stored inside the install folder were removed with it."
echo "HandBrake's Flatpak, if you installed it, is still there. Remove it with:"
echo "  flatpak uninstall --user fr.handbrake.ghb"
