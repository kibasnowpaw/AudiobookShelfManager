Use this full version instead:

````md
# BookLibConnect

BookLibConnect is a personal audiobook library manager built with Python and Qt.

It is made for managing large audiobook collections in a way that fits my own workflow better than typical audiobook downloaders or media-player-style tools.

## Current status

Alpha

The app already does most of what I want, but it is still in active development and not fully finished yet.

## What it currently does

- Browse audiobook collections by author and series
- Show custom series or album covers in a clean grid view
- Open a series and view the books or files inside it
- Edit metadata directly
- Rename files based on metadata
- Refresh changed file data after metadata edits and renames
- Track read status
- Track ownership within a series
- Track Goodreads review progress at series level
- Track Audible and Goodreads review data
- Store per-track or per-book review information where needed

## Review tracking

The app can track review progress from both Goodreads and Audible.

- Goodreads review progress is shown at the series level, alongside read and owned counts
- Audible review data is not shown the same way at series level right now
- Audible review information can be added per track or per book

## Why this exists

Most existing tools I tried did not fit the way I manage audiobooks.

Some feel more like media players than library managers. This project is focused on library overview, metadata control, series tracking, and clean presentation.

## Planned features

Things I still want to add:

- Search function
- Better sorting options
- A-Z sorting
- Sort by length
- Sort by newest to oldest
- Sort by oldest to newest
- More statistics
- Track-based statistics
- Better long-term stats than Goodreads
- A new name for the project
- Testing on more systems

## Known limitations

- Still in alpha
- Only tested on Ubuntu Server 25.10 with KDE Plasma and Ubuntu Server 25.10 with GNOME so far
- The project name will change later
- Some features and views are still built around older music-style naming and need cleanup

## Notes

Back up your library before using metadata editing or rename features.

## Build requirements

To build this Flatpak from source you currently need:

- git
- flatpak
- flatpak-builder
- Flathub enabled
- KDE Flatpak SDK version 6.10
- KDE Flatpak Platform version 6.10
- PySide BaseApp version 6.10

This does not mean the app requires the KDE desktop. It only means the Flatpak build currently targets the KDE Flatpak runtime and PySide BaseApp.

## Clone the repository

```bash
git clone https://github.com/kibasnowpaw/BookLibConnect.git
cd BookLibConnect
````

## Install dependencies

```bash
sudo apt update
sudo apt install git flatpak flatpak-builder -y
flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak install flathub org.kde.Sdk//6.10 org.kde.Platform//6.10 io.qt.PySide.BaseApp//6.10
```

## Build and install

```bash
rm -rf build-dir .flatpak-builder
flatpak-builder --force-clean build-dir io.github.kibasnowpaw.BookLibConnect.json
flatpak-builder --install --user --force-clean build-dir io.github.kibasnowpaw.BookLibConnect.json
```

## Run the app

```bash
flatpak run --user io.github.kibasnowpaw.BookLibConnect
```

## Rebuild after changes

```bash
cd ~/BookLibConnect
rm -rf build-dir .flatpak-builder
flatpak-builder --install --user --force-clean build-dir io.github.kibasnowpaw.BookLibConnect.json
```

## Uninstall

```bash
flatpak uninstall --user io.github.kibasnowpaw.BookLibConnect
```

## Troubleshooting

If the build fails because of missing Flatpak dependencies, run the dependency install command again:

```bash
flatpak install flathub org.kde.Sdk//6.10 org.kde.Platform//6.10 io.qt.PySide.BaseApp//6.10
```

If you want to do a completely clean rebuild:

```bash
flatpak uninstall --user io.github.kibasnowpaw.BookLibConnect
rm -rf build-dir .flatpak-builder
flatpak-builder --force-clean build-dir io.github.kibasnowpaw.BookLibConnect.json
flatpak-builder --install --user --force-clean build-dir io.github.kibasnowpaw.BookLibConnect.json
```

## Screenshots

Screenshots can be added here later.

## License

This project is licensed under the MIT License.

```

One small thing I would also change in the actual repo later is the app name and maybe the wording in the UI from `album` and `track` toward `series`, `book`, or `file`. That would make the whole project feel less like a music tool adapted for audiobooks.
```
