"""How this works. The rules match the Windows program."""

SECTIONS = [
    (
        'Opening the program',
        "After install, open Video Dehydrator from the application menu, or run video-dehydrator in a terminal.\n\n"
        "The window is a normal desktop program. It uses the same Qt window on Ubuntu, KDE, and other desktops.\n\n"
        "The installer keeps the program, FFmpeg, and HandBrake in your home folder. Removing it does not remove the videos.",
    ),
    (
        'What it does',
        "Point it at a folder. Scan finds videos that are too big for their picture size. Optimize makes a smaller copy with the same picture size, the same audio, and the same subtitles.\n\n"
        "The new file takes the original's place. The original is kept in a folder named .vd-originals next to it, until you delete it. Turn on Delete originals automatically to remove each original as soon as its new file checks out.",
    ),
    (
        'The size rule',
        "A 1920x1080 video is too big when the whole file is 2 GiB per hour or more. HEVC and AV1 are left alone under 3 GiB per hour. The target for that 1080p picture is 3000 kbps of video.\n\n"
        "Other picture sizes multiply those numbers by how many pixels they have compared with 1920x1080. 3840x2160 is allowed 8 GiB per hour and is encoded at 12000 kbps. 1280x720 is encoded at 1333 kbps. 720x480 is encoded at 500 kbps. A very small picture stays at 200 kbps.\n\n"
        "The Budgets row has a choice for 2160p, 1080p, 720p, and 480p. 2160p is blue, 1080p is teal, 720p is purple, and 480p is orange. Each one starts on Recommended. Half, 1.5 times, and Double lower or raise that line. HEVC and AV1 stay at one and a half times the budget you picked.\n\n"
        "The encode is software x265, two passes, medium speed. The frame rate stays as it was. The picture is not cropped or scaled. Audio that can be copied is copied. Re-encode audio that cannot be copied turns the other kinds, such as Windows Media Audio, into AAC. Subtitles are copied and are not burned into the picture.\n\n"
        "A file whose picture is already at that target rate is left alone. Dolby Vision is left alone.",
    ),
    (
        'Scanning',
        "Choose a folder, then click Scan. Include subfolders looks inside every folder within the one you chose. The list shows bloated files and files marked Can shrink, checked and ready to optimize.\n\n"
        "Can shrink means the whole file is under the budget line, but the picture is still above the target rate, so a new file should be smaller.\n\n"
        "Select all checks the rows you can see. Deselect all clears every check. Clear removes the scanned files from the list. Files still waiting in .vd-originals stay. Showing all can limit the list to Can shrink, Bloated, Slight, Heavy, Extreme, Failed, Skipped, Ready to delete, or Finished. Hidden rows are unchecked. Select shown checks the visible rows Optimize can take.\n\n"
        "Drag a column edge to change its width. The width is remembered. Drag a heading to move that column. The File column stays first. Drag the bar under the list to make the list taller or shorter. The activity box shrinks first, then Options scrolls. Double-click the bar to put the list back.\n\n"
        "GiB/h is the size of the whole file per hour of runtime. Budget is the line for that picture size. Ratio is how many times that budget the file uses. It shows two decimal places, and one more digit when the file is only just over 1.00x. Slight, shown in green, is under 1.5 times. Heavy, shown in yellow, is from 1.5 times up to 3 times. Extreme, shown in red, is 3 times the budget or more. That color is only for Bloated and Failed rows.\n\n"
        "The list beside the folder list offers Slight, Heavy, and Extreme. Choosing one checks only that group.\n\n"
        "New size is a guess of the new file. When the current picture rate can be read, the guess keeps the audio and other tracks. When that rate is missing, the guess starts with a tilde. ~Savings is the current size minus New size before the file is converted. Space Saved is the real difference after a file shrinks. Estimated savings is the current size minus New size for the checked files.",
    ),
    (
        'Converting',
        "Optimize shrinks every checked file whose status is Bloated, Can shrink, Failed, or Skipped. Parallel jobs chooses how many files encode together, from 1 to 6. Each file uses about 8 cores. One is right for most computers. Two needs about 16 cores, three about 24, four about 32, five about 40, and six about 48. On fewer cores the files share the processor and the batch does not finish sooner. Find can stop the scan after 5, 10, 25, 50, or 100 files that can be reduced. All keeps scanning the whole folder. Cancel stops the files that are running. Stop after current file lets the files already encoding finish, then leaves the rest checked. Continue keeps going. Files that already finished stay finished. Manual converts a chosen file or folder even when it is under the budget. Test savings estimates the savings first. Convert now skips that estimate. A result that is not smaller is still thrown away. Dolby Vision is still left alone.\n\n"
        "While a file is converting, a line under the activity shows how long this file has left and the estimated time remaining for the rest of the batch. On the first pass of the first file, the batch stays on estimating. 10-bit files are timed apart from 8-bit files. The time line keeps a rolling average and changes about every 30 seconds, and when a file starts. Above the bar, 4/100 means this batch has started file 4 of 100. The count moves when the next file starts. When more than one file is encoding, each file has its own bar with its name, percent, and pass. One file keeps the single bar. The bars change about twice a second.\n\n"
        "The new file is written inside .vd-originals first, then checked: it has to be smaller, the same length, the same picture size, with the same audio when that audio can be copied, and at least as many subtitles, and the start and end have to play. When the check passes, the original moves into .vd-originals and the new .mkv takes its place. When the new file is not smaller, the row is marked Skipped and later scans leave it off the list while the file size and budget stay the same. Clear skips forgets those marks.\n\n"
        "Show detailed activity lists each encode step. The activity box does not add a line for each percent. The bar along the bottom still shows the percent.\n\n"
        "Shutdown computer upon completion turns the computer off after the convert finishes. A one-minute countdown gives you a chance to cancel. If you stop the convert, the computer stays on. A scan does not turn the computer off.",
    ),
    (
        'Checking the picture',
        "Select a file marked Ready to delete and click Compare. The window plays both copies side by side: original on the left, new file on the right. Sound is the new file. It will not stay in sync with the picture. Go to the file on disk to confirm the audio.\n\n"
        "This preview is a smaller picture than the files themselves.",
    ),
    (
        'Originals and deleting',
        "Delete originals removes Ready to delete files that are checked. When none are checked, it removes the highlighted one. The new files stay. The button refuses to delete anything outside the holding folder. If the new file is missing, the original is kept. The program asks before it deletes. Don't show this in the future hides that question until the next version of the program.\n\n"
        "Undo puts back the last conversion. The originals return to where they were, and the new files are deleted. One use covers every file from that convert. It can only undo a conversion whose original is still in .vd-originals.\n\n"
        "A .plexignore file tells Plex to skip .vd-originals while originals are held there. When that holding folder is removed, the .plexignore file is removed too, unless it also names other things for Plex to skip.\n\n"
        "Each result is also written to app/convert-log.tsv. On a shared drive, the drive can keep a copy in #recycle. The Drive recycle bin section explains that copy.",
    ),
    (
        'Auto',
        "Delete originals automatically does the deletion as soon as a file checks out. The check looks at size, length, picture size, audio, subtitles, and whether the start and end play. It does not judge how the picture looks. The first time Auto is turned on, the program explains that Compare and Undo cannot bring the original back. You can hide that explanation. The program still asks before every convert with Auto turned on, until you check Stop asking and click Convert. Cancel leaves that warning on.\n\n"
        "On a shared drive, the matching copy in #recycle is removed only when Clear the drive recycle copy is on, and only when the path and the size match. The .plexignore from that convert is removed when it only hides the holding folder. When that removal fails, a window opens with a link to the manual steps. Mute hides that window after this one time. Later failures stay in the activity list.",
    ),
    (
        'Drive recycle bin',
        "A shared drive can keep its own copy of a file after Video Dehydrator deletes it. The copy goes in a folder named #recycle at the top of the share. The folders underneath stay the same, including .vd-originals. That folder is on the drive. It is separate from the trash on this computer.\n\n"
        "Clear the drive recycle copy starts off. With it on, after Delete originals, after Delete originals automatically removes a file, or after undo removes a new file, the program looks for that one copy. It builds the path from the share mount, then #recycle, then the same folders. The file has to sit directly inside .vd-originals, and the size has to match the file that was just deleted. When both match, that copy is deleted. With the box off, #recycle is left alone. The .plexignore from that convert is removed too, when the recycled copy contains only the lines that hide .vd-originals. A .plexignore that lists anything else stays. Empty folders left behind are removed, and the folders above them are removed when those are empty too. A folder that still contains another file or folder is left, so another computer can finish its own delete. Removal stops at the #recycle folder. Anything else in the bin stays. The new video that stays in the library is left alone.\n\n"
        "When this step cannot remove the copy, a window opens. The link in that window opens this section.\n\n"
        "To remove a leftover yourself:\n\n"
        "1. Open the share in the file manager. The #recycle folder is at the top of that share.\n"
        "2. Open #recycle, then the same show and season folders, then .vd-originals.\n"
        "3. Delete the original file there. A file deleted from inside #recycle is removed for good.\n"
        "4. Leave every file that is outside a .vd-originals folder.\n\n"
        "List the holding folders. Change /mnt/share to the mount of your share:\n\n"
        "find '/mnt/share/#recycle' -type d -name '.vd-originals' -print\n\n"
        "Remove only those folders:\n\n"
        "find '/mnt/share/#recycle' -type d -name '.vd-originals' -exec rm -rf '{}' +\n\n"
        "To remove one file:\n\n"
        "rm -f '/mnt/share/#recycle/TV/Show/Season 1/.vd-originals/Episode.avi'\n\n"
        "Keep a path in single quotes. A delete run on the drive itself leaves the file out of #recycle.",
    ),
    (
        'Updates',
        "The program checks the Linux releases on GitHub when it opens. Check for updates looks again. Update downloads the Linux archive, opens a terminal, and runs its installer. The program opens again when that finishes.\n\n"
        "Report a bug opens a GitHub page. This program does not send the report. You submit it on the GitHub page. Include latest activity adds the latest lines from the activity box, which can include folder paths.\n\n"
        "Donate opens https://ko-fi.com/philipvern. The program stays free.",
    ),
]
