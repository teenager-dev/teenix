# Teenix per Android TV

L'APK include l'interfaccia e il backend Python. All'avvio l'app avvia il server
sulla TV stessa e apre automaticamente il catalogo: non serve un PC, un server
LAN o un hosting. La connessione Internet resta necessaria per cataloghi,
metadati e sorgenti video.

## Installazione

1. Genera l'APK con `build apk.bat` oppure apri questa cartella in Android Studio
   e crea la variante debug.
2. Copia `Teenix-AndroidTV.apk` sulla TV e installalo con un installer APK o ADB.
3. Apri Teenix: il backend locale parte automaticamente.

Sono supportate le ABI ARM a 64 e 32 bit. Il requisito minimo è Android 7.0
(API 24).

## Aggiornamenti

All'avvio l'app controlla l'ultima release pubblica di
`teenager-dev/teenix`. Se trova una versione più recente con l'asset
`Teenix-AndroidTV.apk`, scarica l'APK e apre l'installazione di sistema:
Android/Google TV richiede sempre la conferma finale dell'utente. L'APK della
release deve avere lo stesso certificato di firma delle versioni già installate.

La versione Windows usa Electron updater: `npm run release:win` crea e pubblica
su GitHub l'installer `Teenix-Setup-<versione>.exe`, il relativo `.blockmap` e
`latest.yml`. Richiede `GH_TOKEN` con permessi di scrittura sul repository.
Allega `Teenix-AndroidTV.apk` alla stessa release pubblica; il controllo Android
non usa token incorporati nell'app.

## Uso con telecomando

Usa le frecce per spostare il focus tra navigazione, pulsanti e copertine;
premi OK/Seleziona per aprire l'elemento evidenziato. Nelle raccolte, le frecce
orizzontali scorrono anche i titoli fuori schermo. La ricerca include un
tastierino utilizzabile con il D-pad.

## Limiti

La risoluzione video su Android usa percorsi HTTP; le sorgenti che richiedono un
browser automatizzato non sono disponibili nell'app TV. Non è incluso alcun file
`.env` o segreto: le funzioni che richiedono una chiave OpenRouter necessitano
di una configurazione separata. Il comportamento va verificato anche su una TV
reale, perché WebView e codec multimediali variano tra produttori.

## Requisiti per compilare

- JDK 17 o successivo
- Android SDK Platform 36 e Build Tools 36.0.0
- Gradle 9.6.1 o successivo
- Python 3.11 sul computer di build per Chaquopy

La prima compilazione scarica il runtime Python e le dipendenze; richiede una
connessione Internet.
