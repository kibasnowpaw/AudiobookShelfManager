# AudiobookShelfManager: faster loading and save progress

Based on the latest August 30 ratings/narrator/200x200-cover update. The public
package and launcher remain AudiobookShelfManager 1.48 and audiobookshelfmanager.

What changed:
- Storage initialization runs after the window appears, in a background thread.
- Book opening loads metadata, history, preferences and artwork in the background.
- Artist/series warning badges reuse scan results instead of reading every MP3 cover.
- Cover-validation results persist between launches. Changes to the MP3, sidecar,
  or folder artwork invalidate the check. Opening a book verifies its current cover.
- Book cards appear before their artwork is filled in gradually.
- Resizing the window no longer rereads the selected book's artwork.
- Save Metadata shows an animated "Saving, please wait..." dialog.
- The dialog stays open through writing, cover verification, rename and data refresh.
- A second save is blocked while the first is running. Closing waits for it to finish.
- Cover verification still retries once. On failure the cover remains pending and
  the file is not renamed. Errors close the wait indicator and explain the problem.
- Reload From File performs a fresh background read.

Changed files: booklibconnect/ui_mainwindow.py, scan_index_store.py, review_store.py.
The matching metadata_io.py, listening_history_store.py and models.py are included
for compatibility with the preceding ratings/narrator update.

Installation (close the running app first):

```bash
unzip -o ~/Downloads/AudiobookShelfManager_FAST_LOADING_SAVE_PROGRESS_20260905.zip -d ~
cd ~/AudiobookShelfManager
flatpak-builder \
  --install-deps-from=flathub \
  --install --user --force-clean \
  build-dir \
  io.github.kibasnowpaw.AudiobookShelfManager.json
flatpak run --user io.github.kibasnowpaw.AudiobookShelfManager
```

This is an update for the existing project, not a standalone Flatpak installer.
The ZIP starts directly with AudiobookShelfManager/, without an extra staging folder.
It includes no user databases, listening data, audiobook files, build output or credentials.

The first scan after installing this update needs to populate the new cover cache.
Existing version-3 index metadata is reused during the upgrade to index version 4.
The permanent listening-history database schema is unchanged. Later unchanged scans
skip MP3 cover reads. Full-library timing still depends on storage and collection size.

Validation: 11 tests passed with real generated MP3 files and PySide6's offscreen
Qt event loop, including timer responsiveness during slow startup/open/save, save
success, duplicate-save blocking, retry success, failed retry, write errors, safe
close during save, ratings/favorites after rename, fresh Reload From File, and
cover-cache reuse/invalidation/old-index migration. Python compilation passed.
The actual Flatpak build and timing on your full audiobook collection must be
checked on your computer.

Optional developer test command (pytest, PySide6, mutagen and ffmpeg required):

```bash
QT_QPA_PLATFORM=offscreen BOOKLIBCONNECT_DEBUG=0 PYTHONPATH=. \
  python3 -m pytest -q tests/test_responsive_io.py
```

Threading reference used during implementation:
https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html
