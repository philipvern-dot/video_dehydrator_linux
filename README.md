# Video Dehydrator for Linux

This is the Linux copy of Video Dehydrator 1.0.9.8. It does the same jobs as the Windows program: scan a folder, shrink videos that are too big for their picture size, check the new file, and keep or delete the original. The window is a normal desktop window. It runs on Ubuntu and on other desktops that can show a Qt window.

The picture rules match Windows 1.0.9.5: the same x265 medium two-pass rates, the same audio copy, and the same checks. A file made on this computer will not be byte-for-byte the same as a file made on the Windows PC. The target rate is the same, so the size and the kind of picture stay the same.

The Windows program stays in the `windows` folder. This copy does not change it.

## What's new in 1.0.9.8

A successful update closes the installer terminal by itself. The program opens again when the installer finishes. If the installer stops, the terminal stays open so the message can be read.

## What's new in 1.0.9.7

Update opens a terminal and runs the installer there. The program opens again when the installer finishes. The same log is kept at `~/.local/state/video-dehydrator/install.log`.

## What's new in 1.0.9.6

Drag the edge of a column to make it wider or narrower. The width is remembered. Check boxes are white with a dark outline. A checked box is dark with a white check, so it stands out on the light window.

Check for updates reads the Linux releases. Update downloads that release's Linux archive and runs its installer.

## What's new in 1.0.9.5

Above the progress bar, a count such as 4/100 shows how many files in this batch have started. It changes to 5/100 when the next file starts. The bar still shows the file that is encoding.

## What's new in 1.0.9.4

The drive recycle check looks again when a deleted original has not appeared in #recycle yet. A share can finish moving that file after the delete returns. The check tries immediately, then after 1, 2, 4, and 8 seconds. A copy that shows up at the same size is removed. A copy that never shows up still opens the warning.

## What's new in 1.0.9.3

The check at the end of a file keeps a good encode when the only notes are repeated equal timestamps and one short AC3 frame. That pair shows up on some VirtualDub Xvid files. A backwards timestamp, a picture error, or more than one broken AC3 frame still fails the file.

## What's new in 1.0.9.2

Parallel jobs can run up to six files at once. The choice sits just to the left of Find. Each file still uses about 8 cores. The tooltip says how many cores each level needs. On a smaller processor, extra files share the cores and the batch does not finish sooner.

Find can stop the scan after 5, 10, 25, 50, or 100 files that can be reduced. All keeps scanning the whole folder.

A file that is still on its first pass keeps its holding folder while another file in the same folder finishes. If the second pass cannot write the new file, the program tries that same two-pass encode again. The bar along the bottom updates twice a second.

## Install

The installer puts the program in your home folder. The first run needs a network connection. It downloads a static FFmpeg build and installs HandBrake from Flathub. HandBrake's download is large. The Windows program ships HandBrake 1.11.2. This installer uses that same HandBrake from Flathub and updates it when you run the installer again.

On Ubuntu, the installer checks for `python3`, `python3-venv`, `python3-pip`, `flatpak`, `curl`, `xz-utils`, and `ca-certificates`. It installs any that are missing and asks for your password. It also adds the Flathub remote.

From this folder:

```bash
bash install.sh
```

When it finishes:

```bash
video-dehydrator
```

You can also open Video Dehydrator from the application menu.

The installer adds `~/.local/bin` to `~/.bashrc`. Open a new terminal, then run `video-dehydrator`. In the terminal where you ran the installer, use:

```bash
~/.local/bin/video-dehydrator
```

Running `bash install.sh` again updates the program, replaces FFmpeg when the installed build is not 7.0.2, updates the HandBrake Flatpak, and keeps your folder, budget choices, held originals, skipped files, and convert log.

## Try it on this PC

Ubuntu is already installed in WSL. From PowerShell:

```powershell
wsl -d Ubuntu
```

Then:

```bash
cd /mnt/e/Programming/video_dehydrator/linux
bash install.sh
```

Open a new Ubuntu terminal and run:

```bash
video-dehydrator
```

WSLg shows the window on the Windows desktop. The install itself lands in the Linux home folder, `~/.local/opt/video-dehydrator`, so the program does not run from the Windows drive after that.

Shutdown computer upon completion asks Linux to power off. In WSL that stops the Ubuntu session. It does not turn off Windows. While a scan or convert is running, the program asks systemd to ignore idle sleep. The screen can still turn off. If systemd is not running, that request does nothing and the rest of the program still works.

Point Scan at a folder Linux can see. A Windows drive is under `/mnt`, for example `/mnt/z` when Z: is mounted in WSL. A share mounted in the Linux file manager works too.

## What gets installed

| Piece | Place |
| --- | --- |
| Program | `~/.local/opt/video-dehydrator` |
| Command | `~/.local/bin/video-dehydrator` |
| Menu entry | `~/.local/share/applications/video-dehydrator.desktop` |
| Icon | `~/.local/share/icons/hicolor/256x256/apps/video-dehydrator.png` |
| Settings, held originals, skips, convert log | `~/.local/opt/video-dehydrator/app` |

`VIDEO_DEHYDRATOR_HOME` changes the program folder. The folder name still has to be `video-dehydrator`.

FFmpeg and ffprobe are the static GPL builds from <https://johnvansickle.com/ffmpeg/>. The installer copies them into the tools folder and keeps the GPL text beside them. See `tools/SOURCES.txt`. Compare plays sound through the desktop.

Run the installer from your normal account. A root prompt installs into `/root`, which is a different home folder from the one that opens the window.

HandBrake is the official Flatpak `fr.handbrake.ghb` from Flathub. The tools folder holds a small `HandBrakeCLI` script that runs that Flatpak. The script asks Flatpak for the whole filesystem for that one run, so a video on a mounted share outside your home folder can still be encoded. The script does not change Flatpak's saved permissions.

Check for updates reads the Linux releases at <https://github.com/philipvern-dot/video_dehydrator_linux>. Update downloads an archive whose name contains `linux` and ends in `.tar.gz`, `.tar.xz`, or `.run`, then runs `install.sh` from that archive.

## Remove it

From this folder:

```bash
bash uninstall.sh
```

That removes the program, the menu entry, the command, and the settings stored in the install folder. It does not delete videos. It does not remove the HandBrake Flatpak. The uninstall script prints the Flatpak command if you want that gone too.

## License

The program is MIT. See `LICENSE`.

HandBrake is GPL-2.0. FFmpeg is GPL-3.0. `tools/SOURCES.txt` names the builds the installer uses and where the source is published.
