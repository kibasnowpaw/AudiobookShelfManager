# Audiobook Shelf Manager - Gallery rules and card settings

For the latest update and installation commands, see **README_M4B_SUPPORT.md**.

Cumulative update, 29 September 2026. Includes separate libraries, decimal
ratings, writer averages, the full Statistics workspace, removal controls and
all earlier loading/saving improvements.

## Choose a library or one series

Open **Settings > Open Settings** and select the library at the top.

- **Tag rules** and **Card display** have an **Apply to** dropdown.
- Choose **Library defaults** to change the selected gallery.
- Choose a series to change that series and its books. Tick **Override** only
  beside the options you want to change; the other options keep following the
  library defaults.
- **Use library defaults** clears the selected series' overrides.
- Right-click a series card and choose **Series settings...** for a shortcut.
- **Choose series folder...** also works for another configured library that
  has not been scanned in this session. Select its actual series folder.
- **Save** applies the changes. **Cancel** discards the draft.

Main and Adult keep separate rules and preferences. Writer cards use the
library defaults because a writer may contain several series with different
overrides. Overrides are identified by the series folder, not its display name;
series with the same name in different folders remain separate. Moving a series
to a different folder requires selecting that new folder for its overrides.

Startup, speed estimates, rename defaults and scan-report preferences remain
on **General**. The new tag and card settings are scoped to the selected library
or series.

## Tag rules

| Mode | Result |
| --- | --- |
| No count rule | Any number, including no tags |
| Exactly | Must match the entered number |
| At least | Must have that many or more |
| At most | May have zero through that many |
| Between | Inclusive minimum and maximum |

Counts accept whole numbers from 0 through 999. Existing libraries retain
**Exactly 10** until you change the rule. Each new library also starts there.
Commas, semicolons and vertical bars separate tags. Case and Unicode-equivalent
duplicates count only once.

The same rule controls scan reports, yellow book/series/writer warnings, Book
Details, the editor and the Statistics metadata chart. Saving a rule refreshes
cached tags without a new MP3 metadata scan. No tags are inserted or removed
automatically. Other metadata checks continue to apply.

## Card display

- **Rating:** show or hide the saved decimal rating and fractional stars.
- **Rating corner:** top left, top right, bottom left or bottom right of the
  cover. The warning badge moves aside when a rating uses the top right.
- **Owned** and **Completed:** independently show or hide progress labels.
- **Goodreads reviewed**, **Audible reviewed** and **Reviewed (general):**
  independently enable the review markers you use.
- **Last listened and days to finish:** optional extra listening detail on book
  cards and Book Details, available when Completed is enabled.

The live preview shows an example series. Covers remain 200x200; card height
adjusts to fit the enabled status lines. With all status labels off, the strip
below the cover disappears. Hiding ratings does not change series or writer
averages, and hiding progress does not change Statistics totals.

Review switches also hide the corresponding fields in Book Details and Edit.
The numeric rating field remains available in **Edit Metadata** even when its
card badge is hidden, so you can continue entering values such as **3.61**.

## General Reviewed marker

Enable **Reviewed (general)**, open a book, choose **Edit Metadata**, and tick
**Reviewed**. This marker saves immediately, like the existing review controls.
It is independent of both Goodreads and Audible: none of these switches claims
a review has been published or sends anything to those services.

General Reviewed is stored in the active library's permanent database and
included in its backups. It follows the permanent book identity across renames
and replacement files. Hiding the marker does not clear it. It is not embedded
in the MP3 or shared between independent libraries.

Goodreads/Audible retain their existing compatible MP3 flags and fallback
storage. Their enabled card totals now update when you change either flag.

## Install into your existing project

Close the app, then run:

```bash
unzip -o \
  ~/Downloads/AudiobookShelfManager_GALLERY_SETTINGS_20260929.zip \
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

This ZIP contains complete replacement modules and cumulative update files.
Keep the existing manifest, pyproject.toml, entry point and remaining source.
There are no new runtime dependencies. The public package version stays 1.48.
The ZIP contains no personal database or audiobook files.

The settings catalog and listening database are backed up before their schema
upgrades. Use all files in this update together; older builds reject these newer
schema versions instead of rewriting the new saved state.

## Verification

**136 tests passed**, covering tag-rule boundaries and duplicates, old settings
and database migration, sparse series overrides, cancel/reset, independent
libraries, live warning/Statistics changes, all four rating corners, hidden
markers, card layout sizing and review state surviving actual MP3 renaming.
The earlier scanning, saving, cover verification, deletion, precise rating and
asynchronous library-switching tests also pass.

Native Qt visual checks covered Tag rules, Card display, series/book cards and
the numeric rating editor. Test data was isolated and MP3 fixtures generated.
The full Flatpak build and your real galleries still need testing on your PC.
