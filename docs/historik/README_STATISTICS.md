# Audiobook Shelf Manager - Rating number field fix (2026-09-16)

For the latest update and installation commands, see **README_M4B_SUPPORT.md**.
Statistics now reports only the selected library; the features below remain included.

Open **Statistics** in the toolbar, or press **Ctrl+Shift+S**.
This cumulative update includes the earlier Statistics, missing-file,
background loading and saving improvements.

## Precise personal ratings

Open a book, choose **Edit Metadata**, and type in **Rating (0-5)** directly
below **Title**. The number box is now larger and belongs to the metadata form.
Opening Edit brings the form into view, even after a previous book was scrolled
to the cover or Save buttons.

You can also click **Edit rating...** beside the Book Details stars. This opens
the editor, scrolls to the number box and selects its value so you can type
immediately. You do not need to click any stars.

Values from **0.01 to 5.00** are supported, with two decimal places. Both `3.61`
and `3,61` are accepted. The stars and exact number preview the entered value
immediately: **3.61 out of 5** shows three full stars and 61% of the fourth star.

Click **Save Metadata** to save the number. Cancelling the edit discards the
pending rating. **Clear rating** selects Unrated; save to apply that change.
Zero means Unrated, as in the previous version. Clicking the stars directly on
Book Details still provides the existing quick whole-star rating action.

- Book Details and book, series and writer card badges show fractional stars
  alongside the precise number.
- Series averages use rated books only. Writer averages give each rated series
  equal weight. Calculations keep their precision; displayed averages round to
  two decimal places.
- History, collection tables, recap captions, numeric sorting and Statistics
  averages preserve decimal ratings.
- The rating chart uses five labelled ranges: 0.01-1.00, 1.01-2.00, 2.01-3.00,
  3.01-4.00 and 4.01-5.00, plus Unrated. Every rated book appears in one range.
  Clicking a range shows its books. The My rating filter also offers exact saved
  scores, such as Exactly 3.61 out of 5. Relistens do not duplicate rating votes.
- The five-star recap shelf still means exactly 5 out of 5.
- Ratings remain personal database values, separate from MP3 metadata and
  Goodreads or Audible flags.

Existing whole-star ratings, favorites and listening history keep their values.
The first launch backs up an existing older database and upgrades its format
version to 3. Older app builds refuse that database instead of truncating its
decimal ratings. To roll back the app, use the matching pre-upgrade database
backup as well; keep the current database when doing so.

A failed MP3 write does not commit the pending rating. If metadata is saved but
the rating cannot be written, the app reports that separately and keeps the
entered number available to retry, including after a successful file rename.

## Full-page Statistics

Statistics uses the main window's central page. Its toolbar replaces the normal
library toolbar while Statistics is open: **Back**, **Listening**, **History**,
**Collection**, **Year recap**, period and **Refresh**. Back restores the normal
navigation. The duplicate Statistics heading and inner tabs are hidden.

Repeated explanatory paragraphs have been shortened or moved into tooltips.
The charts, filters and saved-data calculations remain available. Year recap
keeps 200x200 covers and gives long titles enough caption space to show ratings.
Collection still describes current ownership and lifetime completion, so its
period control is disabled.

## Remove a saved book, with optional file deletion

In **Statistics > History**, set **Files** to **Unavailable files** to find a
deleted MP3's saved entry. Select the row and click **Remove book...**. The same
action is available in Collection's book table, including for existing MP3s.

The confirmation shows the title, exact file path and number of listening
entries. **Remove saved data** erases that book's listening entries across all
years, personal rating and favorite status. Totals update after removal. Other
books are unchanged. An existing MP3 stays in the collection when this option
alone is used; opening it again can register it with empty listening history.

**Also delete the audiobook file (move to Trash)** is unchecked every time the
dialog opens. It is never remembered. Only the explicitly selected MP3 is moved;
sidecar artwork, shared folder artwork and neighboring files are kept. The
checkbox is disabled for missing, unverified or mismatched files.

- A SQLite backup is created before removal. If backup creation fails, removal
  stops. Backups are in the permanent database's `backups/` directory.
- The operation runs in the background with a progress dialog.
- The saved state and MP3 identity are checked again before removal. If either
  changed while the dialog was open, refresh and try again.
- If moving to Trash fails, saved-data removal is rolled back. The app never
  falls back to permanently deleting the MP3.
- Local Trash uses Qt. Flatpak document paths use the desktop Trash portal.
  If a later database failure occurs, the app attempts to restore a local
  trashed file. Portal moves do not provide a restore path; the error explains
  when the MP3 must be restored from Trash manually.
- After optional file deletion, a rescan updates the collection and cards.

## Writer ratings

Writer cards show a gold star rating calculated from their rated series.
Each series average has equal weight, regardless of how many books it contains.
Unrated books do not contribute to a series average, and unrated series do not
contribute to a writer average. A series listed twice for a writer counts once.

For example, a series average of 5 and a series average of 1 give the writer
3/5. **Highest rated writer** and **Lowest rated writer** sorting put unrated
writers last. Personal book-rating changes update the credited writer cards.

## Genre tag rules

Audiobooks initially require exactly **10 unique genre tags**. Settings now
lets each library or series choose no rule, exactly, at least, at most or a
range. Counts outside the configured rule receive a yellow metadata warning,
which propagates from book to series to writer. Book Details and the editor explain how many
tags to add or remove. Collection's metadata chart includes **Too few genre
tags**, **Too many genre tags** and **Missing genre**.

Commas, semicolons and vertical bars remain supported separators. Case and
Unicode-equivalent duplicates count once. This validation does not invent tags
or change an MP3's metadata automatically.

## Deleted and renamed files

This cumulative update includes Statistics Explorer and fixes stale file paths:

- Statistics checks file availability on a worker thread when opened/refreshed.
  This checks file status, without reopening every MP3 for metadata or artwork.
- Listening history defaults to **Available files**. The **Files** filter also
  offers **All saved history** and **Unavailable files**. Missing files have a
  visible status and cannot open Book Details. Chart drilldowns include all
  matching saved history so their counts still agree with the charts.
- The cover gallery and current collection exclude unavailable files. Historical
  longest/shortest highlights can still describe an unavailable book; its cover
  becomes a labelled placeholder and cannot be opened.
- If a file disappears after the last refresh, the first attempted open
  refreshes Statistics automatically instead of repeatedly showing an error.
- Deleting a file outside the app does not erase listening dates, ratings, favorites or totals.
  Disconnected drives and lost file access do not delete saved history either.
- A completed rescan reconnects an existing book after an external rename/move
  using its embedded ASM_TRACK_ID. Duplicate IDs and destinations already
  assigned to another book are not guessed or merged. Files replaced with a
  different ID are not assumed to be the same book solely from their title.
- Existing scan indexes are reused. The first scan reads and caches IDs that
  older indexes did not retain; later unchanged scans reuse them.

After installing, run **Rescan** once to update renamed paths, then open
Statistics. Use **Refresh** after deleting a file while Statistics is open.
New files and other metadata changes still require a rescan. If a library drive
or Flatpak folder permission is unavailable, reconnect/reselect that folder and
rescan; this update cannot restore a physically deleted MP3.

## Listening

- Select a year with saved history or All time.
- Completed listens, different books and relistens remain separate.
- Monthly/yearly bars switch between completed listens and known audio hours.
- Click a bar, or focus the chart and use the arrow keys and Enter, to inspect
  the matching history. Unrated books have their own clickable rating bar.
- Explore genre tags, authors and narrators. People can be ranked by different
  books or completed listens; genre counts always use different books.
- The selected playback speed defaults to 2x and is remembered. Estimated
  listening time divides known audio length by this speed. The app does not
  measure actual playback, pauses or changes of speed.
- Calendar days to finish, favorites, ratings and length highlights remain.

## Listening history and filters

Combine genre, author, narrator, personal rating and finish month/year filters.
Switch between completed listens and different books. In the latter view, dates
and lengths describe the latest matching listen for each book.

The sortable table now includes cover thumbnails. Double-click a row, press
Enter on the selected row or choose Open book to open Book Details.
Clicking a chart starts a fresh filter selection; further controls can narrow it.
Clear filters restores available files in the selected period. Overview totals continue to
represent the whole selected year or All time.

Back from a book opened through Statistics returns to Statistics and refreshes
its saved data. Back from there returns to the library root. Visiting Statistics
and returning without opening another book still preserves the previous page.
If an earlier metadata edit is open, choosing another book returns you to that
editor so you can finish or cancel it without losing your changes.

## Collection

Collection is the available files from the latest scan plus lifetime listening history. Its year picker
is disabled to make this scope clear.

- Valid owned books, finished-at-least-once books and books with no recorded finish.
- Known missing books, series with complete ownership and fully listened series.
- Owned/expected and finished/owned figures for each series.
- Average personal series ratings calculated from rated books only.
- Missing book numbers when the expected total and book numbering are consistent.
- Filters for missing books, unknown/conflicting totals and completed series.
- Clickable metadata issue counts: genres, narrator, Album/folder mismatch,
  cached cover problems and covers not yet checked.
- Filter collection books by series, completion, unrated status, favorites or
  metadata attention, then open a book directly.

A missing expected total stays Unknown. Inconsistent numbers, duplicates or
conflicting expected totals do not produce a claim of complete ownership.
Without reliable book numbers, a known gap count can still be displayed, while
its exact missing volume numbers remain unknown. Unknown series are excluded
from the known-missing sum and are counted separately as uncertain gaps.

"No recorded finish" can include an in-progress book. Issue counts may overlap
on the same book. Statistics does not rescan or write tags to produce these
figures; rescanning updates them when files or metadata have changed elsewhere.

## Year recap

The recap shows completed listens, different books, relistens, known audio
length, personal average rating, favorites, and the most listened author and
narrator. Tied top credits use the first name in the existing ranking.

Longest and shortest books have covers. Choose a cover shelf for your five-star
books, favorites or all different books finished in the selected period.
Cover areas are 200x200, preserve image proportions and open Book Details.
All time produces an all-time recap. The current year is labelled as in progress.
Recap image export is not part of this update.

## Counting and data rules

- Saved finish dates determine periods. Valid start and finish dates are
  required; invalid legacy entries are counted in a notice.
- Relistens are classified over the entire history before filtering by year.
  A listen can be a different book within a year and also a relisten from an
  earlier year. The two figures are not intended to add to completed listens.
- Permanent track identities determine different-book counts. Each supported
  audiobook file remains a book, consistent with the existing application.
- Event duration snapshots determine audio totals; current scan lengths never
  replace older listening-event durations.
- Missing, non-finite or non-positive durations stay unknown. They are excluded
  from hour totals, speed estimates and longest/shortest comparisons.
- Zero means no recorded completions. Future empty months and hours for a
  period with only unknown lengths show a dash. Future-dated saved finishes
  produce a notice so they can be corrected in history.
- Current saved ratings and favorites are used. Relistens add no extra rating
  votes, and unrated books are excluded from the average rather than scored zero.
- Genre delimiters match the editor: comma, semicolon and vertical bar.
  Case-insensitive repeated tags are counted once per book.
- Multi-tag and multi-credit bars can sum to more than the overall book count.
  A book receives one count per distinct credited author/narrator in that chart.
- Genres come from current cached metadata. Books absent from that scan can
  retain history and saved people credits while their genre remains Unknown.
- Unknown genres, narrators, totals or durations are not replaced with guesses.
- The application does not import Goodreads, StoryGraph or CLZ account data.
  This build contains no invented listening entries or example library.

## Responsiveness and compatibility

Statistics calculations read the saved SQLite database and a copied scan cache
on a worker thread, including file availability checks. They never reopen every
MP3 or write metadata for Statistics. Cover reads
are separate, lazy and limited to one background worker, with a bounded cache
of 200x200 thumbnails keyed by the scanner's artwork signature. Hidden pages
cancel pending thumbnail work. Closing waits asynchronously for an active read.

The earlier startup, background book loading, Save progress dialog, duplicate
save protection, cover verification/retry, ratings, narrator handling and
metadata warnings remain included. The database version updates automatically
after making a backup; no new runtime package is required. The public package
version remains 1.48.

## Install into the existing project

Close the application, then merge the update into your existing project:

```bash
unzip -o \
  ~/Downloads/AudiobookShelfManager_RATING_INPUT_FIX_20260916.zip \
  -d ~

cd ~/AudiobookShelfManager

flatpak-builder \
  --install-deps-from=flathub \
  --install --user --force-clean \
  build-dir \
  io.github.kibasnowpaw.AudiobookShelfManager.json
```

Start normally or run:

```bash
flatpak run --user io.github.kibasnowpaw.AudiobookShelfManager
```

This is an update package for the existing project. Keep the existing manifest,
pyproject.toml, entry point and other application files. The archive contains
complete versions of its included Python files and includes the earlier
September 5 update files. No personal database or media files are included.

## Validation

All 89 included tests passed. They cover the old loading/saving/cover checks, saved statistics,
read-only database access, genre and credit counting, combined filters, series
gaps and uncertainty, numeric sorting, native chart clicks, book navigation,
unsaved edits, lazy cover caching, signature changes and safe asynchronous close.
The new cases cover deleted files, loss of file access, deletion between refresh
and opening, ID-based reconnection after a real MP3 rename, preservation of
history/ratings/favorites, ambiguous-ID protection and reuse of cached IDs.
Additional checks cover explicit removal, unchecked file deletion, real local
Trash moves, database backups and rollback, changed-file protection, portal
request/response handling, writer averages/sorting, exact tag counts and full-page
navigation. Decimal tests cover version-2 data upgrades and backups, reopened
ratings, fractions below one star, invalid input, dot/comma entry, pending edits,
cancel/clear, MP3 and database write failures, retry after rename, replacement
identities, exact filters, chart range counts, precise averages and fractional
star rendering. The input-location checks click Edit rating, type 3.61, save it,
verify the exact stored result and check that Edit Metadata reveals the input
after changing books from an editor scrolled to the bottom.
Qt layouts were rendered at 1440px and 900px widths, including
the rating editor, Book Details, card badges and Statistics.

Tests use isolated databases and generated media fixtures. To run them from the
complete project with pytest, PySide6, mutagen and ffmpeg installed:

```bash
QT_QPA_PLATFORM=offscreen python3 -m pytest -q tests
```

The full Flatpak build, live desktop Trash portal and real-library performance
still need checking on the user's computer. Portal protocol tests use a fake
bus; local Trash tests use real generated MP3s in an isolated Trash directory.
