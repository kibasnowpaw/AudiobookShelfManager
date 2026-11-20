# Audiobook Shelf Manager - MP3 and M4B support

Cumulative update, 2 October 2026. Includes the purchase/discovery update and
all earlier gallery settings, separate libraries, decimal ratings, Statistics,
metadata warnings and background loading/saving improvements.

## Use M4B books

Put unencrypted `.m4b` files in your existing author/series folders, then click
**Rescan**. Both `.m4b` and `.M4B` are recognized. MP3 and M4B can coexist in the
same library or series. Main and Adult retain their separate personal data.

Each M4B file is one audiobook, even when it contains many chapters.

M4B books support:

- Scanning, duration, incremental caching and unreadable-file reports.
- Artist, series and book cards, with 200x200 covers.
- Title, author, album/series, album artist, narrator, translator, release date,
  genres, description and book number/series total in Edit Metadata.
- Embedded covers, sidecar covers, cover verification and one automatic retry.
- Permanent identities, listening history, relistens, decimal ratings,
  favorites, general Reviewed and portable Goodreads/Audible markers.
- Optional purchase date/source and ownership time.
- Genre/text searches, current collection totals, release-year statistics,
  writer/series averages and year recaps.
- Removal of saved records, with the separate file-to-Trash checkbox still
  unchecked by default every time.

Renaming retains the `.m4b` extension. When two formats share a sidecar cover,
renaming one keeps the original cover for the other and copies it for the new
filename. Destination basenames already used by another format or cover receive
a numeric suffix instead of overwriting or adopting unrelated artwork.

## Metadata and chapters

The program writes M4B's MP4 metadata directly. It does not convert the audio to
MP3, re-encode it, split chapters into separate books or renumber chapters when
you edit the book's position in a series.

Normal metadata, identity, review and cover saves retain the audio and existing
chapter information. Tests cover both AAC and ALAC M4Bs, files with metadata at
the beginning or end, and metadata growth that moves the audio's position in
the container. Chapter names and timings were checked independently with
FFprobe, and decoded audio hashes compared before and after saving.

Narrator-specific tags take priority. Composer remains a silent narrator
fallback for older audiobook tagging conventions. The hidden Disc metadata is
preserved, including the disc total. Other metadata outside the fields being
edited is retained.

JPEG and PNG covers are embedded in their original format. Other readable cover
images, including WebP, are converted to PNG in memory for M4B compatibility;
the source image is left unchanged. Existing cover verification still compares
the embedded result with the selected image.

M4B release dates may include a time or UTC offset. Statistics extracts the year
of the recorded calendar date. Unknown or invalid release dates remain Unknown.

Encrypted M4B audio and malformed M4B containers are reported as unreadable;
this update does not add DRM unlocking. The MP3-specific damaged-ID3 repair
routine is never applied to M4B files. Playback and chapter editing remain
outside the application's current library-management features.

Technical reference: [Mutagen MP4 metadata API](https://mutagen.readthedocs.io/en/latest/api/mp4.html).

## Install into your existing project

Close the application, then run:

```bash
unzip -o \
  ~/Downloads/AudiobookShelfManager_M4B_SUPPORT_20261002.zip \
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

Click **Rescan** after opening the app to discover M4B files in the selected
library. Use your usual author/series folder structure.

This ZIP contains complete replacement modules and cumulative update files for
your existing project. Keep its manifest, pyproject.toml, entry point and other
source files. There are no new runtime dependencies or database schema changes
in this update. The public package version remains 1.48. No personal databases,
audiobooks or test media are included.

## Verification

187 test cases passed across the full regression suite and final targeted
checks. This includes the earlier 168 tests and 19 M4B/format checks: real-file
metadata, cover, identity and review round trips; audio/chapter preservation;
corrupt and encrypted input; failed identity writes; mixed-format rescans and
cache invalidation; automatic cover retry; extension-preserving renaming;
ratings, history and purchases; independent libraries; Statistics/search;
optional Trash removal; release timestamps; and shared sidecar protection.

Native Qt checks also covered M4B editing, Book Details and collection search
using the dark theme. The full Flatpak build and your real M4B collection still
need testing on your computer.
