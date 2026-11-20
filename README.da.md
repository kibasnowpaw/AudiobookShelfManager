# Audiobook Shelf Manager 1.49

[English](README.md) · [Udgivelsesnoter](docs/releases/1.49.md)

Lokal lydbogssamling til Linux med MP3- og ukrypterede M4B-filer. Du kan organisere forfattere og serier, redigere metadata og omslag og holde styr på dine ratings, køb og lytninger. Hvert bibliotek har sine egne personlige data og indstillinger.

I denne udgave vises omslag, ratings og status på bogkortene, når du åbner en serie. De synlige omslag hentes først i baggrunden, og resten af serien følger. Bogdetaljer kan genbruge et forberedt omslag, hvis billedet og lydfilen stadig er uændrede. Lyttehistorik og rating læses fortsat på ny, når du åbner bogen. Opgradering fra den omdøbte 1.48-Flatpak bruger samme app-ID og dataplacering. Tag gerne en sikkerhedskopi under **Settings → Backups** før opgradering. Pakken indeholder ingen lydbøger eller personlige data.

## Byg og kør med Flatpak

Luk den kørende app. Har du Git, kan du hente præcis denne udgave med de første to kommandoer. Har du hentet **Source code (zip)** fra GitHub, så pak den ud, åbn en terminal i mappen med `pyproject.toml`, og start ved `flatpak remote-add`.

```bash
git clone --branch V1.49 --depth 1 https://github.com/kibasnowpaw/AudiobookShelfManager.git AudiobookShelfManager-1.49
cd AudiobookShelfManager-1.49
flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak-builder --user --install --install-deps-from=flathub --force-clean \
  build-dir io.github.kibasnowpaw.AudiobookShelfManager.json
flatpak run --user io.github.kibasnowpaw.AudiobookShelfManager
```

Du skal have Flatpak, flatpak-builder og Flathub tilgængelige. Når du installerer, opdateres appen med samme Flatpak-ID; en separat kildemappe opretter ikke automatisk et nyt personligt bibliotek. **Settings → Libraries** bruges til at vælge bibliotek; **Settings → Backups** kan tage en sikkerhedskopi af de personlige data. Den installerede app kan også åbnes fra Linux-menuen som **Audiobook Shelf Manager**.

Vil du tilbage til den tidligere programkode, kan du bygge fra din tidligere projektmappe med samme Flatpak-kommando. Det kræver ingen sletning af lydbøger eller database.

## Kør direkte fra Python

På et Linux-skrivebord med Python 3.10 eller nyere:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
audiobookshelfmanager --version
audiobookshelfmanager
```

## Brug

- Tilføj din lydbogsmappe under Indstillinger. Scannerens mappestruktur er bibliotek → forfatter → serie → lydbogsfil.
- **Rescan** finder ændringer og genbruger tidligere kontroller. Første scanning skal stadig læse hver ny bog.
- **Edit Metadata** redigerer tags, omslag og din personlige rating (f.eks. `3.61`). M4B-kapitler og lyd genkodes ikke ved redigering.
- **Statistics** viser lyttehistorik og den aktuelle samling. Oplysninger om manglende filer bevares, indtil du selv vælger at fjerne dem.
- Brug **Settings** til bl.a. biblioteker, genrekrav og mærker på bogkortene. Visningen af Goodreads, Audible og egne anmeldelser kan styres separat.

Programmet er en samlings- og metadataeditor. Det afspiller ikke lydbøger.

## Hvis appen er langsom eller fejler

Genskab problemet, gå til **Settings → Diagnostics**, og vælg **Save report…**. Du kan læse hele teksten, før du sender den sammen med en kort beskrivelse af, hvad du klikkede på. Brug **Refresh** for at opdatere forhåndsvisningen. **Copy report** kopierer samme tekst.

Hvis vinduet ikke kan åbnes, kan du hente rapporten fra terminalen uden at starte brugerfladen:

```bash
flatpak run --user io.github.kibasnowpaw.AudiobookShelfManager --diagnostics > ~/AudiobookShelfManager-diagnostics.txt
```

Rapporten viser systemet på eksporttidspunktet samt de seneste tre programstarter (op til 200 hændelser pr. start). Tider er i UTC og varigheder i millisekunder. Hændelser gemmes løbende i en separat lokal cache. En manglende afslutning kan også betyde, at appen stadig kører; en forsinkelse i vinduet kan også skyldes, at computeren har været i dvale. Systemoplysninger, der ikke er tilgængelige inde i Flatpak, kan mangle.

Ingen data sendes automatisk. Rapporten indeholder ikke bogtitler, filstier, biblioteksnavne, lyd, omslag, lyttehistorik eller den rå udviklerlog. **App** viser udgivelsesversionen; **Build** viser, hvilken kode rapporten stammer fra. En kildeinstallation kan bruge `audiobookshelfmanager --diagnostics`.

Ved en langsom genscanning viser `scan.discovery` tiden til at finde bøgerne, `scan.checks` fil- og omslagskontroller, `scan.index` opdateringen af indeks og forventede serietotaler, og `scan.history` kontrollen af gemte filplaceringer. `cover_ms` er en del af `scan.checks`. `index_written=0` betyder, at det eksisterende indeks kunne genbruges uden en ny skrivning. `portal_roots` er antallet af mapper med en kendt Flatpak-dokumentportalsti; ingen stier eller dokument-id'er gemmes i rapporten.

Ved åbning af en bog viser `book.open` tiden for metadata, omslag og historik hver for sig. Her betyder `cover_cached=1`, at omslagskontrollen kunne genbruges. `history_written=0` betyder, at bogen og lytningerne blev læst direkte fra databasen uden at ændre noget. Begge tal gælder kun den valgte bog. `preview_cached=1` betyder, at den viste bog har genbrugt det forberedte billede fra serielisten. `book.render` viser tiden til at tegne bogsiden.

Vil du teste seriekortene, så åbn en serie uden at vælge en bog. Kortene skal få deres omslag og status af sig selv. Hændelsen `series.covers` viser antal bogkort stillet i kø (`books`), antal forberedt (`prepared`) og fejl (`failed`). En bog uden et omslag kan stadig være forberedt. Tiden er den samlede læsetid i baggrunden; en kø kan blive afbrudt ved skift af serie eller bibliotek.

## Test og ændringer

[Åbning af bøger: ændringer og måling](docs/BOGAABNING.md) · [Genscanning: ændringer og måling](docs/GENSCANNING.md) · [Gennemgang og målte resultater](docs/GENNEMGANG.md) · [Rå målinger](docs/benchmark.json) · [Tidligere versionsnoter](docs/historik/)

Udviklertestene bruger midlertidige filer. De kræver ffmpeg og ffprobe samt Python-afhængighederne:

```bash
python -m pip install -e '.[test]'
QT_QPA_PLATFORM=offscreen BOOKLIBCONNECT_DEBUG=0 python -m pytest -q
PYTHONPATH=. python tools/bench_scan.py --art mixed
```
