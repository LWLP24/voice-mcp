# CallTool Roadmap

Diese Roadmap leitet sich aus dem technischen Plan in
[`projects-v0.1.md`](projects-v0.1.md) ab. Sie beschreibt die Reihenfolge der
Entwicklung und die Architekturgrenzen für die kommenden Versionen.

Der aktuelle Entwicklungszweig ist `feature/initial`. Development-Releases
werden als `v0.1.1-dev.N` veröffentlicht. Ein stabiler Release wird nur nach
erfüllter Definition of Done und erfolgreicher Infrastrukturprüfung markiert.

## Leitprinzip

CallTool ist die Policy-, State- und Persistence-Schicht. LiveKit übernimmt
möglichst viele Echtzeit-, Medien- und Telefonieprimitive.

```text
AI-Agent / MCP / REST
          │
          ▼
CallTool: Auftrag, Policy, Tools, State, Outcome, Historie
          │
          ▼
LiveKit: Audio, Turn Detection, Barge-in, SIP, DTMF, Transfer
          │
          ▼
Telnyx: PSTN-Anbindung
```

Die Grenze bleibt bewusst stabil:

- LiveKit darf den Voice-Hot-Path und SIP-Mechaniken optimieren.
- CallTool entscheidet über Berechtigungen, verbindliche Aktionen und
  Geschäftslogik.
- `ActiveCallContext` ist der schnelle Laufzeit-State im Worker.
- PostgreSQL bleibt die dauerhafte Quelle für Calls, Events, Transkripte und
  Outcomes.
- Redis bleibt Infrastruktur für LiveKit/Kommunikation und optionale
  Koordination, nicht die Source of Truth.

## Versionierungsregeln

| Version | Zweck |
| --- | --- |
| `v0.1.0` | initiale technische Basis |
| `v0.1.1-dev.N` | aktuelle Integrations- und Stabilitätsentwicklung |
| `v0.1.1` | stabiler v0.1-Funktionsstand |
| `v0.2.x` | erweiterte Telefonie, Analytics und optionale Nebenpfade |
| `v1.0.x` | produktionsreife Skalierung und Provider-Resilienz |
| `v1.x` | zusätzliche Provider, Kanäle und Betriebsfunktionen |
| `v2.x` | größere Agenten-, Mandanten- und Automatisierungsfunktionen |

Jeder Push auf den Entwicklungszweig durchläuft die CI. Nach erfolgreicher CI
legt der Workflow den nächsten Development-Tag an und baut das Image. Stabile
Tags werden weiterhin manuell beziehungsweise bewusst erstellt. Ein echter
PSTN-Call läuft nicht auf jedem Push, sondern in einem separaten manuellen oder
Nightly-Test.

## v0.1.0 — technische Basis

### Ziel

Ein einzelner AI-Agent kann über MCP oder REST einen Telefonauftrag starten,
den CallTool über Telnyx und LiveKit ausführt und anschließend ein strukturiertes
Ergebnis liefert.

### Bestandteile

- Python-Worker und API-Prozess im gemeinsamen Docker-Image
- PostgreSQL, Redis, LiveKit und LiveKit SIP per Compose
- Telnyx-SIP-Trunk für deutsche Rufnummern
- Outbound- und Inbound-Call-Flows
- Gemini Realtime als Standard sowie OpenAI Realtime als konfigurierbare
  Alternative
- konfigurierbare Sprache, Stimme und Prompt-Profile aus Dateien
- `ActiveCallContext` im RAM mit asynchroner Persistenz
- PostgreSQL-Call-Historie für inbound und outbound
- MCP- und REST-Methoden für Erstellen, Status, Auflisten, Konversation,
  Antworten und Abbrechen
- Policy Engine mit Fakten, Kandidaten und Commit-Autorisierung
- Human-in-the-loop über `input_required` und `respond`
- strukturierte Outcomes, Transcript-Turns und Call-Events
- Idempotency, Fehler-Mapping, Graceful Shutdown und Recovery-Grundlagen
- Health-Endpunkte, `calltool doctor`, strukturierte Logs und CI/Docker-Build

### Abnahmekriterium

Die technische Basis kann einen echten kurzen Testcall führen, ohne die
Gesprächsqualität durch API- oder Datenbankzugriffe im Voice-Hot-Path zu
blockieren.

## v0.1.1 — LiveKit-first Voice und v0.1-Abschluss

Dies ist die aktuelle Entwicklungsreihe `v0.1.1-dev.N`.

Implementierungsstatus für den Feature-Kandidaten: Die nachfolgenden sieben
Softwarepfade sind im Code, in der Konfiguration und in automatisierten Tests
umgesetzt. Offen sind die ausdrücklich externen Abnahmen mit echten Telnyx-
Anrufen, insbesondere der 20-Minuten-Soak, reale IVR-/Mailbox-Szenarien und die
Freischaltung beziehungsweise Bestätigung von SIP REFER. Diese Ergebnisse sind
das Test- und Fix-Ziel ab `v0.1.1-dev.4`.

### 1. Native Turn Detection als Testpfad

- Konfigurationsschalter für `realtime_llm` und LiveKit
  `TurnDetector(version="v1-mini")`
- `v1-mini` zunächst lokal und CPU-bewusst testen; deutsche Thresholds über
  `unlikely_threshold` und `backchannel_threshold` anhand realer Evals kalibrieren
- Vergleichskorpus mit kurzen Antworten, Pausen, Backchannels und
  Unterbrechungen
- Messung von End-of-Turn, verspäteten Antworten, False Interruptions und
  Barge-in-Latenz
- nur eine Strategie je Session aktivieren; kein paralleler Shadow-STT allein
  zur Turn Detection

Die Realtime-native Erkennung bleibt der Default, bis reale Telefonmessungen
zeigen, dass der LiveKit-Detektor besser geeignet ist.

**Code-Status:** umgesetzt. Der lokale Pfad deaktiviert die provider-native
Endpointing-Strategie für genau diese Session und fügt den lokalen LiveKit-VAD
sowie `TurnDetector(version="v1-mini")` hinzu. Modus, Thresholds und Session-
Report werden im Call State gespeichert.

### 2. LiveKit-native Interruption und Barge-in

- AgentSession-Interruption-Primitives verwenden und konfigurieren
- `vad` als einzige Self-Hosted-Interruption-Strategie explizit konfigurieren
- das in LiveKit Agents 1.7.1 verfügbare `overlapping_speech` sowie
  False-Interruption-Events auswerten und intern als
  `call.user_interruption_detected` abbilden
- Barge-in-Stop-Latenz weiter als CallTool-Metrik messen
- keine eigene Audio- oder Endpointing-Implementierung bauen
- Watchdog ausschließlich für Hänger, Stille und Recovery verwenden

**Code-Status:** umgesetzt. `vad` ist die einzige Interruption-Strategie im
Self-Hosted-Build. LiveKit übernimmt die Erkennung und das Stoppen der Ausgabe;
CallTool misst die nativen Events und die Barge-in-Latenz.

### 3. DTMF und IVR

- `send_dtmf` als CallTool-Policy-Tool behalten
- DTMF-Versand und -Empfang ausschließlich über LiveKit abwickeln
- `ivr_detection` optional aktivierbar machen
- LiveKit-Mechanik mit realen IVR-Testfällen prüfen
- erlaubte Ziffern, Menüstrategie, Timeout und Audit im CallTool kontrollieren

**Code-Status:** umgesetzt. Senden und Empfangen verwenden LiveKit; Allowlist,
Aktionslänge, Inter-Digit-Delay, Navigationstimeout und optionales Raw-Digit-
Audit liegen bei CallTool.

### 4. AMD und Voicemail

- LiveKit AMD optional und zunächst für Outbound testen
- Zustände `human`, `machine-ivr`, `machine-vm`, `machine-unavailable` und `uncertain`
  in `ActiveCallContext`, Events und Outcome abbilden
- prüfen, ob der vorhandene Realtime-Transcript plus kleiner provider-nativer
  Text-Classifier für das Self-Hosted-Realtime-Setup ausreicht
- Policy für Mailbox definieren: auflegen, Nachricht hinterlassen oder
  menschliche Entscheidung anfordern
- keine eigene Audio- oder Mailbox-Erkennung bauen

**Code-Status:** umgesetzt. Self-Hosted AMD verwendet LiveKit AMD, den
vorhandenen Realtime-Input-Transcript und einen kleinen Text-Classifier beim
gewählten Gemini-/OpenAI-Provider. Ein zweiter STT-Stream oder zusätzlicher
Inference-Dienst ist dafür nicht erforderlich. Die Policies `hangup`, `continue`,
`leave_message` und `request_user` sind konfigurierbar.

### 5. Native LiveKit-Metriken

- Plugin-Metriken für STT, LLM, TTS und VAD einsammeln, wo vorhanden
- per-Turn-Latenzen und Session Usage aus den LiveKit-Hooks übernehmen
- Session Report beim Call-Ende erzeugen
- eigene CallTool-Metriken nur für Lifecycle, Policy, Tools, Persistenz,
  Recovery und Geschäftsresultate behalten
- keine synchronen Rohmetriken in PostgreSQL schreiben

**Code-Status:** umgesetzt. Native Plugin- und Turn-Metriken werden in
Prometheus aggregiert; Session Usage und ein kompakter finaler Session Report
werden im dauerhaften Voice-Session-State abgelegt.

### 6. Cold Transfer vorbereiten

- Transfer-Tool und Policy-Schnittstelle definieren
- Zielnummer validieren und Transfer-Berechtigung prüfen
- LiveKit SIP Transfer API beziehungsweise SIP REFER verwenden
- Telnyx-REFER-Freischaltung und Fehlerfälle testen
- Transfer-Zustand, Ziel, Timeout und Ergebnis dauerhaft speichern

**Code-Status:** umgesetzt und mit simulierter LiveKit-Antwort getestet. Der
Pfad ist standardmäßig deaktiviert, benötigt `may_transfer=true` und muss noch
gegen einen Telnyx-Account mit erlaubtem SIP REFER real verifiziert werden.

Cold Transfer ist in v0.1.1 zunächst ein kontrollierter Testpfad. Warm Transfer
und ein vollständiges Beratungsgespräch bleiben v0.2 beziehungsweise
experimentell, weil der Python-Workflow aktuell zusätzliche Komplexität und
Beta-Abhängigkeiten mitbringt.

### 7. Self-Hosted Noise-Strategie

- Krisp nicht als Voraussetzung behandeln
- `krisp_enabled` nicht standardmäßig für die Self-Hosted-SIP-Installation
  aktivieren
- Sprachqualität und Turn Detection zunächst ohne Krisp messen
- keine proprietäre Noise-Processing-Abhängigkeit für die v0.1-Baseline

**Code-Status:** umgesetzt. `krisp_enabled=false` ist Default und wird explizit
an den LiveKit-SIP-Participant-Request übergeben.

### v0.1.1-Abnahme

- [ ] Outbound und Inbound funktionieren im aktuellen Kandidaten real mit Telnyx
  und deutscher DID.
- [ ] Ein realer 20-Minuten-Call funktioniert inklusive Transcript und Outcome.
- [ ] Barge-in, False Interruption und Watchdog-Recovery sind in realen Calls
  gemessen.
- [x] Turn Detection kann per Env/YAML umgeschaltet und verglichen werden.
- [x] DTMF, IVR und AMD sind opt-in testbar und policy-kontrolliert.
- [x] Native LiveKit-Metriken und CallTool-Metriken sind getrennt sichtbar.
- [x] Call-Historie enthält Richtung, Zeitstempel, Events, Transcript und Outcome.
- [x] MCP, REST, Idempotency, Human-in-the-loop und Cancel sind implementiert und
  automatisiert getestet.
- [x] Lint, strikte Typprüfung, Unit-/API-/MCP- und Konfigurations-Checks sind
  lokal grün; PostgreSQL-Integration und Docker-Build bleiben Teil des CI-Gates.

## v0.2.x — Telefonie- und Analyseausbau

### Ziel

Die v0.1-Basis wird um robuste Telefonie-Funktionen und optionale Analysepfade
erweitert, ohne den nativen Voice-Hot-Path unnötig zu verlängern.

### Telefonie

- AMD-Ergebnisse in produktiven Outbound-Flows verwenden
- konfigurierbare Voicemail-Aktionen und sichere Nachrichtenerstellung
- IVR-Navigation mit robusten Timeouts, Wiederholungen und Abbruchregeln
- Cold Transfer produktionsreif machen
- Warm Transfer als explizit experimentelle Funktion integrieren
- Transfer-Rückkehr, Ziel nicht erreichbar und Timeout sauber behandeln
- Retry-Scheduler für sichere, nicht verbindliche Fehlerfälle
- bessere Provider- und SIP-Fehlerdiagnose

### Transcript und Analytics

- Gemini Transcribe Live als optionaler Shadow-STT für Interim-Transkripte,
  Captions, Custom Vocabulary und tiefere Call Analytics
- Session-Rollover für Shadow-STT bei langen Gesprächen
- Supervisor Cache und effizientere Outcome-Enrichment-Aufträge
- Live-Dashboard für Voice-, Tool-, SIP- und Session-Metriken
- Analytics-Events dürfen bei Backpressure gedroppt werden; Commit-, Outcome-
  und Call-Lifecycle-Events nicht

Shadow-STT bleibt ein Nebenpfad. Er wird nicht als Voraussetzung für
Turn Detection, Barge-in oder normale Gesprächsführung verwendet.

### Provider und UX

- systematische Gemini-/OpenAI-Realtime-Evaluation mit identischen Szenarien
- Modell- und Voice-Upgradeprozess mit festgeschriebenen IDs
- bessere Sprach- und Stimmprofile je Auftrag
- robuste Zahlen-, Datums- und Uhrzeit-Evals
- weitere Prompt-Profile für Inbound, Outbound, Mailbox und IVR

## v0.3.x — Resilienz und kontrollierte Fallbacks

### Ziel

Ausfälle einzelner Modell- oder Infrastrukturkomponenten werden kontrolliert
behandelt, ohne unsichere Zusagen zu erzeugen.

### Bestandteile

- vollständige Model-Failure-Recovery mit Session Resumption und kompakter
  State-Wiederaufnahme
- getesteter Fallback von Realtime auf STT → LLM → TTS für definierte Szenarien
- Fallback-Policy je Provider und Auftrag
- saubere Übergabe zwischen LiveKit Session, Call State und Fallback-Modus
- Webhooks für `input_required`, `completed` und `failed` mit HMAC-Signatur
- bessere Trace-Korrelation von Call, Room, SIP und Modell-Session
- Audio Recording als separater, opt-in Recorder außerhalb des Hot-Paths
- Datenschutz-, Retention- und Löschkonfiguration für Transkripte und Aufnahmen

Der Fallback darf nicht eigenständig verbindliche Fakten oder Commitments
erfinden. Die Policy Engine und der persistierte State bleiben autoritativ.

## v1.0.x — produktionsreife Skalierung

### Ziel

CallTool ist für dauerhaften Betrieb mit mehreren parallelen Calls und
kontrollierter horizontaler Skalierung geeignet.

### Bestandteile

- Long-Call-Soak-Tests und stabile Session-Resumption
- horizontale Worker und verteilte Concurrency-Kontrolle
- Queueing, Rate Limits und belastbare Backpressure-Strategie
- mehrere parallele Rooms und definierte Ressourcenbudgets
- fortgeschrittener Retry-Scheduler mit Idempotency
- Provider-Failover und getrennte Provider-Gesundheitschecks
- mehrere SIP-Provider neben Telnyx
- automatisierte Regressionen für Latenz, Barge-in, Speicher und CPU
- belastbare Alerting- und Dashboard-Schwellenwerte
- sichere Secret-Verwaltung, Rotation und getrennte Umgebungen
- reproduzierbare Releases mit SBOM, Provenance und Rollback-Strategie
- dokumentierte SLOs und Error Budgets

## v1.x — Plattform- und Integrationsausbau

- zusätzliche Länder, Rufnummernregeln und Signaling-Regionen
- weitere SIP- und PSTN-Provider
- Provider-Routing nach Kosten, Region, Qualität oder Ausfallstatus
- produktionsreife Warm Transfers und Human Escalation Workflows
- externe Kalender-, CRM- und Ticketsystem-Adapter über sichere Tools
- Mandantenfähigkeit, Quotas und getrennte Datenzugriffe
- rollenbasierte Administration und feinere Tool-Berechtigungen
- verwaltete Prompt-, Voice- und Policy-Versionen
- Auswertungen über mehrere Calls und Kontakte
- Audit-Export und revisionssichere Ereignisaufbewahrung

## v2.x — größere Agentenfunktionen

Diese Funktionen werden erst nach stabiler v1-Basis bewertet:

- Multi-Agent-Orchestrierung und spezialisierte Voice-Agenten
- kontrollierte Agentenübergaben innerhalb eines Calls
- komplexe mehrstufige IVR- und Workflow-Automatisierung
- pro Kontakt persistenter, ausdrücklich freigegebener Memory-Layer
- proaktive Rückrufe und Termin-/Task-Scheduler
- mehrere parallele Gesprächsteilnehmer und Konferenzszenarien
- fortgeschrittene Qualitätsbewertung und automatische Call-Reviews
- plattformübergreifende Voice- und Messaging-Kanäle

## Querschnittliche offene Punkte

Die folgenden Punkte begleiten mehrere Releases und sind nicht an eine einzelne
Feature-Version gebunden:

### Voice und Modell

- reale Latenz-Baselines für Gemini Realtime und GPT Realtime
- endgültige Turn-Detection-Auswahl anhand von Telefonmessungen
- Verhalten bei Provider-Reconnects, GoAway und Context Compression
- Stimmen, Sprachen, Disclosure und Prompt-Profile pro Call
- Grenzen von Realtime-Input-/Output-Transkripten dokumentieren

### Telefonie

- Telnyx-Produktionsfreigabe, deutsche DID-Anforderungen und Rufnummern-
  Validierung
- E.164-Normalisierung sowie Premium-/Notruf-Block
- SIP-Fehlercodes und Provider-Ursachen vollständig abbilden
- SIP-Header über LiveKit Attributes/Mapping dokumentieren und validieren
- AMD-/IVR-/Transfer-Verhalten für Human, Mailbox, IVR, Busy, No Answer und
  Rejected testen
- Audioqualität, Echo, Paketverlust und Noise Processing messen

### State und Daten

- Event-Reihenfolge und Sequenznummern unter Parallelität garantieren
- kritische Events bei Worker Exit flushen
- Transcript-Batches und Retention festlegen
- Outcome darf nur aus State, Tool Events und validierter Analyse entstehen
- PostgreSQL-Migrationen, Backups und Löschkonzept operationalisieren
- Redis-Ausfallverhalten und Wiederanlauf dokumentieren

### API und Sicherheit

- Authentifizierung und Autorisierung für MCP, REST und Webhooks
- Auftragsschema, Timeouts und Fehlerverträge versionieren
- Idempotency über Retries und Worker-Neustarts hinweg testen
- Tool-Policy pro Inbound- und Outbound-Kontext absichern
- Secrets nicht in Logs, Events oder Prompt-Snapshots leaken

### Betrieb und Qualität

- `docker compose up -d`-Setup reproduzierbar halten
- `calltool doctor` um SIP-, LiveKit-, Modell- und Datenbankprüfungen erweitern
- Unit-, Integrations-, Simulations- und echte Call-Szenarien getrennt ausführen
- reale PSTN-Tests manuell/Nightly, nicht bei jedem Push
- Lasttests mit 1, 2, 4 und 8 simulierten Rooms vor horizontaler Skalierung
- p50/p95/p99-Baselines pro Release speichern
- SBOM, Build-Provenance, Image-Signierung und Rollback testen

## Release-Gates

Ein Feature wird erst in eine stabile Version aufgenommen, wenn:

1. der normale Voice-Hot-Path nicht unnötig blockiert wird;
2. die Funktion mit Unit- und Integrations-Tests abgedeckt ist;
3. ein echter Telefonie-Test oder ein begründeter Simulations-Test vorliegt;
4. Fehler-, Timeout- und Recovery-Verhalten definiert ist;
5. State, Events, Outcome und Berechtigungen berücksichtigt sind;
6. Metriken und Logs die Funktion diagnostizierbar machen;
7. die Funktion hinter einem Feature-Schalter deaktivierbar ist, wenn sie noch
   experimentell ist.

## Aktueller nächster Meilenstein

Der Feature-Umfang ist für den `v0.1.1-dev.3`-Kandidaten implementiert. Ab
`v0.1.1-dev.4` gilt folgende Reihenfolge:

1. Real-Call-Testmatrix für Inbound, Outbound, IVR, Mailbox und Transfer
   ausführen.
2. 20-Minuten-Soak mit Transcript, Outcome, Session Usage und Report prüfen.
3. `realtime_llm` gegen `livekit_v1_mini` mit `vad` messen und für Deutsch die
   Turn-Thresholds gemeinsam mit den Qualitätsmetriken kalibrieren.
4. Alle gefundenen Fehler ohne neue v0.1.1-Features beheben.
5. Definition of Done abschließen und anschließend `v0.1.1` bewusst releasen.
