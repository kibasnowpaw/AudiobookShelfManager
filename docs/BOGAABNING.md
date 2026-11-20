# Åbning af bøger i build 20261120.open.3

Når du åbner en tidligere registreret bog, læser appen rating, favorit, køb og lytninger i samme SQLite-transaktion. Den ændrer kun databasen, når den registrerer bogen eller skal opdatere oplysninger, f.eks. efter omdøbning, udskiftning eller en ny kendt varighed. Lytninger læses fra databasen, også hvis de er ændret siden sidst.

Når lydfilen, mappen og de relevante billedfiler er uændrede, genbruges den tidligere omslagskontrol. Appen læser stadig den aktuelle billedfil til visning. Hvis billedet er indlejret, kan samme læsning levere bog-ID og anmeldelsesmarkeringer. Ændret omslag kontrolleres igen; en udskiftning midt i åbningen forsøges læst på ny én gang. Når du gemmer nyt omslag, læser appen stadig det indlejrede billede tilbage og forsøger igen, hvis det ikke stemmer.

## Lokal sammenligning

Testen bruger de samme fire syntetiske bøger og den samme midlertidige database til begge udgaver. Der er to MP3- og to M4B-filer. Alle har et indlejret billede på 1.400 × 1.400 pixels, og halvdelen har også en separat omslagsfil. De fire bøger har hver en rating og en afsluttet lytning. Indekset og operativsystemets filcache er varme; programmets midlertidige tag-cache tømmes før hver åbning. Der er syv runder pr. udgave.

| Pr. bogåbning | 20261120.scan.2 | 20261120.open.3 |
| --- | ---: | ---: |
| Forbindelser til historikdatabasen | 4 | 1 |
| Observerede åbninger af selve lydfilen | 2 | 1 |
| Visuelle fingeraftryk beregnet (gennemsnit) | 1,5 | 0 |
| Median for baggrundsarbejderen (28 åbninger pr. udgave) | 40,15 ms | 14,78 ms |

Optællingerne gælder Python-kald i baggrundsarbejderen. Testen omfatter ikke tegning, Flatpak-dokumentportalen eller brugerens egen disk. Kør fra projektmappen med testafhængighederne og ffmpeg installeret:

```bash
PYTHONPATH=. python tools/bench_book_open.py --rounds 7
# Valgfrit: kør begge udgaver på præcis samme sæt filer
PYTHONPATH=. python tools/bench_book_open.py --rounds 7 --compare /sti/til/tidligere/AudiobookShelfManager
```

På din computer kan du sammenligne ved at åbne de samme to eller tre bøger efter en genstart og bruge **Settings → Diagnostics → Save report…**. Sammenhold `book.open` og `book.render`; et eventuelt skift af bog eller omslagstype kan påvirke resultatet.
