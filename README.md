# Audiobook Shelf Manager 1.49

[Dansk](README.da.md) · [What's new in 1.49](docs/releases/1.49.md) · [GitHub Releases](https://github.com/kibasnowpaw/AudiobookShelfManager/releases)

A Linux desktop app for organizing audiobooks you own. Browse **author → series → book**, edit tags and covers, and keep a listening history. It supports **MP3 and unencrypted M4B**. It does not play audio.

## Build and install on Linux

Use Flatpak, flatpak-builder and Flathub. If you are upgrading, close the app first. The Flatpak ID is the same as the renamed 1.48 release; you can back up your personal data under **Settings → Backups**.

If you use Git, get this exact version:

```bash
git clone --branch V1.49 --depth 1 https://github.com/kibasnowpaw/AudiobookShelfManager.git AudiobookShelfManager-1.49
cd AudiobookShelfManager-1.49
```

If you prefer a download, get **Source code (zip)** from the [1.49 release](https://github.com/kibasnowpaw/AudiobookShelfManager/releases/tag/V1.49), extract it, and open a terminal in the folder containing `pyproject.toml`. From that folder, build and launch:

```bash
flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak-builder --user --install --install-deps-from=flathub --force-clean \
  build-dir io.github.kibasnowpaw.AudiobookShelfManager.json
flatpak run --user io.github.kibasnowpaw.AudiobookShelfManager
```

After installation it also appears in your applications menu. Flathub supplies the build dependencies; this app is built from the source you downloaded.

## Starting a collection

1. In **Settings → Libraries**, choose a library and add its root folder. Use one folder per author, with a folder for each series underneath, e.g. `Audiobooks/Author/Series/01 - Title.m4b`.
2. Select **Rescan**. The first scan reads the files; later scans reuse checks for unchanged books. Open a series to see its books. Covers load in the background before you click a book.
3. Open **Book Details → Edit Metadata** to edit a book, type a rating such as `3.61`, or record where and when you bought it. M4B edits preserve the audio and existing chapters.
4. Use **Statistics** for listening history, collection progress and year recaps. Select a genre or release year to find matching books. Libraries keep their own history, ratings and settings.

For more on [library and series settings](docs/historik/README_GALLERY_SETTINGS.md), [statistics](docs/historik/README_STATISTICS.md) or [M4B support](docs/historik/README_M4B_SUPPORT.md), see the feature notes. Use the build commands **on this page** for the current release.

## Report a problem

Open **Settings → Diagnostics → Save report…**. Review the text before sharing it in a [GitHub issue](https://github.com/kibasnowpaw/AudiobookShelfManager/issues). Nothing is uploaded automatically. The report includes software versions, counts and timing; it omits book titles, file paths and listening history. If the window doesn't open:

```bash
flatpak run --user io.github.kibasnowpaw.AudiobookShelfManager --diagnostics
```

## Run from Python or contribute

On a Linux desktop with Python 3.10+:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
audiobookshelfmanager --version
audiobookshelfmanager
```

To run the tests, install ffmpeg (including ffprobe), then run the following in the same virtual environment:

```bash
python -m pip install -e '.[test]'
QT_QPA_PLATFORM=offscreen BOOKLIBCONNECT_DEBUG=0 python -m pytest -q
```

Released under the [MIT License](LICENSE).
