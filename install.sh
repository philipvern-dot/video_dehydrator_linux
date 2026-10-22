#!/bin/bash
# Install Video Dehydrator into the home folder. Re-running keeps settings.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="${VIDEO_DEHYDRATOR_HOME:-$HOME/.local/opt/video-dehydrator}"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"

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
    echo "Refusing to install into $DEST"
    exit 1
    ;;
esac

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run this installer from your normal account, not the root prompt."
  echo "The program installs into that account's home folder."
  echo "Leave this prompt with: exit"
  echo "Then run: bash install.sh"
  exit 1
fi

apt_installed() {
  dpkg-query -W -f '${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'
}

install_missing_packages() {
  local packages=(python3 python3-venv python3-pip flatpak curl xz-utils ca-certificates)
  local missing=()
  local pkg
  for pkg in "${packages[@]}"; do
    if ! apt_installed "$pkg"; then
      missing+=("$pkg")
    fi
  done
  if [[ ${#missing[@]} -eq 0 ]]; then
    return 0
  fi
  echo "Installing missing packages: ${missing[*]}"
  echo "Ubuntu will ask for your password."
  if [[ "$(id -u)" -eq 0 ]]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
  else
    if ! command -v sudo >/dev/null 2>&1; then
      echo "sudo is not available. Install these packages as root, then run this installer again:"
      echo "  apt-get update && apt-get install -y ${missing[*]}"
      exit 1
    fi
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
  fi
  hash -r
}

ensure_user_path() {
  local file="$HOME/.bashrc"
  local begin="# >>> video-dehydrator path >>>"
  touch "$file"
  if grep -qF "$begin" "$file"; then
    return 0
  fi
  cat >> "$file" << 'EOF'

# >>> video-dehydrator path >>>
if [ -d "$HOME/.local/bin" ] ; then
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) PATH="$HOME/.local/bin:$PATH" ;;
    esac
fi
# <<< video-dehydrator path <<<
EOF
}

if command -v apt-get >/dev/null 2>&1 && command -v dpkg-query >/dev/null 2>&1; then
  install_missing_packages
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is missing. On Ubuntu this installer installs it. Install Python 3.10 or newer and run this installer again."
  exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required. This system has $(python3 --version 2>&1)."
  exit 1
fi
if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  echo "Python's venv module is missing. On Ubuntu this installer installs python3-venv. Install that package and run this installer again."
  exit 1
fi
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  echo "curl or wget is missing, so FFmpeg cannot be downloaded."
  exit 1
fi
if ! command -v xz >/dev/null 2>&1; then
  echo "xz is missing, so the FFmpeg archive cannot be opened."
  exit 1
fi

echo "Installing into $DEST"
mkdir -p "$DEST/app" "$DEST/tools" "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

rm -rf "$DEST/app/video_dehydrator"
cp -a "$ROOT/app/video_dehydrator" "$DEST/app/video_dehydrator"
find "$DEST/app/video_dehydrator" -depth -type d -name '__pycache__' -exec rm -rf {} + || true
cp -a "$ROOT/version.txt" "$ROOT/LICENSE" "$ROOT/requirements.txt" "$ROOT/vd-icon.png" "$DEST/"
cp -a "$ROOT/tools/SOURCES.txt" "$DEST/tools/SOURCES.txt"

venv_python="$DEST/venv/bin/python"
if [[ ! -x "$venv_python" ]] || ! "$venv_python" -m pip --version >/dev/null 2>&1; then
  echo "Creating the Python environment..."
  rm -rf "$DEST/venv"
  if ! python3 -m venv "$DEST/venv"; then
    echo "Could not create a Python environment. Install python3-venv and run this installer again."
    exit 1
  fi
fi
if ! "$venv_python" -m pip --version >/dev/null 2>&1; then
  "$venv_python" -m ensurepip --upgrade
fi
"$venv_python" -m pip install --upgrade pip || true
"$venv_python" -m pip install -r "$ROOT/requirements.txt"

ffmpeg_release="7.0.2"
need_ffmpeg=0
for tool in ffmpeg ffprobe; do
  if [[ ! -x "$DEST/tools/$tool" ]]; then
    need_ffmpeg=1
  fi
done
if [[ "$need_ffmpeg" -eq 0 ]]; then
  if ! "$DEST/tools/ffmpeg" -version 2>/dev/null | head -n 1 | grep -F "ffmpeg version $ffmpeg_release" >/dev/null; then
    echo "Replacing FFmpeg with $ffmpeg_release..."
    need_ffmpeg=1
  fi
fi
if [[ "$need_ffmpeg" -eq 1 ]]; then
  case "$(uname -m)" in
    x86_64) archive="ffmpeg-release-amd64-static.tar.xz" ;;
    aarch64|arm64) archive="ffmpeg-release-arm64-static.tar.xz" ;;
    *)
      echo "No static FFmpeg build is prepared for $(uname -m)."
      exit 1
      ;;
  esac
  url="https://johnvansickle.com/ffmpeg/releases/$archive"
  echo "Downloading FFmpeg..."
  work="$(mktemp -d)"
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --retry 3 -o "$work/ffmpeg.tar.xz" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$work/ffmpeg.tar.xz" "$url"
  else
    echo "curl or wget is needed to download FFmpeg."
    rm -rf "$work"
    exit 1
  fi
  tar -xJf "$work/ffmpeg.tar.xz" -C "$work"
  unpacked=""
  for dir in "$work"/ffmpeg-*-static "$work"/ffmpeg-*; do
    if [[ -d "$dir" && -f "$dir/ffmpeg" && -f "$dir/ffprobe" ]]; then
      unpacked="$dir"
      break
    fi
  done
  if [[ -z "$unpacked" ]]; then
    echo "The FFmpeg archive did not contain ffmpeg and ffprobe."
    rm -rf "$work"
    exit 1
  fi
  install -m 755 "$unpacked/ffmpeg" "$unpacked/ffprobe" "$DEST/tools/"
  if [[ -f "$unpacked/ffplay" ]]; then
    install -m 755 "$unpacked/ffplay" "$DEST/tools/"
  fi
  for license in GPLv3.txt COPYING LICENSE LICENSE.txt; do
    if [[ -f "$unpacked/$license" ]]; then
      cp "$unpacked/$license" "$DEST/tools/FFmpeg-LICENSE.txt"
      break
    fi
  done
  rm -rf "$work"
  echo "FFmpeg is in $DEST/tools"
fi

handbrake_note=""
if ! command -v flatpak >/dev/null 2>&1; then
  handbrake_note="Flatpak is not installed, so HandBrake was not installed."
else
  echo "Checking HandBrake..."
  flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
  if ! flatpak info --user fr.handbrake.ghb >/dev/null 2>&1; then
    echo "Installing HandBrake from Flathub. This download is large."
    flatpak install --user -y --noninteractive flathub fr.handbrake.ghb
  else
    echo "Updating HandBrake from Flathub..."
    if ! flatpak update --user -y --noninteractive fr.handbrake.ghb; then
      echo "HandBrake could not be updated. The installed copy will be used."
    fi
  fi
  cat > "$DEST/tools/HandBrakeCLI" << 'EOF'
#!/bin/sh
exec flatpak run --filesystem=host --command=HandBrakeCLI fr.handbrake.ghb "$@"
EOF
  chmod 755 "$DEST/tools/HandBrakeCLI"
fi

cat > "$BIN_DIR/video-dehydrator" << EOF
#!/bin/bash
ROOT="$DEST"
export PYTHONPATH="\$ROOT/app\${PYTHONPATH:+:\$PYTHONPATH}"
cd "\$ROOT"
exec "\$ROOT/venv/bin/python" -m video_dehydrator "\$@"
EOF
chmod 755 "$BIN_DIR/video-dehydrator"

cp -a "$ROOT/vd-icon.png" "$ICON_DIR/video-dehydrator.png"
cat > "$APP_DIR/video-dehydrator.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Video Dehydrator
Comment=Shrink videos that are too big for their picture size
Exec=$BIN_DIR/video-dehydrator
Icon=$ICON_DIR/video-dehydrator.png
Terminal=false
Categories=AudioVideo;Video;
StartupNotify=true
EOF
chmod 644 "$APP_DIR/video-dehydrator.desktop"

ensure_user_path

echo
echo "Installed Video Dehydrator $(tr -d '[:space:]' < "$DEST/version.txt")."
echo "Open a new terminal and run: video-dehydrator"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo "In this terminal, run: ~/.local/bin/video-dehydrator"
fi
if [[ -n "$handbrake_note" ]]; then
  echo
  echo "$handbrake_note"
  echo "On Ubuntu, run:"
  echo "  sudo apt install flatpak"
  echo "  flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo"
  echo "Then run this installer again."
fi
echo
echo "Settings stay in $DEST/app and are kept when you run this installer again."
