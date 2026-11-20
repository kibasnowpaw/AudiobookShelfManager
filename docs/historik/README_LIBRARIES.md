# Audiobook Shelf Manager - Separate libraries (2026-09-29)

For the latest update and installation commands, see **README_M4B_SUPPORT.md**.

Your existing collection becomes **Main**. Create **Adult** or any other named
library, then switch using the dropdown in the main toolbar or Statistics.
Each library has its own collection, listening history, ratings, favorites,
series totals, scan index and statistics. No combined-library view is used.

## Create another gallery

1. Open **Settings > Open Settings** (Ctrl+,).
2. On **Libraries**, click **New library...** and enter a name, such as Adult.
3. Click **Add folder...** and select that library's root folder.
4. Leave **Open this library after saving** checked and click **Save**.
5. Use the toolbar dropdown to return to Main or another library.

Choose a root containing author folders, with series folders underneath,
following the same folder layout as your current collection. Several folders
can belong to one library; those folders are combined only within that library.
Different libraries cannot share the same folder or use parent/child folders
that overlap. Existing symlink and same-directory aliases are checked too.

Libraries can be renamed in Settings without changing their saved data.
**Cancel** discards the entire Settings draft, including newly added libraries
and folder changes. Switching does not move or copy audiobook files.

New libraries start with empty personal history, ratings and favorites.
Assigning a previously used folder to a different library does not transfer its
old personal data: that history remains in the original library. Removing a
folder does not erase its listening history or delete its files.

## Startup and general settings

- **Open at startup:** Main is the default. Choose a specific library or
  **Last used library**.
- **Listening speed estimate:** choose 0.50x-4.00x for the estimate in Statistics.
  This stays in sync with the speed control on Statistics. Playback is not
  measured or changed.
- **Remember sorting:** remember writer and series sorting separately for each
  library. Turn it off to return to A-Z.
- **Rename audiobook files after saving metadata:** choose the editor's default.
- **Also rename the matching sidecar cover:** choose the corresponding cover
  default. The editor still lets you change either option for an individual save.
- **Show the metadata warning report after scans:** the report is optional;
  yellow warning markers and Statistics metadata counts remain available.

File deletion continues to start unchecked in every removal dialog. Tag counts
now have per-library and per-series rules; Exactly 10 remains the initial
default. See README_GALLERY_SETTINGS.md for the new tag rules, card display
controls and independent Reviewed marker. Precise ratings and prior features
remain included.

## Safe switching

An unfinished metadata edit stays open: save or cancel it before switching.
An active file save cannot change libraries halfway through. During a scan,
switching cancels that scan and waits for it to finish stopping. A pending
Statistics calculation or cover read finishes before its old view is removed.
Old results and artwork are cleared before the new library is displayed.

Disconnected folders remain configured. The app does not silently replace an
unavailable or empty library with Main's folders. An empty library can still
open Statistics. After reconnecting a drive, use **Rescan** to restore its gallery
without restarting the app. Scanning and refreshing affect only the selected library.

## Existing data and backups

Main keeps the existing listening database and saved values in place. Its scan
cache, review flags and expected series totals continue using their existing
locations. The old folder configuration is backed up before its first import.

New libraries keep separate databases and backups under the existing permanent
data directory. The library names, folder assignments and general preferences
are stored there too, so removing disposable application configuration does not
lose the library identities.

The default permanent directory is:

```text
~/.local/share/booklibconnect/
```

`BOOKLIBCONNECT_DATA_DIR`, when explicitly configured, remains supported.
The old internal directory name is kept for compatibility.

**Settings > Backups** offers **Save settings and back up now** and **Save
settings and open backup folder** for the selected library. Database backups
contain listening history, ratings, favorites and the general Reviewed marker. Keep separate backups of your
audiobook files. Automatic daily database backups remain enabled.

## Install into the existing project

Close the application, then run:

```bash
unzip -o \
  ~/Downloads/AudiobookShelfManager_SEPARATE_LIBRARIES_20260929.zip \
  -d ~

cd ~/AudiobookShelfManager

flatpak-builder --install --user --force-clean \
  build-dir \
  io.github.kibasnowpaw.AudiobookShelfManager.json

flatpak run --user io.github.kibasnowpaw.AudiobookShelfManager
```

This cumulative update includes the complete updated modules and earlier
Statistics/decimal-rating update files. Keep your existing manifest,
pyproject.toml, entry point and remaining source files. No new runtime package
is required; the public package version remains 1.48. No personal database or
media files are included.

## Verification

The separate-libraries build passed 112 tests. The current cumulative build
passes **136 tests**, including the new gallery-settings checks.

The included tests cover old folder migration, offline folders, overlap and
symlink rejection, independent databases and scan stores, separate history and
precise ratings, independent Statistics, settings cancellation, rename defaults,
per-library sorting, startup choices, backup contents, failed settings writes,
and switching during scanning, Statistics reads, thumbnail reads and saving.
Tests also cover closing during a pending switch and recovery after disposable
configuration is removed.

The previous loading, saving, cover, metadata, removal and decimal-rating tests
remain included. Tests use isolated databases and generated MP3 fixtures. The
Settings pages and gallery/Statistics switchers were checked in the running Qt
interface with a dark palette.

```bash
QT_QPA_PLATFORM=offscreen python3 -m pytest -q tests
```

The full Flatpak build and switching between your real library folders still
need checking on your computer.
