Right. I removed too much.

You wanted the install part kept, just not the uninstall part. And yes, the KDE Flatpak SDK/runtime install should be there too.

Use this version:

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
- Open a series and view the books or tracks inside it
- Edit metadata directly
- Rename files based on metadata
- Refresh changed file data after metadata edits and renames
- Track read status
- Track ownership within a series
- Track Goodreads review progress at series level
- Track Audible and Goodreads review data
- Store per-track review information where needed

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
- KDE Flatpak SDK and runtime version 6.10

This does not mean the app requires the KDE desktop. It only means the Flatpak build currently targets the KDE Flatpak runtime.

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
flatpak install flathub org.kde.Sdk//6.10 org.kde.Platform//6.10
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

## Screenshots

Screenshots can be added here later.

## License

This project is licensed under the MIT License.

```

That keeps:
clone,
dependency install,
KDE SDK/runtime install,
build and install,
run,
rebuild after changes,

and removes the uninstall section.

One small correction I would also make in the text is changing “tracks” to “books or files” later if you want it to sound less like a music app.
```
