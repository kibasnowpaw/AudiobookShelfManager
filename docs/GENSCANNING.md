# Genscanning i build 20261120.scan.2

Denne udgave bruger én mappelæsning til at finde både lydbøger og omslagsnavne. Når filens størrelse og tidsstempler stadig passer til indekset, og dens permanente bog-ID allerede er registreret, genbruger scanneren den kontrol. Hver ny scanning kontrollerer stadig lydfilen og de relevante eksterne billeders aktuelle stempler. Et nyt eller ændret omslag kontrolleres igen efter læsning, før resultatet kan genbruges. Eksisterende v5-indeks og omslagskontroller er kompatible.

En lokal måling brugte 605 syntetiske bøger (603 MP3, 2 M4B), 230 serier, indlejrede billeder og en blanding af mappe- og bogomslag. De syv varme genscanninger kørte på testmaskinens lokale filsystem uden Flatpak. Begge udgaver genbrugte alle 605 poster og skrev aldrig indekset om.

| Pr. varm scanning | 20261120.diagnostics.1 | 20261120.scan.2 |
| --- | ---: | ---: |
| Mappelæsninger under biblioteksroden | 575 | 345 |
| Observerede Python-filstempelopslag under roden | 3.624 | 1.966 |
| Median for testscanningen | 62,1 ms | 43,0 ms |

Målingen tæller kald fra Python og er ikke en optælling af alle kernelkald. Testen åbner et lokalt indeks og kører scanningsarbejderen uden en lyttehistorikdatabase eller tegning af brugerfladen. Den forudsiger ikke scannetiden på en ekstern disk eller via Flatpak. Kør fra projektmappen med testafhængighederne installeret:

```bash
PYTHONPATH=. python tools/bench_scan.py --series 230 --total-books 605 --m4b-count 2 --art mixed --rounds 7
```

For at sammenligne på din egen maskine: kør **Rescan** uden at ændre samlingen, vent på at den er færdig, og vælg **Settings → Diagnostics… → Save report…**. Et langsomt bogopslag kræver, at bogen også åbnes, før rapporten gemmes.
