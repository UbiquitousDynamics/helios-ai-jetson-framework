# Memoria della conversazione — Voice Test Suite

## Obiettivo

Costruire una suite automatizzata end-to-end per le conversazioni vocali di Helios/Emilia, usando il percorso Piper già presente nel progetto e distinguendo chiaramente:

- test locali deterministici per la logica;
- test hardware-in-the-loop (HIL) per il percorso acustico PC → Emilia → PC;
- metriche, soglie, artefatti e report riproducibili;
- stati `passed`, `failed`, `blocked` e `not_applicable` senza trasformare prerequisiti mancanti in falsi successi.

La richiesta impone di lavorare su un solo test task per volta, aggiornare il checkpoint e fermarsi prima del task successivo.

## Vincoli concordati

- Non modificare il codice di produzione per rendere i test verdi.
- Non creare speech falso con WAV arbitrari quando si valida il riconoscimento: usare Piper.
- Non selezionare mai implicitamente dispositivi audio predefiniti.
- Per HIL servono identificativi espliciti di input/output PC ed Emilia, calibrazione e isolamento acustico.
- Usare solo frasi generate e fixture sintetiche; mai registrare parlato reale.
- Conservare audio/transcript solo in una directory temporanea esplicita e ignorata.
- Non riportare credenziali, token, transcript sensibili o dati utente negli artefatti.
- Usare clock monotoni e correlation ID condivisi; non sottrarre clock di host diversi senza sincronizzazione calibrata.
- Un test HIL passa solo con evidenza acustica reale; un controllo dei prerequisiti non equivale a un pass.

## Task 00 — Baseline e tracciabilità

Task 00 è stato definito come documentazione/specification-only. Non esegue runtime audio.

Artefatti creati:

- `tests/voice_suite/fixtures.json`: catalogo v1.0.0 con 46 fixture, 23 italiane e 23 inglesi, categorie normal/pause-correction/barge-in/control/task-steering/noise/failure, inclusi i sette controlli locali per lingua.
- `tests/voice_suite/metrics.json`: 52 metriche e 38 eventi temporali, con unità, sorgente, sample unit, aggregazione e clock domain.
- `tests/voice_suite/thresholds.json`: profili desktop e Jetson v1.0.0. Il limite provvisorio di 5000 ms p95 per il first-audio è solo una soglia pianificata, non una calibrazione Emilia.
- `tests/voice_suite/hardware.example.json`: manifest di esempio con quattro ruoli audio, dispositivi non selezionati, calibrazione non verificata, isolamento e limiti temporali.
- `tests/voice_suite/traceability.json`: mappatura FR-01–FR-45 con testo, sorgente, task previsti e stato dell’evidenza.
- `scripts/voice_test_suite.py`: validatore locale fail-closed per specifiche, metriche, soglie, hash, tracciabilità e artefatti. Dichiara esplicitamente che non apre dispositivi audio.
- `tests/test_voice_suite_baseline.py`: test deterministici, inclusi casi negativi e prevenzione dei falsi pass.

Risultato finale Task 00:

- Stato: `passed`.
- Test focalizzati: 51 passati.
- Regression gate rilevante: 227 passati, inclusi i 51 focalizzati.
- Sintassi, Ruff e `git diff --check`: passati.
- HIL: `not_applicable`, perché il task è solo documentazione/specification-only.
- HIL deferito al primo task che esegue realmente audio.
- Nessun audio generato, riprodotto o registrato.
- Task successivo: 01.

La disposizione è stata corretta esplicitamente: il vecchio guard HIL che usciva con codice 2 è conservato come osservazione storica, ma non è un gate applicabile a Task 00.

Checkpoint: [docs/voice-test-suite-progress.md](voice-test-suite-progress.md).

## Task 01 — Conversation events e floor states

Task 01 è stato avviato. La logica locale è stata verificata con segnali tipizzati, ma il requisito HIL acustico è reale e non può essere soddisfatto dal solo runner dei prerequisiti.

Artefatti creati:

- `tests/voice_suite/task01-transitions.json`: contratto indipendente v1.0.0 con 12 contesti e 19 eventi, per 228 celle totali: 136 transizioni legali e 92 illegali. Include i tre possibili `candidate_return_state` della modalità barge-in.
- `tests/voice_suite/task01-profile.json`: profili locali desktop/Jetson con 228 casi obbligatori e invarianti a massimo zero errori.
- `tests/test_voice_suite_floor_states.py`: test della matrice, duplicati, transizioni illegali, recovery, sospensione/terminazione, candidati di interruzione, snapshot immutabili, concorrenza e cleanup.
- `tests/test_voice_suite_events.py`: test di correlation ID, eventi tardivi, response ID, failure recovery, trace bounded, clock monotono, manifest e controllo HIL.
- `scripts/voice_suite_task01.py`: runner locale della matrice e report dei prerequisiti HIL. Produce `cases.jsonl`, `events.jsonl` e `results.json`; non apre dispositivi.
- `docs/voice-test-suite/task01-report.md`: report dettagliato.
- `docs/voice-test-suite/task01-results.json`: evidenza JSON sanitizzata.

Validazione locale finale Task 01:

- Test focalizzati: 603 passati.
- Regression gate rilevante: 1177 passati, inclusi i 603 focalizzati.
- Sintassi e Ruff: passati.
- Matrice locale: 228/228 casi passati, 0 errori di verdict, stato, revision, mutazione o identity snapshot.
- Due profili CLI eseguiti: desktop e Jetson-profile-on-PC.
- Entrambi hanno prodotto `local_matrix=passed` e `hil=blocked`, exit code 2.
- 456 record locali correlati verificati tra `cases.jsonl` ed `events.jsonl`.
- Nessun dispositivo aperto, nessun audio generato/registrato, nessun transcript reale.

Prerequisiti HIL attualmente mancanti:

- device ID espliciti per PC input/output ed Emilia input/output;
- manifest di calibrazione verificato e legato ai dispositivi;
- conferma di speech sintetico-only, cuffie/speaker isolati e loopback/monitoring disabilitati;
- Piper, Vosk, sounddevice, PyAudio e ONNX Runtime nel dedicated test environment selezionato;
- adapter acustico PC → Emilia → PC realmente eseguibile;
- mapping degli eventi osservati verso il floor controller con correlation ID;
- misure di latenza, state/event mapping, runaway-level abort e cleanup.

Il manifest di esempio non deve essere riempito inventando dispositivi o calibrazione. Anche un manifest compilato manualmente non è sufficiente a dichiarare la calibrazione verificata.

Stato corrente Task 01: `blocked` sul gate HIL, con gate locale passato. Non avanzare a Task 02 finché il gate HIL non è completato oppure esplicitamente disposto come bloccato con evidenza.

## Ambiente e risultati rilevanti

Interpreter usato nei gate locali:

```powershell
$taskPython = Join-Path $env:TEMP 'helios-live-conversation-task00-20260918/Scripts/python.exe'
```

Versioni PC osservate: Python 3.12.10, pytest 8.4.2, numpy 2.5.3, httpx 0.28.1, Ruff 0.16.8. Nel dedicated interpreter usato per i test locali mancavano Piper, Vosk, sounddevice, PyAudio e ONNX Runtime.

Il progetto contiene già i modelli Piper italiani/inglesi a 22050 Hz, il percorso `audio.tts.PiperTTS.synthesize_wave`, i modelli Vosk e diagnostica audio, ma questi asset non costituiscono da soli evidenza HIL.

## Regola storica per il completamento HIL di Task 01

Questa regola era il punto di arresto dopo Task 01; la successiva richiesta dell'utente ha autorizzato il lavoro locale sul task numerato successivo. Per completare l'HIL di Task 01, prima di eseguire qualsiasi audio reale:

1. acquisire identificativi espliciti e verificabili dei quattro dispositivi;
2. creare e verificare l’artefatto di calibrazione versionato;
3. installare/provisionare solo le dipendenze native necessarie nel dedicated environment;
4. confermare isolamento e assenza di loopback;
5. eseguire una sessione acustica bounded con marker unico e abort su runaway-level/self-trigger;
6. raccogliere metriche acustiche e mappatura degli eventi;
7. eseguire l’intero gate locale e HIL;
8. aggiornare il checkpoint e fermarsi.

Se anche un solo prerequisito o una misura è indisponibile, mantenere Task 01 `blocked`; non fabbricare dati e non segnare HIL come passato.

## Aggiornamento — Task 02

La richiesta successiva era di continuare fino alla fine del task numerato seguente. Task 02 è stato quindi svolto nella parte locale, mentre Task 01 è rimasto `blocked` sul proprio HIL. I nuovi artefatti sono `tests/voice_suite/task02-contract.json`, `tests/voice_suite/task02-profile.json`, `tests/test_voice_suite_authority.py`, `tests/test_voice_suite_task02_runner.py`, `scripts/voice_suite_task02.py`, `docs/voice-test-suite/task02-report.md` e `docs/voice-test-suite/task02-results.json`.

Il contratto Task 02 ha 10 scenari italiani/inglesi e 36 eventi tipizzati: 10 provvisori, 14 finali autorevoli e 12 ignorati. La validazione locale ha passato 29 test focalizzati e 2043 test nel gate locale completo; quest'ultimo ha un test saltato per i privilegi Windows sui symlink e un test `remote_live` escluso. Entrambi i profili del runner hanno passato gli invarianti locali ma sono usciti con codice 2 perché HIL è `blocked`.

Su PC è stato installato soltanto `sounddevice` nel dedicated environment per enumerare i dispositivi. Sono stati letti 25 endpoint PortAudio. Emilia è stata interrogata in sola lettura: due enumerazioni hanno restituito 13 e 24 endpoint, con indici cambiati. Nessuno dei quattro ruoli fisici è stato selezionato; non sono verificati isolamento, calibrazione né percorso acustico. Sono stati eseguiti zero scambi e aperti zero stream. Task 02 resta `blocked` sul proprio HIL; Task 03 è il successivo e non è stato avviato. Il checkpoint e il report Task 02 sono le fonti aggiornate dei dettagli.
