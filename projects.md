# Call tool

## CallTool v3

Universeller, self-hostbarer Telefon-Agent als MCP-/REST-Tool

Optimiert auf natürliche Gespräche, minimale Latenz und aktuelle Google-Voice-Modelle

Stand der technischen Verifikation: 28. August 2026
Architekturstatus: empfohlene Implementierungsbasis
Primäres Ziel: Ein beliebiger AI-Agent soll per MCP oder REST einen Telefonauftrag starten können. Der Telefon-Agent soll sich auf normalem menschlichem Gesprächsniveau anfühlen: geringe Antwortlatenz, saubere Unterbrechungen, keine langen künstlichen Pausen und trotzdem kontrollierbare Aktionen.

## 0. Executive Summary

Die empfohlene Architektur für CallTool ist jetzt:

```text
beliebiger Agent
Hermes / Claude / OpenAI / eigener Agent / n8n
                    │
                    │ MCP oder REST
                    ▼
┌────────────────────────────────────────┐
│              CallTool API              │
│                                        │
│ Auth / Validation                      │
│ CallService                            │
│ MCP                                    │
│ REST                                   │
│ Status / Events / Webhooks             │
└───────────────────┬────────────────────┘
                    │
                    │ durable job
                    ▼
               PostgreSQL
                    │
                    │ LiveKit dispatch
                    ▼
┌────────────────────────────────────────┐
│             CallTool Worker            │
│                                        │
│ LiveKit AgentSession                   │
│ Call State                             │
│ Policy Engine                          │
│ Tools                                  │
│ Realtime Supervisor                    │
│ Outcome Builder                        │
└───────────────────┬────────────────────┘
                    │
                    ▼
                LiveKit
                    │
                LiveKit SIP
                    │
                 Telnyx SIP
                    │
                    ▼
                 Telefon
```

Der normale Gesprächspfad ist bewusst so kurz wie möglich:

```text
Telefon-Audio
     │
     ▼
Gemini 3.1 Flash Live
     │
     ▼
Telefon-Audio
```

Also:

```text
AUDIO → AUDIO
```

und nicht standardmäßig:

```text
AUDIO → STT → LLM → TTS → AUDIO
```

Das spart den größten Teil der vermeidbaren Voice-Latenz.

Parallel beziehungsweise optional:

```text
Gemini 3.5 Transcribe Live
          ↓
Realtime Transcript
          ↓
State / Observability / Supervisor
Gemini 3.7 Flash
          ↓
komplexe Entscheidungen / Outcome / Supervisor
Gemini 3.1 Flash TTS
          ↓
exakte scripted speech / Fallback
```

## 1. Finale Technologieentscheidung

### 1.1 Programmiersprache

Empfehlung: Python 3.13

Nicht Node.js, nicht Go, nicht Rust.

Das ist eine bewusste Änderung gegenüber einer ersten TypeScript-Einschätzung.

Der entscheidende Grund ist nicht rohe Interpreter-Performance, sondern die aktuelle Feature-Parität des Google-/LiveKit-Stacks.

Stand 28.08.2026:

- LiveKit Agents unterstützt Python und Node.
- Gemini 3.1 Flash Live funktioniert in beiden.
- Der aktuelle Python-Google-Realtime-Adapter exponiert:
  - context_window_compression
  - session_resumption
  - Input-/Output-Transcription
  - Realtime Input Config
  - Thinking Config
  - Die aktuelle Node-Referenz exponiert contextWindowCompression, aber aktuell kein entsprechendes sessionResumption-Argument.
  - Gemini-Live-WebSocket-Verbindungen werden regelmäßig beendet; Session Resumption ist deshalb für lange Telefonate relevant.
  - LiveKit isoliert einzelne Agent Jobs ohnehin in eigenen Prozessen. Dadurch spielt der Python-GIL für diesen Workload praktisch keine relevante Rolle.

Für einen Telefon-Agenten, der zehn oder zwanzig Minuten in einer Arzt-Warteschleife landen kann, ist saubere Session-Lifecycle-Unterstützung wichtiger als ein theoretischer Sub-Millisekunden-Vorteil im HTTP/Event-Loop.

**Quellen:**

- <https://docs.livekit.io/agents/models/realtime/plugins/gemini/>
- <https://docs.livekit.io/reference/python/livekit/plugins/google/realtime/realtime_api.html>
- <https://docs.livekit.io/reference/agents-js/classes/plugins_agents_plugin_google.beta.realtime.RealtimeModel.html>
- <https://docs.livekit.io/agents/server/options/>

## 2. Warum Python-Performance hier vollkommen ausreicht

Der Performance-kritische Pfad besteht fast nur aus:

```text
Netzwerk
WebSocket
LiveKit Media
Google Model Inference
Tool Events
kleinen RAM Lookups
```

Nicht aus:

```text
großen CPU-Berechnungen
DSP in Python
Audio-Decoding in Python
RTP-Parsing in Python
ML-Inferenz lokal
```

LiveKit übernimmt:

```text
WebRTC
Audio Transport
SIP Media
Buffering
Barge-in Plumbing
Agent Process Isolation
```

Google übernimmt:

```text
Speech Understanding
Reasoning
Speech Generation
```

CallTool selbst macht im Hot Path ungefähr:

```python
constraint = call_state.constraints.get("earliest_time")
allowed = candidate.time >= constraint
```

Der Unterschied zwischen:

```text
Python: 50 µs
Rust:    5 µs
```

ist irrelevant, wenn ein Modell-/Netzwerkturn mehrere hundert Millisekunden benötigt.

## 3. Sprachvergleich

| Kriterium | Python | TypeScript/Node | Go | Rust |
| ---------------------------------------------------- | ---------------------: | -------------------------------------------: | --------------: | ---------------: |
| LiveKit Agents | sehr gut | sehr gut | nein / Eigenbau | nein / Eigenbau |
| Gemini Live Plugin | sehr gut | gut | Eigenbau | Eigenbau |
| Session Resumption aktuell im LiveKit-Google-Adapter | **ja** | derzeit nicht in aktueller Node-API-Referenz | Eigenbau | Eigenbau |
| MCP SDK | sehr gut | sehr gut | gut | weniger relevant |
| Voice Tooling | **am vollständigsten** | sehr gut | schwach | schwach |
| Entwicklergeschwindigkeit | **hoch** | hoch | mittel | niedrig |
| Raw I/O Performance | gut | sehr gut | sehr gut | sehr gut |
| Unterschied für tatsächliche Voice-Latenz | minimal | minimal | minimal | minimal |
| Empfohlene Wahl heute | **JA** | zweite Wahl | nein | nein |

Entscheidung

```text
Python 3.13
```

für v0.1 und v1.

Wenn der Node-Google-Adapter später volle Feature-Parität erreicht, bleibt eine Portierung möglich, weil API- und Domain-Modelle sprachagnostisch gehalten werden.

## 4. Runtime-Version

Empfohlener Pin:

```text
Python 3.13.15
```

Python 3.14.7 ist aktuell ebenfalls stabil, aber wir brauchen für diesen Workload weder Free-Threading noch neue Interpreter-Features.

LiveKit Agents 1.7.1 unterstützt:

```text
Python >=3.10,<3.15
```

Python 3.13 ist deshalb ein konservativer Produktions-Pin.

**Quellen:**

- <https://www.python.org/downloads/release/python-31315/>
- <https://www.python.org/downloads/release/python-3147/>
- <https://pypi.org/project/livekit-agents/1.7.1/>

## 5. Aktuell verifizierte Kernversionen

Stand 28.08.2026:

| Komponente | Version / Modell |
| --------------------- | ------------------------------- |
| Python | `3.13.15` empfohlen |
| LiveKit Agents | `1.7.1` |
| LiveKit Google Plugin | `1.7.0` |
| MCP Python SDK | `2.1.1` |
| MCP Protokoll | `2026-07-28` |
| LiveKit Server | `1.13.5` |
| Gemini Realtime | `gemini-3.1-flash-live-preview` |
| Gemini STT | `gemini-3.5-transcribe-live` |
| Gemini Workhorse LLM | `gemini-3.7-flash` |
| Gemini TTS | `gemini-3.1-flash-tts-preview` |

**Quellen:**

- <https://pypi.org/project/livekit-agents/1.7.1/>
- <https://pypi.org/project/livekit-plugins-google/1.7.0/>
- <https://pypi.org/project/mcp/2.1.1/>
- <https://github.com/livekit/livekit/releases>
- <https://ai.google.dev/gemini-api/docs/models>
- <https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview>
- <https://ai.google.dev/gemini-api/docs/models/gemini-3.5-transcribe>
- <https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash>
- <https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-tts-preview>

## 6. Primäres Voice-Modell

```text
gemini-3.1-flash-live-preview
```

Google beschreibt es aktuell als:

```text
low-latency audio-to-audio model
optimized for real-time dialogue
```

Es unterstützt:

- Audio Input
- Audio Output
- Text Input
- Function Calling
- Thinking
- Live API
- Input Audio Transcription
- Output Audio Transcription
- Custom VAD / Realtime Input Config

**Quelle:**

- <https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview>

## 7. Warum Native Audio der Hot Path ist

Vergleich:

Cascade

```text
Speech End
   ↓
STT final
   ↓
LLM first token
   ↓
TTS first audio
   ↓
Playback
```

Jede Stufe fügt:

```text
Netzwerklatenz
Queueing
Inference Startup
Streaming Startup
```

hinzu.

Native Gemini Live

```text
Speech
   ↓
Gemini Live
   ↓
Speech
```

Das Modell hört direkt die Sprache und erzeugt direkt Sprache.

LiveKit beschreibt Realtime-Modelle aktuell als Pipeline-Typ mit der niedrigsten End-to-End-Latenz.

**Quelle:**

- <https://docs.livekit.io/agents/models/pipelines/>

## 8. Architekturprinzip: Fast Path und Control Path

CallTool besitzt zwei logisch unterschiedliche Pfade.

### 8.1 Fast Path

Alles, was in einem normalen Gespräch ständig passiert:

```text
User Speech
→ Gemini 3.1 Live
→ lokale Tools
→ Gemini 3.1 Live
→ Agent Speech
```

Nur:

```text
RAM
lokale Policy
lokale State Machine
```

dürfen hier synchron blockieren.

### 8.2 Control / Slow Path

Seltene Vorgänge:

```text
komplexe Analyse
externe API
User-Rückfrage
Outcome-Erzeugung
Background Transcription
```

dürfen separat laufen.

## 9. Zielarchitektur Voice

```text
                    SIP Audio
                       │
                       ▼
                   LiveKit
                       │
                       ▼
              AgentSession
                       │
        ┌──────────────┴──────────────┐
        │                             │
        ▼                             ▼
 Gemini 3.1 Flash Live        optional Shadow STT
       FAST PATH              Gemini 3.5 Transcribe
        │                             │
        │                             ▼
        │                       Transcript Events
        │                             │
        │                      Supervisor / Logs
        │
        ├── Tool Calls
        │      │
        │      ▼
        │  Local State
        │  Policy Engine
        │
        └──────────────→ Speech Output
```

Parallel:

```text
Transcript / Call State
          │
          ▼
 Gemini 3.7 Flash
          │
          ▼
complex analysis
outcome
supervisor cache
```

Separate TTS:

```text
Gemini 3.1 Flash TTS
```

für:

```text
scripted speech
pre-generated greeting
deterministische Bestätigung
Hold-Phrases
Fallback
```

## 10. Wichtig: Gemini 3.1 Live hat aktuelle Einschränkungen

Stand heute dokumentiert LiveKit für:

```text
gemini-3.1-flash-live-preview
```

folgende Einschränkungen:

- generate_reply() nicht kompatibel
- update_instructions() mid-session nicht kompatibel
- update_chat_ctx() mid-session nicht kompatibel
- Agent Handoffs über neue Instructions funktionieren nicht zuverlässig
- asynchrone Function Calls werden von Gemini 3.1 nicht unterstützt
- Function Calls sind blocking/sequenziell

Basic:

```text
voice conversation
tool calling
audio I/O
```

funktioniert.

**Quelle:**

- <https://docs.livekit.io/agents/models/realtime/plugins/gemini/>

Konsequenz

Wir designen um diese Einschränkungen herum, statt dagegen anzukämpfen.

## 11. Keine dynamischen Systemprompt-Updates

Der komplette Auftrag wird beim Session Start eingebettet:

```text
IDENTITY
ROLE
DISCLOSURE
OBJECTIVE
KNOWN FACTS
CONSTRAINTS
PERMISSIONS
TOOLS
CALL RULES
ENDING RULES
```

Nicht:

```text
Session läuft
→ update_instructions(...)
```

## 12. Dynamischer State läuft über Tools

Beispiel:

```text
Gemini:
get_call_state()
Tool:
{
  "earliest_time": "15:00",
  "candidates": [...]
}
```

statt:

```text
update_chat_ctx(...)
```

Der State lebt serverseitig.

## 13. Function Calls müssen schnell sein

Gemini 3.1 wartet auf Tool Responses.

Darum:

erlaubt im normalen Fast Path

```text
record_fact
get_call_state
check_candidate
authorize_commit
finish_call
```

Ziel:

```text
p95 Tool Runtime < 20 ms
```

nicht erlaubt im normalen Fast Path

```text
anderes LLM
Web Search
komplexe Remote API
langsamer DB Query
MCP Roundtrip zu fremdem Server
```

## 14. ActiveCallContext

Während eines laufenden Calls komplett im Worker RAM:

```python
@dataclass
class ActiveCallContext:
    call_id: str
    objective: str
    constraints: dict
    permissions: dict
    facts: dict
    candidates: list
    commitments: list
    pending_input: dict | None
    phase: str
    connected_at: datetime
```

Tool Call:

```text
Gemini
  ↓
RAM Lookup
  ↓
Tool Response
```

nicht:

```text
Gemini
  ↓
PostgreSQL
  ↓
Redis
  ↓
HTTP
  ↓
Tool Response
```

## 15. Persistenz läuft asynchron

Hot State:

```text
RAM
```

Durable State:

```text
PostgreSQL
```

Ablauf:

```text
Tool Call
   │
   ├─ RAM sofort ändern
   ├─ Tool Response sofort
   │
   └─ Event asynchron persistieren
```

Kritische Commitments werden synchron durable geschrieben, bevor sie als final markiert werden.

## 16. Policy Engine

Die wichtigste Architekturgrenze:

```text
Gemini entscheidet:
wie spreche ich?
Policy Engine entscheidet:
was darf ich verbindlich tun?
```

Beispiel:

```text
Praxis:
"Donnerstag 14:30."
Gemini:
authorize_commit({
  type: "appointment",
  datetime: "..."
})
Policy:
User wollte ab 15 Uhr.
Result:
DENIED
```

Gemini erhält:

```json
{
  "allowed": false,
  "reason": "before_allowed_time"
}
```

und spricht weiter.

## 17. Tool Set

Minimal:

```text
record_fact
propose_candidate
authorize_commit
request_user_input
send_dtmf
finish_call
```

Optional:

```text
get_supervisor_advice
```

## 18. record_fact

```json
{
  "key": "doctor_name",
  "value": "Dr. Müller"
}
```

Keine LLM-Auswertung.

Nur:

```text
validate
RAM
event
return
```

## 19. propose_candidate

Beispiel:

```json
{
  "kind": "appointment",
  "value": {
    "datetime": "2026-09-03T16:30:00+02:00"
  }
}
```

CallTool validiert sofort gegen Constraints.

## 20. authorize_commit

Jede verbindliche Aktion muss durch dieses Tool.

```json
{
  "action": "book_appointment",
  "payload": {
    "datetime": "2026-09-03T16:30:00+02:00"
  }
}
```

Antwort:

```json
{
  "allowed": true,
  "commit_id": "commit_01K..."
}
```

Erst danach darf eine verbindliche Bestätigung erfolgen.

## 21. Gemini 3.1 TTS als zusätzliches Scripted-Speech-System

Realtime-Modelle können Text nicht zuverlässig wortgetreu sprechen.

LiveKit dokumentiert deshalb:

```text
session.say()
```

benötigt ein separates TTS-Plugin, wenn ein Realtime-Modell verwendet wird.

**Quelle:**

- <https://docs.livekit.io/agents/multimodality/audio/>

Wir konfigurieren daher zusätzlich:

```text
gemini-3.1-flash-tts-preview
```

obwohl normale Antworten weiterhin direkt von Gemini Live kommen.

## 22. Wofür separates TTS?

Nicht für jede Antwort.

Nur für:

```text
exakte KI-Offenlegung
exakte verbindliche Bestätigung
Hold Phrase
Fallback Phrase
Abbruch-/Fehlerphrase
```

Beispiel:

```text
"Ja, ich bestätige Donnerstag,
den 3. September um 16 Uhr 30."
```

Diese Aussage darf nicht kreativ verändert werden.

## 23. Greeting bereits während des Klingelns erzeugen

Extrem wichtiger Latenztrick.

Während SIP klingelt:

```text
1. Call State laden
2. Gemini Live Session vorbereiten
3. TTS Greeting erzeugen
4. Audio im RAM halten
```

Beim Connect:

```text
answer detected
   ↓
Greeting Audio sofort abspielen
```

Damit muss die angerufene Person nach dem Abheben nicht 500 ms bis 2 Sekunden warten, bis ein Modell initialisiert ist.

## 24. Pre-generated Static Audio

Vollständig statische Sätze können sogar lokal gecacht werden:

```text
"Einen kleinen Moment bitte."
"Vielen Dank fürs Warten."
"Auf Wiederhören."
```

LiveKit unterstützt pre-synthesized audio für scripted speech.

Dadurch:

```text
TTS latency = 0
```

für diese Phrasen.

**Quelle:**

- <https://docs.livekit.io/agents/multimodality/audio/>

## 25. Realtime Transcription: zunächst Gemini-Live-eigen

Gemini 3.1 Flash Live kann:

```text
inputAudioTranscription
outputAudioTranscription
```

liefern.

Google dokumentiert dies direkt in der Live API.

**Quelle:**

- <https://ai.google.dev/gemini-api/docs/live-api/capabilities>

Konfiguration:

```text
input_audio_transcription = enabled
output_audio_transcription = enabled
```

Nutzen:

```text
Call Timeline
Outcome
Debugging
Supervisor
```

## 26. Einschränkung dieser Transkripte

LiveKit weist darauf hin:

Realtime-Modelle liefern keine guten Interim-Transkripte; User-Transkriptionen können verzögert eintreffen und teilweise erst nach der Agent-Antwort ankommen.

**Quelle:**

- <https://docs.livekit.io/agents/models/realtime/>

Darum dürfen Gemini-Live-Transkripte nicht den normalen Gesprächspfad blockieren.

## 27. Optional: Gemini 3.5 Transcribe Live als Shadow STT

Aktuell:

```text
gemini-3.5-transcribe-live
```

Features:

- WebSocket Streaming
- Interim Transcripts
- Final Transcripts
- automatische Spracherkennung
- Code Switching
- Custom Vocabulary
- Smart Transcription
- 85+ Sprachen

**Quelle:**

- <https://ai.google.dev/gemini-api/docs/live-api/live-transcribe>

## 28. Shadow STT ist optional, nicht Default

Default v0.1:

```text
Gemini Live Input Transcription
```

Optional:

```text
shadow_stt.enabled = true
```

Warum nicht immer?

Sonst wird derselbe Audioinput:

```text
Gemini Live
+
Gemini Transcribe Live
```

zweimal gestreamt und berechnet.

Es macht das Gespräch selbst nicht schneller.

## 29. Wann Shadow STT einschalten?

Wenn mindestens eines wichtig wird:

```text
Realtime Captions
frühe Interim Transcripts
Live Dashboard
Custom Vocabulary
Background Supervisor
semantischer LiveKit Turn Detector
bessere Call Analytics
```

## 30. Gemini 3.5 Transcribe Live Limit

Aktuell:

```text
max session duration = 10 minutes
```

**Quelle:**

- <https://ai.google.dev/gemini-api/docs/models/gemini-3.5-transcribe>
- <https://ai.google.dev/gemini-api/docs/live-api/live-transcribe>

Bei aktivem Shadow STT braucht CallTool für lange Calls:

```text
Session Rollover
```

z. B.:

```text
T = 9:30
   ↓
neue Transcribe Session öffnen
   ↓
kurzer Audio Overlap
   ↓
Transcript deduplizieren
   ↓
alte Session schließen
```

## 31. LiveKit Inference Alternative für Shadow STT

LiveKit bietet aktuell:

```text
google/gemini-3.5-transcribe-live
```

als Streaming-STT über LiveKit Inference an.

Python und Node werden unterstützt.

**Quelle:**

- <https://docs.livekit.io/agents/models/stt/gemini/>

Für schnellen Entwicklungsstart:

```text
shadow STT via LiveKit Inference
```

Für maximale Provider-Kontrolle später:

```text
direkte Google Live Transcribe Verbindung
```

## 32. Turn Detection

Für den Native-Audio-Hot-Path:

Default

```text
Gemini native activity / turn detection
```

LiveKit empfiehlt bei Realtime-Modellen grundsätzlich zuerst die im Modell eingebaute Turn Detection.

**Quelle:**

- <https://docs.livekit.io/agents/models/realtime/>

Das vermeidet:

```text
extra STT dependency
extra detector
extra synchronization
```

## 33. Alternative Turn Detection

Wenn echte Call-Tests zeigen:

```text
Gemini antwortet zu früh
Gemini wartet zu lange
Backchannels triggern zu oft
```

dann A/B-Test:

A

```text
Gemini Native Turn Detection
```

B

```text
Gemini automatic activity detection OFF
Gemini 3.5 Transcribe Live
LiveKit Audio TurnDetector
```

LiveKit Audio Turn Detector ist seit Agents 1.6.1 im SDK.

**Quelle:**

- <https://docs.livekit.io/agents/logic/turns/turn-detector/>

## 34. Keine theoretische Entscheidung über Turn Detection

Das richtige Modell wird anhand realer Telefonate gewählt.

Testkorpus:

```text
30 kurze Calls
30 Calls mit vielen "äh", "also", "Moment"
20 Calls mit langen Pausen
20 Calls mit Unterbrechungen
```

Metriken:

```text
false_end_of_turn
late_response
false_interruption
barge_in_stop_latency
```

## 35. Barge-in ist Pflicht

Wenn der Agent sagt:

> „Dann könnte ich Ihnen noch—“

und der Mensch sagt:

> „Moment.“

muss die Agentenausgabe nahezu sofort stoppen.

Keine 1–2 Sekunden weiterreden.

Ziel:

```text
speech detected
→ audio output stop
p95 < 250 ms
```

Engineering-Ziel, keine Provider-Garantie.

## 36. Antwortlatenz-Ziel

Hauptmetrik:

```text
remote_speech_end
→
first_agent_audio_playout
```

Ziele:

```text
simple conversation:
p50 300–600 ms
overall:
p50 < 700 ms
p95 < 1.2 s
```

Das sind interne Ziele.

Nicht Google- oder LiveKit-SLAs.

## 37. Warum native Audio hier so wichtig ist

Bei Cascade wäre das Budget etwa:

```text
end detection        200–400 ms
STT final            100–300 ms
LLM                   150–500 ms
TTS first chunk       150–400 ms
network/playout       50–150 ms
```

Selbst mit Überlappung bleibt mehr Pipeline-Latenz.

Native Audio entfernt mehrere Übergaben.

## 38. Gemini Thinking

Für normalen Call:

```text
thinkingLevel = minimal
```

Gemini 3.1 unterstützt:

```text
minimal
low
medium
high
```

und nutzt standardmäßig minimal für niedrige Latenz.

**Quellen:**

- <https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview>
- <https://docs.livekit.io/agents/models/realtime/plugins/gemini/>

## 39. Kein komplexes Reasoning im normalen Voice-Turn

Der Live-Agent sollte nicht bei:

```text
"Wie lautet der Name?"
```

einen externen Reasoner aufrufen.

Das ist unnötig.

## 40. Gemini 3.7 Flash als Supervisor

Aktuell:

```text
gemini-3.7-flash
```

ist Googles aktuelles stabiles Flash-Workhorse für komplexe agentische Workflows.

**Quelle:**

- <https://ai.google.dev/gemini-api/docs/latest-model>
- <https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash>

Rolle:

```text
nicht normaler Gesprächsmotor
```

sondern:

```text
complex analysis
background planning
outcome enrichment
uncertainty analysis
```

## 41. Background Supervisor

Optional:

```text
Final Transcript Turn
        │
        ├────────→ Gemini Live redet normal weiter
        │
        └────────→ Gemini 3.7 Flash
                         │
                         ▼
                 SupervisorCache
```

Damit läuft 3.7 außerhalb des Hot Paths.

Beispiel Cache:

```json
{
  "risk": "cost_discussion",
  "recommended_action": "do_not_accept",
  "confidence": 0.97
}
```

## 42. get_supervisor_advice

Falls Gemini Live bei einem späteren komplexen Punkt Hilfe braucht:

```text
get_supervisor_advice()
```

antwortet aus:

```text
RAM Cache
```

und nicht erst dann zwingend aus einem neuen 3.7 Request.

Damit verstecken wir Reasoning-Latenz.

## 43. Synchronous Deep Reasoning nur selten

Falls kein Cache existiert und eine Entscheidung wirklich komplex ist:

```text
Gemini Live:
"Ich prüfe das kurz."
Tool:
analyze_complex_case()
Gemini 3.7:
thinking=low
Response:
...
```

Dann darf der Call kurz blockieren.

Für Menschen wirkt:

```text
500 ms – 1.5 s
```

nach einer angekündigten Prüfung normal.

Aber das darf nicht bei jedem Satz passieren.

## 44. Gemini 3.7 Thinking

Unterstützt aktuell:

```text
low
medium
high
```

minimal wird nicht unterstützt.

Default 3.7 ist medium.

Für Telefon-Supervisor:

```text
thinking_level = low
```

als Start.

**Quelle:**

- <https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash>

## 45. Long-Running Gemini Live Sessions

Das ist eine Pflichtanforderung.

Google dokumentiert:

```text
Audio-only session ohne Compression:
15 Minuten
einzelne WebSocket-Verbindung:
ungefähr 10 Minuten
```

**Quellen:**

- <https://ai.google.dev/gemini-api/docs/live-api/session-management>
- <https://ai.google.dev/gemini-api/docs/live-api/best-practices>

## 46. Context Window Compression

Aktivieren:

```text
contextWindowCompression
```

Damit können Live Sessions wesentlich länger laufen.

Beispiel:

```text
trigger: ~25k tokens
sliding window: ~8k tokens
```

Die genauen Werte durch Calls testen.

Google nennt diese Größenordnung selbst als Beispiel.

## 47. Session Resumption

Pflicht.

Google sendet:

```text
SessionResumptionUpdate
```

mit Resumption Handle.

Bei Verbindungswechsel:

```text
new websocket
+
last resumption handle
```

Dadurch kann die logische Session erhalten bleiben.

Resumption Tokens sind laut Google nach Sessionende zwei Stunden gültig.

**Quelle:**

- <https://ai.google.dev/gemini-api/docs/live-api/session-management>

## 48. Warum Python hier gewinnt

Der aktuelle LiveKit-Python-Google-Adapter hat explizite Optionen für:

```text
context_window_compression
session_resumption
```

**Quelle:**

- <https://docs.livekit.io/reference/python/livekit/plugins/google/realtime/realtime_api.html>

Die aktuelle Node-RealtimeModel-Referenz enthält:

```text
contextWindowCompression
```

aber derzeit kein dokumentiertes:

```text
sessionResumption
```

**Quelle:**

- <https://docs.livekit.io/reference/agents-js/classes/plugins_agents_plugin_google.beta.realtime.RealtimeModel.html>

Für kurze Demo-Calls egal.

Für:

```text
Arztwarteschleife
Support
Versicherung
Behörde
```

nicht egal.

## 49. GoAway Handling

Google kündigt WebSocket-Schließungen mit:

```text
GoAway
```

an.

CallTool muss:

```text
GoAway empfangen
Resumption Handle sichern
neue Verbindung öffnen
Audiofluss weiterführen
```

ohne dass SIP getrennt wird.

Soak Test:

```text
20 Minuten
30 Minuten
```

Pflicht vor v1.

## 50. Call Worker und API

Ein Produkt.

Eine Codebase.

Ein Docker Image.

Aber zwei Rollen:

```text
calltool api
calltool worker
```

## 51. Warum zwei Prozessrollen?

LiveKit AgentServer:

- registriert Worker
- dispatcht Jobs
- startet pro Agent Job einen isolierten Prozess
- unterstützt Prewarm
- unterstützt Load Control
- unterstützt Graceful Drain

**Quelle:**

- <https://docs.livekit.io/agents/server/lifecycle/>
- <https://docs.livekit.io/agents/server/options/>

Damit soll der Agent Worker nicht mit dem HTTP/MCP Event Loop vermischt werden.

## 52. Trotzdem einfach deploybar

```text
docker compose up -d
```

startet:

```text
calltool-api
calltool-worker
livekit
livekit-sip
redis
postgres
reverse-proxy
```

CallTool bleibt nach außen ein Tool.

## 53. Development-Modus

Lokal dürfen API und Worker auf derselben Maschine laufen.

Bei Bedarf sogar über ein Dev-Skript gemeinsam gestartet.

Produktionsprozessrollen trotzdem getrennt halten.

## 54. MCP

Aktueller MCP-Spezifikationsstand:

```text
2026-07-28
```

MCP Python SDK:

```text
mcp==2.1.1
```

**Quelle:**

- <https://pypi.org/project/mcp/2.1.1/>

## 55. MCP Tools

Minimal und maximal kompatibel:

```text
phone_call.create
phone_call.status
phone_call.respond
phone_call.cancel
```

Optional später:

```text
phone_call.events
phone_call.list
phone_call.retry
```

## 56. phone_call.create

Beispiel:

```json
{
  "target": {
    "phone_number": "+49301234567",
    "name": "Praxis Müller"
  },
  "objective": "Vereinbare einen Kontrolltermin",
  "constraints": [
    "nächste Woche",
    "nicht Dienstag",
    "frühestens 15 Uhr"
  ],
  "context": {
    "caller_name": "Max Mustermann"
  },
  "permissions": {
    "may_commit": true,
    "may_accept_costs": false,
    "may_disclose": [
      "name"
    ]
  }
}
```

Antwort:

```json
{
  "call_id": "call_01K...",
  "status": "queued"
}
```

Kein Warten auf Gesprächsende.

## 57. phone_call.status

```json
{
  "call_id": "call_01K..."
}
```

Antwort:

```json
{
  "status": "active",
  "phase": "negotiating"
}
```

oder:

```json
{
  "status": "completed",
  "outcome": {
    "success": true,
    "summary": "Termin Freitag 16:30 vereinbart."
  }
}
```

## 58. phone_call.respond

Human-in-the-loop:

```json
{
  "call_id": "call_01K...",
  "input_request_id": "input_01K...",
  "response": {
    "choice": "accept"
  }
}
```

## 59. phone_call.cancel

Idempotent.

```json
{
  "call_id": "call_01K..."
}
```

## 60. REST API

Parallel:

```text
POST /v1/calls
GET  /v1/calls/{id}
POST /v1/calls/{id}/respond
POST /v1/calls/{id}/cancel
GET  /v1/calls/{id}/events
```

MCP und REST rufen denselben:

```text
CallService
```

auf.

## 61. API-Framework

Empfehlung:

```text
Starlette
Uvicorn
Pydantic
```

Warum kein riesiges Framework?

CallTool API braucht:

```text
JSON
MCP Mount
Auth
SSE
Health
```

mehr nicht.

Aktuell verifiziert:

```text
Starlette 1.6.0
Uvicorn 0.52.4
```

**Quellen:**

- <https://pypi.org/project/starlette/>
- <https://pypi.org/project/uvicorn/>

## 62. Package Management

Empfehlung:

```text
uv
pyproject.toml
uv.lock
```

Production immer aus Lockfile bauen.

Nicht jeden Docker Build mit ungebundenen Latest-Versionen.

## 63. Projektstruktur

```text
calltool/
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── docker-compose.yml
├── .env.example
│
├── config/
│   ├── calltool.yaml
│   ├── livekit.yaml
│   └── sip.yaml
│
├── src/calltool/
│   ├── __main__.py
│   │
│   ├── api/
│   │   ├── app.py
│   │   ├── mcp.py
│   │   ├── rest.py
│   │   ├── auth.py
│   │   └── schemas.py
│   │
│   ├── calls/
│   │   ├── service.py
│   │   ├── manager.py
│   │   ├── state.py
│   │   ├── events.py
│   │   └── dispatcher.py
│   │
│   ├── worker/
│   │   ├── server.py
│   │   ├── agent.py
│   │   ├── session.py
│   │   ├── dialer.py
│   │   └── lifecycle.py
│   │
│   ├── voice/
│   │   ├── realtime.py
│   │   ├── transcripts.py
│   │   ├── scripted_speech.py
│   │   ├── supervisor.py
│   │   ├── prompts.py
│   │   └── tools.py
│   │
│   ├── policy/
│   │   ├── engine.py
│   │   ├── constraints.py
│   │   ├── permissions.py
│   │   └── commit.py
│   │
│   ├── storage/
│   │   ├── postgres.py
│   │   ├── calls.py
│   │   ├── events.py
│   │   └── migrations/
│   │
│   ├── realtime/
│   │   ├── active_calls.py
│   │   ├── input_requests.py
│   │   └── pubsub.py
│   │
│   ├── observability/
│   │   ├── logging.py
│   │   ├── metrics.py
│   │   └── tracing.py
│   │
│   └── cli/
│       ├── doctor.py
│       └── smoke_test.py
│
└── tests/
    ├── unit/
    ├── integration/
    ├── simulations/
    └── e2e/
```

## 64. Core Dependencies

Startpunkt:

```toml
[project]
requires-python = ">=3.13,<3.14"
dependencies = [
  "livekit-agents==1.7.1",
  "livekit-plugins-google==1.7.0",
  "mcp==2.1.1",
  "starlette==1.6.0",
  "uvicorn[standard]==0.52.4",
  "pydantic>=2",
  "pydantic-settings",
  "asyncpg",
  "redis",
  "phonenumbers",
  "structlog",
  "opentelemetry-api",
  "opentelemetry-sdk"
]
```

Die restlichen Dependencies nach erstem funktionierendem Build exakt im:

```text
uv.lock
```

festschreiben.

## 65. LiveKit Realtime Session — Zielkonfiguration

Logische Konfiguration:

```python
session = AgentSession(
    llm=google.realtime.RealtimeModel(
        model="gemini-3.1-flash-live-preview",
        voice="Puck",
        instructions=compiled_prompt,
        input_audio_transcription=...,
        output_audio_transcription=...,
        thinking_config=ThinkingConfig(
            thinking_level="minimal",
            include_thoughts=False,
        ),
        context_window_compression=...,
        session_resumption=...,
    ),
    tts=google.beta.GeminiTTS(
        model="gemini-3.1-flash-tts-preview",
        voice_name="Puck-or-compatible-voice",
    ),
)
```

Hinweis: Die exakten Konstruktorargumente beim Implementieren gegen:

```text
livekit-agents==1.7.1
livekit-plugins-google==1.7.0
```

prüfen.

Nicht alten Blog-Code blind kopieren.

## 66. Gemini Live Prompt

Beispiel:

```text
IDENTITY
You are a phone-call AI agent acting on behalf of the caller.
STYLE
Speak naturally and briefly.
Use short conversational responses.
Do not monologue.
Do not narrate internal reasoning.
Do not repeat information unnecessarily.
GOAL
Book an appointment.
CONSTRAINTS
- next week
- not Tuesday
- earliest 15:00
PERMISSIONS
- you may commit to an appointment only if authorize_commit returns allowed=true
- you may disclose only the listed data
- do not accept costs unless explicitly allowed
TOOLS
Use local tools for facts and commitments.
Do not invent missing personal information.
SLOW TOOL RULE
Before any tool that may take noticeable time, tell the person briefly that you are checking.
CALL END
Repeat any final time/date once.
End politely.
```

## 67. Prompt Style ist Performance

Gemini spricht schneller, wenn wir keine langen Antworten provozieren.

Regeln:

```text
1–2 Sätze pro Turn
eine Frage auf einmal
keine Listen am Telefon
kein "Gerne helfe ich Ihnen dabei..."
kein ständiges Zusammenfassen
```

## 68. Human-In-The-Loop

Gemini 3.1 Tool Calls sind blocking.

Das ist hier sogar praktisch.

Ablauf:

```text
Agent:
"Einen kleinen Moment bitte."
Gemini:
request_user_input(...)
Tool wartet.
Hermes:
fragt User.
User:
antwortet.
Tool Future resolved.
Tool Response → Gemini.
Agent:
"Danke fürs Warten..."
```

## 69. Human-In-The-Loop Tool

```python
async def request_user_input(question, options):
    request = await input_store.create(...)
    await event_bus.publish(...)
    return await input_waiter.wait(
        request.id,
        timeout=180,
    )
```

Das Modell wartet bewusst auf das Tool.

## 70. Slow Tools

Drei Klassen:

Klasse A — Fast

```text
<20ms
```

normal erlaubt.

Klasse B — Moderate

```text
20–400ms
```

sparsam.

Klasse C — Slow

```text
>400ms
```

nur nach verbalem Hold/Acknowledge.

## 71. Kein MCP-Inside-MCP im Voice-Hot-Path

CallTool kann selbst MCP-Server sein.

Der Live Voice Agent soll aber nicht für jeden Gesprächsschritt:

```text
CallTool
→ fremder MCP Server
→ Tool
→ Netzwerk
→ zurück
```

machen.

Benötigte externe Daten vor Call laden.

## 72. Prefetch

Bevor gewählt wird:

```text
User Context
Permissions
Known Contact Data
Calendar Constraints
Business Goal
Custom Vocabulary
```

laden.

Dann erst:

```text
dial()
```

## 73. Parallelisierung vor Connect

Beim Call Start gleichzeitig:

```text
LiveKit Room vorbereiten
Gemini Live Session initialisieren
Prompt kompilieren
Greeting TTS vorbereiten
SIP Call starten
Supervisor Client prewarm
```

Nicht sequenziell.

## 74. Ringing Time nutzen

Telefon klingelt oft:

```text
2–10 Sekunden
```

Diese Zeit ist kostenlose Prewarm-Zeit.

Währenddessen:

```text
Google Connection warm
TTS warm
State vollständig
```

Beim Abheben soll der Agent sofort bereit sein.

## 75. Call State Machine

```text
created
  ↓
validating
  ↓
queued
  ↓
prewarming
  ↓
dialing
  ↓
ringing
  ↓
connected
  ↓
active
  ├────────────→ input_required
  │                  │
  │                  └──→ active
  │
  ├────────────→ completing
  │                  ↓
  │              completed
  │
  ├────────────→ failed
  └────────────→ cancelled
```

## 76. Phases innerhalb active

```text
opening
identifying
requesting
negotiating
confirming
closing
```

Nur zur Observability.

Nicht als starre Gesprächslogik.

## 77. Telnyx + LiveKit SIP

Telefonie läuft über:

```text
Telnyx SIP Trunking
```

mit deutscher Rufnummer.

Für den Self-Hosted-Standort in Deutschland ist der europäische Signaling-Endpunkt:

```text
sip.telnyx.eu
```

Default:

```text
TCP
SIP Digest Auth
X-Telnyx-Username im ersten INVITE
+E.164 für Zielnummer und Caller ID
```

Auf Telnyx-Seite werden ein bezahlter Account, eine freigeschaltete deutsche Rufnummer,
eine FQDN-/Credential-SIP-Connection und ein Outbound Voice Profile benötigt. Deutsche
Rufnummern erfordern eine Identitäts- und Adressprüfung.

Telnyx dokumentiert HD Voice derzeit für US-Kunden. Für deutsche Rufnummern wird deshalb
im v0.1 nicht mit G.722 gerechnet; der normale PSTN-/G.711-Pfad bleibt vollständig
kompatibel.

LiveKit SIP macht:

```text
SIP Participant
RTP Media
Outbound Call
DTMF
Hangup
```

**Quelle:**

- <https://docs.livekit.io/telephony/making-calls/outbound-calls/>
- <https://docs.livekit.io/telephony/start/providers/telnyx/>
- <https://developers.telnyx.com/docs/voice/sip-trunking/livekit-configuration-guide>
- <https://sip.telnyx.com/>

## 78. SIP Flow

```text
CallTool Worker
   │
LiveKit Agent
   │
LiveKit Room
   │
CreateSIPParticipant
   │
LiveKit SIP
   │
Telnyx
   │
PSTN
   │
Ziel
```

## 79. wait_until_answered

Outbound Call sollte auf tatsächliche Annahme warten.

Nicht bereits beim:

```text
180 Ringing
```

Conversation State starten.

## 80. Fehlercodes

Map:

```text
486 → busy
603 → rejected
408 → no_answer
480 → unavailable
5xx → provider_failure
```

## 81. DTMF

Tool:

```text
send_dtmf
```

für:

```text
"Drücken Sie 1 für Termine."
```

Nicht LLM frei auf Tastatur hämmern lassen.

DTMF Actions dürfen Policy haben.

## 82. Voicemail

Ein SIP 200 OK kann auch Mailbox sein.

Später:

```text
AMD / voicemail detection
```

Default:

```text
hangup
```

oder konfigurierbar.

## 83. Retry

Nicht blind.

| Grund | Retry |
| ----------------------------------- | -------- |
| Busy | optional |
| No Answer | optional |
| Rejected | nein |
| Invalid Number | nein |
| SIP 5xx | begrenzt |
| Netzwerk vor Connect | begrenzt |
| Gemini Connect Failure vor Gespräch | ja |
| unklarer Commit | **nein** |
| User Cancelled | nein |

## 84. Idempotency

Pflicht.

```text
client_request_id
```

Unique pro Caller/Principal.

Sonst:

```text
Agent timeout
→ retry
→ Praxis zweimal angerufen
```

## 85. Commitment Idempotency

Jede verbindliche Aktion:

```text
commit_id
```

Tool Retry mit gleicher ID darf nicht zweite Zusage erzeugen.

## 86. PostgreSQL Schema

Minimal:

```sql
CREATE TABLE calls (
  id UUID PRIMARY KEY,
  principal_id TEXT NOT NULL,
  client_request_id TEXT,
  status TEXT NOT NULL,
  phase TEXT,
  target_number TEXT NOT NULL,
  request JSONB NOT NULL,
  state JSONB NOT NULL,
  outcome JSONB,
  error JSONB,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  connected_at TIMESTAMPTZ,
  ended_at TIMESTAMPTZ,
  UNIQUE(principal_id, client_request_id)
);
CREATE TABLE call_events (
  id BIGSERIAL PRIMARY KEY,
  call_id UUID NOT NULL REFERENCES calls(id),
  sequence BIGINT NOT NULL,
  type TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  UNIQUE(call_id, sequence)
);
CREATE TABLE input_requests (
  id UUID PRIMARY KEY,
  call_id UUID NOT NULL REFERENCES calls(id),
  status TEXT NOT NULL,
  request JSONB NOT NULL,
  response JSONB,
  created_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ,
  resolved_at TIMESTAMPTZ
);
```

## 87. Redis

Verwendung:

```text
LiveKit Backend
Pub/Sub API ↔ Worker
optional distributed semaphore
```

Nicht Source of Truth.

Source of Truth:

```text
PostgreSQL
```

## 88. Event Model

```text
call.created
call.prewarming
call.dialing
call.ringing
call.connected
call.user_speech_started
call.user_speech_ended
call.agent_speech_started
call.agent_speech_ended
call.tool_started
call.tool_finished
call.fact_recorded
call.candidate_detected
call.commit_requested
call.commit_allowed
call.commit_denied
call.input_required
call.input_received
call.input_timeout
call.session_resuming
call.session_resumed
call.remote_hangup
call.completed
call.failed
```

## 89. Outcome

Outcome kommt primär aus:

```text
Structured State
Tool Events
Commitments
```

und nur sekundär aus Transcript.

Beispiel:

```json
{
  "success": true,
  "reason": "objective_completed",
  "summary": "Termin erfolgreich vereinbart.",
  "facts": {
    "appointment_at": "2026-09-03T16:30:00+02:00",
    "doctor": "Dr. Müller"
  },
  "commitments": [
    {
      "id": "commit_01K...",
      "type": "appointment",
      "confirmed": true
    }
  ],
  "notes": [
    "Versicherungskarte mitbringen"
  ]
}
```

## 90. Outcome Enrichment durch Gemini 3.7

Nach Call Ende:

```text
Structured State
+
Transcript, falls vorhanden
+
Tool Events
     │
     ▼
Gemini 3.7 Flash
     │
     ▼
human-friendly summary
```

Aber:

```text
3.7 darf keine neue Commitment-Fact erfinden.
```

Fakten aus State bleiben authoritative.

## 91. Performance Instrumentierung

Für jeden User Turn:

```text
speech_started_at
speech_ended_at
model_turn_started_at
model_first_audio_at
playout_started_at
playout_stopped_at
tool_start
tool_end
```

## 92. Hauptmetrik

```text
conversation_response_latency =
playout_started_at - speech_ended_at
```

Dashboard:

```text
p50
p90
p95
p99
```

## 93. Weitere Voice-Metriken

```text
barge_in_stop_latency
false_interruption_rate
agent_silence_after_turn
average_turn_duration
agent_talk_ratio
user_talk_ratio
overlap_duration
```

## 94. Tool-Metriken

```text
tool_latency_ms{tool}
slow_tool_count
tool_failure_rate
policy_denied_count
```

## 95. Session-Metriken

```text
live_ws_reconnects
session_resumptions
goaway_events
context_compressions
gemini_empty_responses
```

Letzteres ist wichtig, weil Realtime-Provider naturgemäß gelegentliche Edge Cases haben können.

## 96. Watchdog gegen “Agent ist still”

Ein kritischer Produktionsmechanismus.

Wenn:

```text
User Turn beendet
und
kein Tool läuft
und
kein Agent Audio
und
kein Modellturn
```

für beispielsweise:

```text
2.5 Sekunden
```

dann:

```text
watchdog event
```

und Recovery.

Nicht 20 Sekunden still bleiben.

## 97. Recovery Ladder

```text
1. prüfe Gemini Session
2. prüfe pending tool
3. prüfe connection state
4. reconnect/resume
5. spiele kurze scripted recovery phrase
6. wenn unmöglich: sauber auflegen
```

## 98. Keine automatische kreative Recovery

Nicht:

> „Ich glaube, unser Gespräch wurde unterbrochen, aber ich erinnere mich, dass Sie …“

wenn State unsicher ist.

Stattdessen kurze deterministische Phrase.

## 99. Server Sizing

Für 1–2 parallele Calls:

```text
4 vCPU
8 GB RAM
öffentliche IPv4
```

guter Start.

Keine GPU.

## 100. Warum 4 vCPU / 8 GB?

LiveKit Agents startet pro Job isolierte Prozesse und prewarmt in Production standardmäßig Prozesse anhand CPU.

**Quelle:**

- <https://docs.livekit.io/agents/server/options/>

Wir wollen:

```text
2 aktive Calls
+
1 warmes Prozessbudget
+
LiveKit
+
SIP
+
Postgres
```

ohne Swap oder CPU-Spikes.

## 101. num_idle_processes

Kleine VM:

```text
1 oder 2
```

explizit setzen.

Nicht einfach Default übernehmen.

Damit steht ein bereits initialisierter Worker bereit.

## 102. Prewarm Function

Prewarm:

```text
Google credentials
HTTP clients
Supervisor client
cached prompts
static audio
database pool
```

Nicht erst beim ersten Gesprächsturn.

## 103. Concurrency

CallTool eigener Limit:

```text
max_concurrent_calls = 2
```

zusätzlich zu SIP-Kanälen.

Weitere Calls:

```text
queued
```

## 104. Graceful Shutdown

Worker:

```text
draining
```

nimmt keine neuen Calls.

Laufende Calls dürfen auslaufen.

LiveKit unterstützt Drain Timeout.

Default im SDK ist derzeit großzügig; CallTool explizit konfigurieren.

## 105. Deployments

Bei Update:

```text
new worker starts
health OK
new calls → new worker
old worker drains
active calls finish
old worker terminates
```

Kein:

```text
docker kill
```

mitten im Arztgespräch.

## 106. Docker Compose

```text
services:
  calltool-api
  calltool-worker
  livekit
  livekit-sip
  redis
  postgres
  caddy/nginx/traefik
```

## 107. Ein Docker Image für CallTool

```dockerfile
ENTRYPOINT ["calltool"]
```

API:

```bash
calltool api
```

Worker:

```bash
calltool worker start
```

Dasselbe Image.

## 108. Health Endpoints

API:

```text
/health
/ready
```

Worker hat LiveKit-Agent-Healthcheck.

LiveKit AgentServer stellt standardmäßig einen Health Endpoint bereit.

**Quelle:**

- <https://docs.livekit.io/agents/server/options/>

## 109. calltool doctor

Pflicht für gutes Self-Hosting.

```bash
calltool doctor
```

Output:

```text
[OK] PostgreSQL
[OK] Redis
[OK] LiveKit
[OK] LiveKit SIP
[OK] Telnyx configuration
[OK] Gemini Live
     gemini-3.1-flash-live-preview
[OK] Gemini 3.7
     gemini-3.7-flash
[OK] Gemini TTS
     gemini-3.1-flash-tts-preview
[INFO] Shadow STT
     disabled
[OK] MCP
     2026-07-28
     SDK 2.1.1
READY
```

## 110. Model Smoke Tests

Beim Deploy:

```text
Gemini Live connect
Gemini 3.7 one tiny request
Gemini TTS tiny speech
```

Shadow STT nur wenn aktiviert.

Nicht jede /ready Anfrage darf Geld kosten.

Probe einmal beim Start und danach gecached.

## 111. Preview-Modelle

Aktuell Preview:

```text
gemini-3.1-flash-live-preview
gemini-3.1-flash-tts-preview
```

Darum:

```text
MODEL_ID aus Config
```

und niemals quer durch Code hardcoden.

## 112. Provider Interfaces

Auch wenn Google Default ist:

```python
class RealtimeVoiceProvider(Protocol):
    ...
class SupervisorProvider(Protocol):
    ...
class ScriptedTTSProvider(Protocol):
    ...
class TelephonyProvider(Protocol):
    ...
```

Damit später austauschbar.

## 113. Default Provider Registry

```yaml
providers:
  realtime:
    type: gemini_live
    model: gemini-3.1-flash-live-preview
  supervisor:
    type: gemini
    model: gemini-3.7-flash
  scripted_tts:
    type: gemini_tts
    model: gemini-3.1-flash-tts-preview
  shadow_stt:
    enabled: false
    type: gemini_transcribe_live
    model: gemini-3.5-transcribe-live
  telephony:
    type: livekit_sip
```

## 114. Beispiel CallTool Config

```yaml
server:
  host: 0.0.0.0
  port: 8080
mcp:
  enabled: true
  path: /mcp
  protocol: "2026-07-28"
rest:
  enabled: true
  base_path: /v1
calls:
  max_concurrent: 2
  ring_timeout_seconds: 45
  max_duration_seconds: 1800
  user_input_timeout_seconds: 180
voice:
  realtime:
    provider: gemini
    model: gemini-3.1-flash-live-preview
    voice: Puck
    thinking_level: minimal
    input_transcription: true
    output_transcription: true
    context_compression:
      enabled: true
    session_resumption:
      enabled: true
  scripted_tts:
    provider: gemini
    model: gemini-3.1-flash-tts-preview
    enabled: true
  shadow_stt:
    enabled: false
    provider: gemini
    model: gemini-3.5-transcribe-live
  supervisor:
    enabled: true
    model: gemini-3.7-flash
    thinking_level: low
    mode: background
performance:
  prewarm_workers: 1
  targets:
    turn_latency_p50_ms: 600
    turn_latency_p95_ms: 1200
    barge_in_stop_p95_ms: 250
policy:
  require_commit_authorization: true
storage:
  transcript: false
  audio: false
```

## 115. Audio Recording

Für Performance ist Recording nicht notwendig.

Wenn später gewünscht:

```text
separater Recorder
```

nicht im normalen Hot Path.

## 116. Realtime Transcript Storage

Auch wenn Transkription aktiviert ist:

```text
nicht bei jedem Partial synchron DB schreiben
```

Final Turns:

```text
batch/event
```

Interim Turns:

```text
nur RAM / optional WebSocket UI
```

## 117. Backpressure

Wenn Event Consumer langsam ist:

```text
Voice darf niemals warten.
```

Event Queue begrenzen.

Prioritäten:

```text
P0 Voice
P1 Tools
P2 State
P3 Events
P4 Analytics
```

Analytics darf gedroppt werden.

Voice nicht.

## 118. Event Queue

Worker intern:

```text
asyncio.Queue(maxsize=N)
```

Bei Überlast:

```text
drop debug/transcript interim
```

aber niemals:

```text
commit event
call completed
policy decision
```

## 119. Database Pool

Klein halten:

```text
API Pool
Worker Parent Pool
```

Job Prozesse sollten nicht hunderte DB-Verbindungen erzeugen.

Ggf. pro Job nur eine kleine Verbindung / Shared Service über IPC vermeiden.

Für 1–2 Calls kein Problem.

## 120. Outcome und State bei Worker Exit

LiveKit Shutdown Hook:

```text
persist final state
flush critical events
mark ended
```

kurz halten.

LiveKit wartet standardmäßig nur begrenzte Zeit auf Shutdown Callbacks.

**Quelle:**

- <https://docs.livekit.io/agents/server/job/>

## 121. Error Budgets

Produktqualität nicht nur nach Success Rate.

Track:

```text
technical_call_failure < 1%
silent_agent_failure < 0.2%
constraint_violation = 0
false_commit = 0
```

Ziele müssen durch Evals bestätigt werden.

## 122. “Human-Level” Test

Ein Call gilt nicht als gut, nur weil Ziel erreicht wurde.

Bewerte:

```text
Latenz
Unterbrechbarkeit
Pausen
Antwortlänge
Wiederholungen
natürliche Bestätigungen
Umgang mit Korrekturen
Zahlen/Uhrzeiten
```

## 123. Gesprächsheuristiken

Agent:

```text
antwortet kurz
fragt einzeln
bestätigt wichtige Zahlen
spricht nicht übermäßig höflich
vermeidet Meta-Kommentare
```

## 124. Keine unnötigen Filler

Verboten im Prompt:

```text
"Natürlich!"
"Sehr gerne!"
"Absolut!"
"Ich helfe Ihnen gerne dabei."
```

bei jedem zweiten Turn.

Telefonagent soll effizient klingen.

## 125. Natürliche Wartephrasen

Nur bei tatsächlicher Wartezeit:

```text
"Einen Moment bitte."
"Ich prüfe das kurz."
```

Nicht vor lokalen 5-ms-Tools.

## 126. Telefonnummern

Alle Nummern intern:

```text
E.164
```

Validation:

```text
phonenumbers
```

## 127. Premium-/Notruf-Block

Unabhängig von Datenschutz:

```text
Premium
Notruf
unerlaubte Länder
```

standardmäßig blockieren.

Sonst ist ein kompromittierter MCP-Key teuer.

## 128. Auth

Persönlich:

```text
Bearer API Key
```

Später:

```text
OAuth/OIDC
```

MCP und REST beide auth.

## 129. Secrets

```text
DATABASE_URL
REDIS_URL
LIVEKIT_URL
LIVEKIT_API_KEY
LIVEKIT_API_SECRET
TELNYX_SIP_ADDRESS
TELNYX_SIP_USERNAME
TELNYX_SIP_PASSWORD
TELNYX_FROM_NUMBER
GOOGLE_API_KEY
CALLTOOL_API_KEY
WEBHOOK_SIGNING_SECRET
```

## 130. Google API oder Vertex?

Da Region/Datenschutz aktuell kein Ziel ist:

Realtime

Einfach:

```text
Gemini Developer API
GOOGLE_API_KEY
```

weniger Setup.

### 3.7 Supervisor

ebenfalls Gemini API.

Vertex nur wenn:

```text
IAM
Enterprise Billing
Quota Management
Cloud Integration
```

gewünscht werden.

## 131. Keine unnötige Provider-Mischung

Default:

```text
Google everywhere
```

Das vereinfacht:

```text
Auth
Model behavior
Billing
Debugging
```

## 132. Outbound-Call End-to-End

```text
Hermes:
"Ruf den Zahnarzt an..."
        ↓
phone_call.create
        ↓
CallTool API
        ↓
persistent CallRequest
        ↓
LiveKit dispatch
        ↓
Call Worker
        ├─ compile prompt
        ├─ warm Gemini
        ├─ prepare scripted greeting
        └─ dial SIP
        ↓
Praxis nimmt ab
        ↓
Greeting praktisch sofort
        ↓
Gemini 3.1 Live Audio↔Audio
        ↕
local tools/policy
        ↓
authorize_commit
        ↓
exact scripted confirmation if needed
        ↓
hangup
        ↓
structured outcome
        ↓
Gemini 3.7 summary
        ↓
Hermes:
"Termin Freitag 16:30."
```

## 133. Human-In-The-Loop End-to-End

```text
Praxis:
"Nur 14:30."
Gemini:
constraint check
Tool:
not allowed
Gemini:
"Einen kleinen Moment bitte."
request_user_input()
        ↓
Call status:
input_required
        ↓
Hermes:
"14:30 akzeptieren?"
        ↓
User:
"Ja."
        ↓
phone_call.respond
        ↓
pending tool resolves
        ↓
Gemini:
"Danke fürs Warten. 14:30 passt."
        ↓
authorize_commit
        ↓
confirmation
```

## 134. Keine Agent Handoffs in v0.1

Wegen aktueller Gemini-3.1-/LiveKit-Limitierung:

```text
single live agent
```

Der Supervisor ist kein Voice-Agent-Handoff.

Er ist:

```text
Tool / Background Service
```

## 135. Keine mid-session Prompt Mutation

Wenn User Permission nachträglich ändert:

Nicht versuchen:

```text
update_instructions
```

Stattdessen:

```text
Policy State ändern
```

Gemini bekommt diese Änderung beim nächsten Tool Call zurück.

## 136. Realtime Input nach Tool

User-Rückfragen werden über den offenen Tool Call gelöst.

Dadurch müssen wir kein neues Chat Context Mid-Session injizieren.

Das passt gut zur Gemini-3.1-Architektur.

## 137. Model Failure Recovery

Wenn Gemini Live Connection stirbt:

```text
1. Session Resumption
2. falls möglich neue Verbindung
3. State bleibt lokal
4. kurzer Recovery-Hinweis
```

Nicht SIP sofort auflegen.

## 138. Wenn Session Resumption scheitert

Fallback:

```text
neue Gemini Session
```

mit:

```text
initial history seed
+
kompakter Call State
```

vor erstem Modellturn.

Da Gemini 3.1 initial history seeding unterstützt.

Während aktiver neuer Session dann wieder keine Chat Updates.

## 139. Kompakter Recovery Context

Nicht gesamtes Transcript.

```json
{
  "objective": "...",
  "facts": {...},
  "constraints": {...},
  "candidates": [...],
  "commitments": [...],
  "last_remote_utterance": "..."
}
```

## 140. Fallback Cascade

Optional später:

```text
Gemini 3.5 Transcribe Live
        ↓
Gemini 3.7 Flash
        ↓
Gemini 3.1 Flash TTS
```

Nicht v0.1 Hot Path.

Nutzen:

```text
Live Modell outage
A/B Test
extrem kontrollierter Modus
```

## 141. Warum Fallback nicht sofort bauen?

Mehr Code:

```text
Turn synchronization
STT rollover
LLM streaming
TTS streaming
Barge-in state
Mode switching
```

Erst native Live robust machen.

## 142. Testing-Reihenfolge

Test 1

```text
LiveKit ↔ Telnyx
```

ohne AI.

Test 2

```text
Gemini Live ↔ LiveKit ↔ Telefon
```

Test 3

```text
Tool Calling
```

Test 4

```text
Barge-in
```

Test 5

```text
Session > 10 min
```

Test 6

```text
Policy Commit
```

Test 7

```text
MCP
```

## 143. Real Call Scenarios

Mindestens:

```text
1. einfacher Termin
2. zwei Alternativen
3. keine passende Alternative
4. Nummer falsch verstanden
5. Geburtsdatum verlangt
6. Agent wird unterbrochen
7. Agent wird korrigiert
8. lange Pause
9. 5 Minuten Wartemusik
10. 15 Minuten Call
11. Gemini WebSocket reconnect
12. Busy
13. Rejected
14. Mailbox
15. IVR / DTMF
```

## 144. Zahlen-Test

Telefonagenten scheitern gerne an:

```text
03.09.
16:30
030 123456
90 Euro
Geburtsdatum
```

Eigene Eval-Suite nur für:

```text
dates
times
numbers
spellings
```

## 145. Confirmation Rule

Jede verbindliche kritische Zahl einmal wiederholen.

```text
"Also Donnerstag um 16 Uhr 30, richtig?"
```

oder nach Bestätigung:

```text
"Perfekt, dann ist Donnerstag um 16 Uhr 30 bestätigt."
```

## 146. Shadow STT Custom Vocabulary

Wenn aktiviert:

```text
Praxisname
Arztname
Straßenname
Produktname
```

Custom vocabulary hilft.

Gemini 3.5 Transcribe Live erlaubt bis zu 1.000 Begriffe, Google empfiehlt für beste Ergebnisse typischerweise deutlich weniger.

**Quelle:**

- <https://ai.google.dev/gemini-api/docs/live-api/live-transcribe>

## 147. Session Duration Test

Pflicht-Test:

```text
31 Minuten
```

Warum 31?

Damit mindestens mehrere:

```text
WebSocket lifecycle boundaries
context compression
```

durchlaufen werden.

Call darf:

```text
kein State verlieren
keinen Termin vergessen
nicht doppelt begrüßen
```

## 148. Performance Regression Tests

Jeder Release:

```text
20 simulierte Calls
```

Vergleiche gegen Baseline:

```text
turn latency p50
turn latency p95
barge-in latency
tool latency
memory/call
```

Release blockieren bei starker Regression.

## 149. Load Test

Nicht zuerst 1.000 Calls.

Start:

```text
1
2
4
8
```

parallele simulierte Rooms.

Messen:

```text
CPU
RAM
process spawn latency
event loop health
packet loss
```

## 150. Worker Process Isolation

LiveKit startet für Agent Jobs isolierte Prozesse.

Das ist gut.

Ein Crash in:

```text
Call #3
```

soll nicht:

```text
Call #1
Call #2
```

mitnehmen.

## 151. Monitoring Dashboard

Top-Level:

```text
Active Calls
Queued Calls
Success Rate
Failure Rate
Turn Latency p50/p95
Barge-in p95
Tool p95
Gemini Reconnects
SIP Errors
```

## 152. Logging

Structured JSON:

```json
{
  "level": "info",
  "call_id": "call_...",
  "event": "gemini.session_resumed",
  "duration_ms": 183
}
```

## 153. Trace

```text
call.create
  ↓
dispatch
  ↓
prewarm
  ↓
sip.dial
  ↓
connected
  ↓
voice.turn
  ├─ tool
  └─ speech
  ↓
commit
  ↓
hangup
  ↓
outcome
```

OpenTelemetry.

## 154. Webhooks

```text
call.input_required
call.completed
call.failed
```

HMAC-signiert.

## 155. calltool doctor --call

Optional:

```bash
calltool doctor --call +49...
```

führt einen kurzen Testcall aus:

```text
TTS greeting
Live Gemini response
Hangup
```

für echte Infrastrukturdiagnose.

## 156. CI

CI:

```text
lint
type check
unit
integration
model config check
Docker build
```

Kein realer PSTN-Call auf jedem Commit.

Nightly:

```text
echter Testcall
```

optional.

## 157. Modell-Upgradeprozess

Nie:

```text
latest alias
```

blind.

Statt:

```text
candidate model
→ eval branch
→ 50 test calls
→ compare
→ config switch
```

## 158. Feature Flags

```yaml
features:
  shadow_stt: false
  supervisor: true
  scripted_confirmations: true
  cascade_fallback: false
  semantic_turn_detector: false
```

## 159. Kosten vs Performance

Bei dieser Architektur wird Performance priorisiert.

Das bedeutet:

```text
Native Live
+
Transcription
+
optional Shadow STT
+
Supervisor
```

kann teurer sein als eine minimale Cascade.

Darum Shadow STT und 3.7 Supervisor nur nutzen, wenn sie echten Nutzen bringen.

## 160. Wichtigste Regel für Kosten und Latenz

Kein Modellaufruf ohne Funktion.

```text
Gemini Live:
Conversation
Gemini 3.7:
only when complex/background
Gemini TTS:
only scripted
Gemini Transcribe:
only when shadow transcript needed
```

## 161. MCP ist Control Plane

Nicht:

```text
MCP streamt Telefon-Audio.
```

Sondern:

```text
MCP
→ Job steuern
LiveKit
→ Media
```

## 162. API ist ebenfalls Control Plane

Ein API-Disconnect darf laufenden Call nicht stoppen.

Call lebt als:

```text
durable job
```

weiter.

## 163. Source of Truth

```text
Call State:
Postgres + Worker Hot State
Media:
LiveKit
Conversation Intelligence:
Gemini Live
Permissions:
Policy Engine
```

## 164. v0.1 Scope

```text
Outbound
Deutsch
Telnyx
max 2 Calls
Gemini 3.1 Live
Gemini Live transcription
Gemini 3.1 TTS scripted
Gemini 3.7 outcome
MCP
REST
Human-in-loop
```

Noch nicht:

```text
Shadow STT
Cascade Fallback
Inbound
voicemail message
multi-agent
large scale
```

## 165. v0.2

```text
Gemini 3.5 Shadow STT
Supervisor Cache
better live analytics
DTMF robust
voicemail detection
```

## 166. v1

```text
Long-call soak proven
horizontal workers
distributed concurrency
advanced retry scheduler
multiple SIP providers
provider failover
```

## 167. Definition of Done v0.1

- Telnyx Outbound funktioniert.
- LiveKit SIP funktioniert bidirektional.
- Gemini 3.1 Flash Live führt deutsches Telefongespräch.
- thinkingLevel=minimal.
- Gemini Live Input Transcription aktiv.
- Gemini Live Output Transcription aktiv.
- Context Window Compression aktiv.
- Session Resumption aktiv und getestet.
- 20-Minuten-Testcall funktioniert.
- Barge-in funktioniert.
- p50 Turn Latency wird gemessen.
- p95 Turn Latency wird gemessen.
- lokales Tool p95 <20 ms.
- Gemini 3.1 TTS kann scripted speech ausgeben.
- Greeting wird vor Connect vorbereitet.
- Commitments laufen durch Policy Engine.
- State liegt im Worker RAM.
- State wird durable persistiert.
- MCP create/status/respond/cancel funktioniert.
- REST create/status/respond/cancel funktioniert.
- Idempotency verhindert Doppelanruf.
- Human-in-loop funktioniert.
- Busy/No Answer/Reject werden sauber erkannt.
- Call kann während Ringing gecancelt werden.
- Call kann während Active gecancelt werden.
- Worker kann graceful drain.
- Watchdog erkennt stuck/silent Agent.
- Outcome wird strukturiert geliefert.

## 168. Implementierungsplan

Phase 1 — Repo

```text
Python 3.13.15
uv
LiveKit 1.7.1
Google Plugin 1.7.0
MCP 2.1.1
```

Phase 2 — SIP

```text
Telnyx
LiveKit
LiveKit SIP
```

Ziel:

```text
Call klingelt + bidirektionales Audio
```

Phase 3 — Gemini Realtime

```text
Gemini 3.1 Flash Live
```

Ziel:

```text
freies Telefongespräch
```

Phase 4 — Long Session

```text
Context Compression
Session Resumption
10+ Minuten
```

Phase 5 — Policy Tools

```text
facts
candidates
commit
```

Phase 6 — Scripted TTS

```text
Greeting
Confirm
Hold
```

Phase 7 — API / DB

```text
CallService
Postgres
```

Phase 8 — MCP

```text
create
status
respond
cancel
```

Phase 9 — Performance

```text
instrument
benchmark
tune
```

Phase 10 — Shadow STT / Supervisor

erst danach.

## 169. Warum diese Reihenfolge?

Wenn Gesprächslatenz schlecht ist, willst du wissen:

```text
ist es SIP?
LiveKit?
Gemini?
Tool?
API?
```

Deshalb jede Schicht zuerst isoliert beweisen.

## 170. Was NICHT gebaut werden sollte

Nicht selbst bauen:

```text
SIP Stack
RTP Stack
jitter buffer
echo cancellation
audio codec pipeline
Realtime media server
```

Dafür ist LiveKit da.

## 171. Was CallTool wirklich ist

CallTool ist:

```text
ein Telefon-Execution-Service für AI-Agenten.
```

Input:

```text
Zielnummer
Aufgabe
Kontext
Regeln
Berechtigungen
```

Output:

```text
strukturiertes Gesprächsergebnis
```

## 172. Finale Architektur

```text
┌───────────────────────────────────────────────┐
│                  AI CLIENT                    │
│ Hermes / beliebiger MCP Agent / REST Client   │
└──────────────────────┬────────────────────────┘
                       │
                  MCP / REST
                       │
┌──────────────────────▼────────────────────────┐
│                 CALLTOOL API                  │
│                                               │
│ Auth                                          │
│ Validation                                    │
│ Idempotency                                   │
│ CallService                                   │
│ Status / Events                               │
└──────────────────────┬────────────────────────┘
                       │
                    Postgres
                       │
                LiveKit Dispatch
                       │
┌──────────────────────▼────────────────────────┐
│               CALLTOOL WORKER                 │
│                                               │
│ ActiveCallContext — RAM                       │
│ Policy Engine                                 │
│ Voice Tools                                   │
│ Supervisor Cache                              │
│                                               │
│  Gemini 3.1 Flash Live ← FAST PATH            │
│         │                                     │
│         ├── native audio conversation         │
│         ├── local function tools              │
│         ├── input transcription               │
│         └── output transcription              │
│                                               │
│  Gemini 3.1 Flash TTS ← scripted speech       │
│                                               │
│  Gemini 3.7 Flash ← supervisor/outcome        │
│                                               │
│  Gemini 3.5 Transcribe ← optional shadow STT  │
└──────────────────────┬────────────────────────┘
                       │
                    LiveKit
                       │
                 LiveKit SIP
                       │
                     Telnyx
                       │
                      PSTN
                       │
                  Gesprächspartner
```

## 173. Finale Model-Rollen

gemini-3.1-flash-live-preview

```text
HÖRT
DENKT
SPRICHT
```

Normales Telefonat.

gemini-3.5-transcribe-live

```text
SCHREIBT MIT
```

Optional parallel.

gemini-3.7-flash

```text
SUPERVISOR / ANALYST
```

Nicht pro Satz.

gemini-3.1-flash-tts-preview

```text
SPRICH EXAKT DIESEN TEXT
```

für kritische oder gecachte Phrasen.

## 174. Finale Sprachentscheidung

Python 3.13 gewinnt aktuell.

Nicht weil Python schneller als Node wäre.

Sondern weil:

```text
Voice-Latenz wird von Netzwerk + Modell bestimmt.
```

und Python heute im konkreten LiveKit/Gemini-Stack die vollständigere Session-Lifecycle-Unterstützung bietet.

Der Verlust durch Python:

```text
praktisch nicht wahrnehmbar
```

Der Gewinn durch:

```text
Session Resumption
maturere Google Voice Integration
weniger Adaptercode
```

ist real.

## 175. Wichtigste Performance-Regeln

Wenn nur zehn Regeln umgesetzt werden:

1. Gemini 3.1 Live direkt Audio↔Audio verwenden.
2. thinkingLevel=minimal.
3. Keine langsamen Tools im normalen Gespräch.
4. Call State im RAM.
5. Postgres Writes aus dem normalen Voice-Hot-Path heraushalten.
6. Alles während des Ringens prewarmen.
7. Greeting vorab synthetisieren.
8. Barge-in messen und optimieren.
9. Session Resumption + Context Compression implementieren.
10. Jede Release-Version mit realen Turn-Latency-Metriken testen.

## 176. Wichtigste Zuverlässigkeitsregeln

1. Verbindliche Aktionen nur über authorize_commit.
2. MCP/API Request bleibt nicht bis Call-Ende offen.
3. Idempotency gegen Doppelanrufe.
4. Worker Jobs isolieren.
5. Watchdog gegen stillen/stuck Agent.
6. Kein automatischer Retry bei unsicherem Commitment.
7. Long-session soak tests.
8. Model IDs konfigurieren und pinnen.
9. Graceful Drain.
10. Structured Outcome aus State, nicht Halluzination.

## 177. Aktuelle Primärquellen

### Google Gemini

Gemini Models
- <https://ai.google.dev/gemini-api/docs/models>

Gemini 3.1 Flash Live
- <https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview>

Gemini Live Capabilities
- <https://ai.google.dev/gemini-api/docs/live-api/capabilities>

Gemini Live Session Management
- <https://ai.google.dev/gemini-api/docs/live-api/session-management>

Gemini Live Best Practices
- <https://ai.google.dev/gemini-api/docs/live-api/best-practices>

Gemini 3.5 Transcribe Live
- <https://ai.google.dev/gemini-api/docs/live-api/live-transcribe>

Gemini 3.5 Transcribe Model
- <https://ai.google.dev/gemini-api/docs/models/gemini-3.5-transcribe>

Gemini 3.7 Flash
- <https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash>

Gemini 3.7 Latest Model Guide
- <https://ai.google.dev/gemini-api/docs/latest-model>

Gemini 3.1 Flash TTS
- <https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-tts-preview>

Gemini TTS Streaming
- <https://ai.google.dev/gemini-api/docs/speech-generation>

### LiveKit

Pipeline Types
- <https://docs.livekit.io/agents/models/pipelines/>

Realtime Models
- <https://docs.livekit.io/agents/models/realtime/>

Gemini Live Plugin
- <https://docs.livekit.io/agents/models/realtime/plugins/gemini/>

Gemini STT
- <https://docs.livekit.io/agents/models/stt/gemini/>

Gemini TTS
- <https://docs.livekit.io/agents/models/tts/gemini/>

Agent Speech / say()
- <https://docs.livekit.io/agents/multimodality/audio/>

Turn Detection
- <https://docs.livekit.io/agents/logic/turns/turn-detector/>

Turn Tuning
- <https://docs.livekit.io/agents/logic/turns/tuning/>

Agent Server Options
- <https://docs.livekit.io/agents/server/options/>

Agent Server Lifecycle
- <https://docs.livekit.io/agents/server/lifecycle/>

Job Lifecycle
- <https://docs.livekit.io/agents/server/job/>

Outbound SIP Calls
- <https://docs.livekit.io/telephony/making-calls/outbound-calls/>

LiveKit Server Releases
- <https://github.com/livekit/livekit/releases>

### Telnyx

LiveKit Provider Setup
- <https://docs.livekit.io/telephony/start/providers/telnyx/>

Telnyx + LiveKit Configuration
- <https://developers.telnyx.com/docs/voice/sip-trunking/livekit-configuration-guide>

SIP Signaling Regions
- <https://sip.telnyx.com/>

SIP Authentication
- <https://developers.telnyx.com/docs/voice/sip-trunking/authentication/credential-types>

German DID Requirements
- <https://support.telnyx.com/en/articles/1311450-germany-did-requirements>

### Python Packages

LiveKit Agents 1.7.1
- <https://pypi.org/project/livekit-agents/1.7.1/>

LiveKit Google Plugin 1.7.0
- <https://pypi.org/project/livekit-plugins-google/1.7.0/>

MCP Python SDK 2.1.1
- <https://pypi.org/project/mcp/2.1.1/>

Python 3.13.15
- <https://www.python.org/downloads/release/python-31315/>

### MCP

MCP 2026-07-28
- <https://blog.modelcontextprotocol.io/posts/2026-07-28/>

### Web

Starlette
- <https://pypi.org/project/starlette/>

Uvicorn
- <https://pypi.org/project/uvicorn/>

## 178. Kurzfassung für den eigentlichen Build

Wenn morgen mit der Implementierung begonnen wird:

```text
LANGUAGE
Python 3.13
VOICE
Gemini 3.1 Flash Live
thinking=minimal
SCRIPTED SPEECH
Gemini 3.1 Flash TTS
SUPERVISOR
Gemini 3.7 Flash
TRANSCRIPTION
Gemini-Live built-in first
Gemini 3.5 Transcribe Live later as optional shadow STT
MEDIA
LiveKit + LiveKit SIP
TELEPHONY
Telnyx
CONTROL
MCP 2026-07-28 + REST
STATE
RAM hot state + PostgreSQL durable
MESSAGING
Redis
DEPLOY
one repo
one CallTool image
two roles: API + Worker
PERFORMANCE
prewarm during ringing
local tools only in hot path
session resumption
context compression
barge-in tuning
per-turn latency metrics
```

## 179. Architektur in einem Satz

> **CallTool ist ein agentenunabhängiger MCP-/REST-Telefonservice, dessen Hot Path über Gemini 3.1 Flash Live direkt Audio↔Audio läuft, während lokale Policies und RAM-State Tool Calls in wenigen Millisekunden beantworten und Gemini 3.7, Gemini 3.5 Transcribe sowie Gemini TTS nur dort eingesetzt werden, wo sie zusätzlichen Nutzen bringen, ohne das normale Gespräch zu verlangsamen.**

## 180. Nächster sinnvoller Engineering-Deliverable

Auf Basis dieses Dokuments sollte als nächstes ein ausführbares Repository-Skeleton entstehen:

```text
docker compose up -d
```

mit:

```text
calltool-api
calltool-worker
livekit
livekit-sip
redis
postgres
```

und zunächst:

```text
phone_call.create
phone_call.status
phone_call.cancel
```

plus einem echten Gemini-Live-Testcall.

Erst wenn die gemessene Conversation Response Latency und Barge-in-Qualität stimmen, werden weitere Business-Funktionen gebaut.

Die Reihenfolge ist bewusst:

```text
Voice UX zuerst.
Dann Features.
```
