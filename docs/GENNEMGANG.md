# Gennemgang · 20. november 2026

Udgangspunktet var 59 kontrollerede projektfiler: 38 Python-moduler, 10 testfiler, projekt- og Flatpak-opsætningen, seks versionsnoter og tre skrivebordsressourcer. De blev sammenlignet fil for fil. De lokale udgaver af startfilen, fejlfindingskoden og den ældre statuslagring er med. Genererede byggefiler, virtuelle miljøer, lokale Qt Creator-indstillinger og private sikkerhedskopier hører ikke til i kildepakken.

## Hastighed

Hver test bruger 576 korte lydbogsfiler, lige mange MP3 og M4B, fordelt på 24 serier og 12 forfattere. Først scannes hele testsamlingen. Derefter måles fem komplette, uændrede genscanninger fra disk, inklusive åbning og validering af indekset. Tabellen viser medianen i samme Linux-miljø, med Python 3.12, PySide6 6.10.2 og Mutagen 1.47.0.

| Testsamling | Før | Efter | Læsninger af undermapper før → efter | Omskrivninger af scanindeks pr. scanning |
| --- | ---: | ---: | ---: | ---: |
| Uden omslag | 0,4167 s | 0,0362 s | 3.492 → 60 | 1 → 0 |
| Med indlejrede omslag, mappeomslag og separate bogomslag | 0,4399 s | 0,0427 s | 2.724 → 60 | 1 → 0 |

Begge udgaver genbrugte metadata for alle 576 bøger hver gang. Hver række bruger lokalt testlager; tallene er ikke et løfte om samme tid på en ekstern disk. De måler genscanning, ikke første scanning, tegning af bogkort eller et helt Flatpak-build. [Alle målinger](benchmark.json); testen kan gentages med `tools/bench_scan.py --art none` eller `--art mixed` fra rodmappen med `PYTHONPATH=.`.

## Hvad der blev ændret

- **Scanning og omslag:** En seriemappes billednavne kan genbruges under samme scanning. Prioriteten mellem bogomslag og mappeomslag, store/små bogstaver og kontrol af ændrede billedfiler bevares. Et forsvundet billednavn udløser et nyt opslag. En senere Rescan starter igen med friske mappenavne. Når indeksindhold og filsignatur er uændrede, springes skrivningen over. Et slettet, erstattet eller gammelt indeks bliver skrevet igen.
- **Serier og gallerier:** Nye serietotaler gemmes samlet efter en gennemført scanning. En manuelt angivet total har forrang. Sortering efter færdigstatus, antal eller fremskridt genbruger samlingens oplysninger uden at læse ændringsdatoer fra alle bøger. Gitteret får kun nyt layout, når størrelsen faktisk skifter.
- **Personlige data:** Gallerioplysninger hentes i én samlet læsetransaktion. Databasen har et indeks til opslag på filsti og lukker forbindelser ved afslutning. En skemaopgradering gemmer kolonner og versionsnummer sammen eller ruller begge dele tilbage. Eksisterende sikkerhedskopier og personlige bogdata bevares.
- **Filnavne og identitet:** Et nul i titel eller forfatternavn forsvinder ikke længere ved automatisk omdøbning. Andre bøgers omslagsnavne reserveres også, når omdøbning af omslag er fravalgt. Udskiftning af en bog med en fil af samme størrelse og ændringstid genbruger ikke en anden bogs midlertidigt gemte ID. Statistikken kan stadig tælle det gamle lyt uden at tilskrive den nye bog en afslutning.
- **Kode og pakning:** De forskellige JSON-lagre bruger den samme atomiske skrivning. Dubleret sorteringskode og ubrugte opslag er ryddet op. Fejlloggen roterer (2 MiB, op til tre tidligere logfiler). Python-pakken angiver sin Qt-afhængighed, og Qt Creator-projektet henviser til de rigtige kildefiler. Flatpak bruger afgrænsede kilder og installerer genvej, ikon og appmetadata under det aktuelle app-ID.

## Verifikation

203 tests bestod, heraf samtlige 187 tidligere tests og 16 nye testtilfælde. De omfatter ægte MP3- og M4B-filer, bevarelse af lyd og kapitler, omslag, decimalratings, biblioteker, lyttehistorik, sortering, indeks, sikkerhedskopi og rollback. Der blev kun brugt testdata.

Python-pakken blev desuden bygget, installeret i en midlertidig mappe og startet via dens launcher. Galleriet og Statistics åbnede og lukkede i Qt. Flatpak-modulets lokale kilder, installationskommandoer, genvej, ikon og AppStream-ID blev kontrolleret. Det fulde Flatpak-build og hastigheden på din faktiske samling er ikke testet i dette miljø.
