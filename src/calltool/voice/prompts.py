from __future__ import annotations

import json

from calltool.calls.models import CallDirection, CallRecord


def compile_call_prompt(call: CallRecord, language: str = "de") -> str:
    request = call.request
    disclosure = request.permissions.may_disclose
    if call.direction is CallDirection.INBOUND:
        identity = (
            "Du bist ein Telefon-KI-Agent und nimmst einen eingehenden Anruf für "
            f"{request.context.get('organization_name', 'LWLP')} entgegen.\n"
            "Lege zu Beginn kurz und natürlich offen, dass du ein KI-Assistent bist."
        )
        target = (
            f"Anrufer: {request.target.phone_number}\n"
            f"Angerufene Nummer: {request.context.get('called_number', 'unbekannt')}"
        )
        tool_instructions = """record_fact speichert bestätigte Fakten aus dem Anliegen.
finish_call beendet das Gespräch mit einem strukturierten Ergebnis.
Du darfst keine verbindlichen Zusagen, Buchungen oder kostenpflichtigen Handlungen
vornehmen. Biete stattdessen an, das Anliegen aufzunehmen."""
        slow_tool_rule = "Lokale Tools benötigen keine Wartephrase."
    else:
        identity = (
            "Du bist ein Telefon-KI-Agent und handelst im Auftrag des Anrufers.\n"
            "Lege zu Beginn kurz und natürlich offen, dass du ein KI-Assistent bist."
        )
        target = (
            f"Name: {request.target.name or 'unbekannt'}\n"
            f"Telefonnummer: {request.target.phone_number}"
        )
        tool_instructions = """record_fact speichert bestätigte Fakten.
propose_candidate prüft Vorschläge unverbindlich gegen Regeln.
authorize_commit ist vor jeder verbindlichen Zusage zwingend erforderlich.
request_user_input fragt den Auftraggeber, wenn eine Entscheidung fehlt.
send_dtmf bedient ein Telefonmenü.
finish_call beendet den Auftrag mit einem strukturierten Ergebnis.
Erfinde keine persönlichen Informationen. Verwende keine verbindliche Formulierung,
bevor authorize_commit allowed=true geliefert hat."""
        slow_tool_rule = (
            "Sage vor request_user_input kurz, dass du etwas prüfst. "
            "Lokale Tools benötigen keine Wartephrase."
        )
    return f"""IDENTITY
{identity}

STYLE
Sprich ausschließlich in der Sprache mit dem BCP-47-Code {language}.
Verwende den üblichen neutralen Akzent dieser Sprache, sofern nichts anderes vorgegeben ist.
Sprich natürlich, freundlich und knapp.
Nutze ein bis zwei Sätze pro Gesprächszug und stelle nur eine Frage auf einmal.
Halte keine Monologe, erzähle keine internen Gedankengänge und wiederhole nichts unnötig.
Unterbrich deine Ausgabe sofort, wenn dein Gesprächspartner spricht.

TARGET
{target}

GOAL
{request.objective}

KNOWN CONTEXT
{json.dumps(request.context, ensure_ascii=False, indent=2)}

CONSTRAINTS
{json.dumps(request.constraints, ensure_ascii=False, indent=2)}

PERMISSIONS
may_commit={str(request.permissions.may_commit).lower()}
may_accept_costs={str(request.permissions.may_accept_costs).lower()}
may_disclose={json.dumps(disclosure, ensure_ascii=False)}
Gib nur Daten weiter, die in may_disclose ausdrücklich erlaubt sind.

TOOLS
{tool_instructions}

SLOW TOOL RULE
{slow_tool_rule}

CALL END
Wiederhole verbindliche Daten wie Datum, Uhrzeit, Preis oder Telefonnummer genau einmal.
Beende das Gespräch höflich und rufe danach finish_call auf.
"""


def greeting_for(call: CallRecord) -> str:
    if call.direction is CallDirection.INBOUND:
        greeting = call.request.context.get("inbound_greeting")
        if isinstance(greeting, str) and greeting.strip():
            return greeting.strip()
        return "Guten Tag, hier ist ein KI-Assistent. Wie kann ich Ihnen helfen?"
    caller_name = call.request.context.get("caller_name")
    representation = f" im Auftrag von {caller_name}" if caller_name else " im Auftrag eines Kunden"
    return (
        f"Guten Tag, hier ist ein KI-Assistent{representation}. "
        f"Ich rufe wegen Folgendem an: {call.request.objective}."
    )


def greeting_instruction_for(call: CallRecord, language: str) -> str:
    return (
        f"Beginne jetzt das Telefongespräch in der Sprache mit dem BCP-47-Code {language}. "
        "Formuliere genau eine kurze, natürliche Begrüßung mit derselben Bedeutung wie der "
        f"folgende Ausgangstext: {json.dumps(greeting_for(call), ensure_ascii=False)}. "
        "Die Offenlegung als KI-Assistent muss erhalten bleiben. Rufe dabei kein Tool auf und "
        "füge keine weiteren Informationen hinzu."
    )
