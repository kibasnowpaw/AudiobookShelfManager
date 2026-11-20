# Audiobook Shelf Manager - Purchases and library discovery

For the latest update and installation commands, see **README_M4B_SUPPORT.md**.

Cumulative update, 30 September 2026. Includes the earlier separate libraries,
gallery and series settings, decimal ratings, writer averages, Statistics,
removal controls, cover verification and background loading/saving improvements.

## Record when and where you bought a book

Open a book and choose **Edit Metadata**. Immediately below Rating:

- **Purchased on:** tick **Date known**, then type a date or use the calendar.
- **Bought from:** optionally enter Audible, an author website, a shop or another
  source. This is free text, with a maximum of 200 characters.
- **Clear purchase details** clears both inputs. Save to apply the change.

Click **Save Metadata** to save. Cancel discards these pending edits. Both fields
are optional and independent: you can record a shop without knowing the date,
or a date without a shop. No purchase date is assigned automatically. Untick
Date known to leave the date unrecorded while keeping the source.

Book Details shows **Purchased on**, **Bought from** and **Owned for** under
Book metadata. Ownership time uses elapsed calendar years, months and days,
plus the total days. It is calculated from the date you recorded, not the file's
creation or scan date. Unknown dates show Not recorded. Future purchase dates
are not accepted.

These personal values belong to the active library. Main and Adult remain
separate, even when copies have the same permanent book identity. The values
follow the book across renames and replacement files at the same path, and are
included in database backups. They are not embedded in MP3 tags.

A failed save keeps your purchase inputs ready to retry. If the MP3 was already
renamed before a reload failed, the editor uses the actual resulting filename
and will not mistake the old filename for a duplicate book number.

## Find books in your collection

Use **Find books** in the toolbar, or press **Ctrl+F**. This opens the active
library's **Statistics > Collection** book browser, including unfinished books.

- Search by title, author, series, narrator or purchase source. Multiple search
  words must all occur somewhere in those fields.
- Click **Genres: All**, search the genre list, tick the genres you want, and
  choose OK. Cancel leaves the existing selection alone.
- **Match all genres** requires every selected tag. **Match any genre** accepts
  at least one. Matching ignores case and compares complete tags.
- Combine genres with the status, series, release-year and purchase-source
  filters. The result count shows how many books match.
- **No genre tags** finds books with no recorded tags, when the library has any.
- **Clear filters** returns to all current books in the selected library.
- Double-click a result, or select it and choose **Open book**.

The table now includes sortable Release year, Purchased on and Bought from
columns. On smaller windows, use its horizontal scrollbar to reach them.

**Oldest purchases first** sorts known purchase dates from oldest to newest,
with unknown dates at the end. Combine it with **No recorded finish** to find
books that have been waiting longest. **Missing purchase date** and **Missing
purchase source** help fill gaps at your own pace; optional purchase information
does not trigger metadata warning badges.

## Owned books by release year

**Statistics > Collection** now includes **Owned books by release year**.
Click a bar to find the books in that year, or use All release years above the
table. The chart has horizontal scrolling when many years are present.

- Counts use available owned books from the active library's current scan.
- Each book is counted once; relistens do not add extra votes.
- The existing MP3 **Year** tag supplies the release year. Edit labels it
  **Release year**. A four-digit year or valid ISO calendar date is accepted.
- Missing, invalid or ambiguous years are grouped as **Unknown**.
- Years are sorted numerically, with Unknown at the end.
- The chart is independent of the listening-period selector. It does not infer
  historical ownership from purchase dates or try to reconstruct older scans.
- This is the release year recorded in your metadata; the app does not look up
  or distinguish an original print publication year automatically.

The chart and discovery filters use cached metadata and the local database;
opening Statistics does not reread every MP3. Use Rescan after external edits.

## Install into your existing project

Close the app, then run:

```bash
unzip -o \
  ~/Downloads/AudiobookShelfManager_PURCHASES_AND_DISCOVERY_20260930.zip \
  -d ~

cd ~/AudiobookShelfManager

flatpak-builder --install --user --force-clean \
  build-dir \
  io.github.kibasnowpaw.AudiobookShelfManager.json
```

Run normally, or use:

```bash
flatpak run --user io.github.kibasnowpaw.AudiobookShelfManager
```

This ZIP contains complete replacement modules and cumulative update files for
your existing project. Keep its manifest, pyproject.toml, entry point and other
source files. There are no new runtime dependencies. The public package version
remains 1.48. No personal database or audiobook files are included.

The listening database is backed up before its upgrade to schema 5. Existing
ratings, favorites, review markers and listening history retain their values.
Install all files in this update together. Older builds reject newer database
schemas instead of rewriting them. The previous download remains a separate
save point.

## Verification

**168 tests passed**, including real MP3 writes, purchase retention across
renames and replacements, source-only purchases, clearing and cancelling,
independent libraries, migration with a verified original backup, failed-write
and failed-reload retries, calendar age calculations, release-year totals,
combined genre/search filters, sorting, book navigation and cached metadata use.
The earlier scan, cover, rating, settings and removal checks also passed.

Native Qt checks covered Edit Metadata, Book Details, the genre picker,
collection searches and the release-year chart at smaller and wider window
sizes with a dark theme. The full Flatpak build and your actual collections
still need testing on your computer.
