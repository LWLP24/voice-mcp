from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from calltool.calls.models import CallDirection, CallOutcome, CallRecord
from calltool.config import PromptProfileConfig, Settings

_MAX_PROMPT_BYTES = 128 * 1024
_PLACEHOLDER = re.compile(r"{{\s*([a-z][a-z0-9_]*)\s*}}")
_SUPPORTED_PLACEHOLDERS = frozenset(
    {
        "call_id",
        "called_phone_number",
        "caller_name",
        "caller_phone_number",
        "constraints_json",
        "context_json",
        "direction",
        "greeting_json",
        "language",
        "may_accept_costs",
        "may_commit",
        "may_disclose_json",
        "may_transfer",
        "objective",
        "organization_name",
        "outcome_json",
        "permissions_json",
        "target_name",
        "target_phone_number",
    }
)


class PromptTemplateError(ValueError):
    """Raised when a prompt profile is missing or contains an invalid template."""


@dataclass(frozen=True)
class _PromptTemplate:
    path: Path
    text: str

    @classmethod
    def load(cls, path: Path) -> _PromptTemplate:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise PromptTemplateError(f"prompt file is not readable: {path}: {exc}") from exc
        if not path.is_file():
            raise PromptTemplateError(f"prompt path is not a file: {path}")
        if size > _MAX_PROMPT_BYTES:
            raise PromptTemplateError(f"prompt file exceeds {_MAX_PROMPT_BYTES} bytes: {path}")
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise PromptTemplateError(f"prompt file is not valid UTF-8: {path}: {exc}") from exc
        if not text:
            raise PromptTemplateError(f"prompt file is empty: {path}")

        placeholders = set(_PLACEHOLDER.findall(text))
        unknown = placeholders - _SUPPORTED_PLACEHOLDERS
        if unknown:
            names = ", ".join(sorted(unknown))
            raise PromptTemplateError(f"unknown placeholder(s) in {path}: {names}")
        remainder = _PLACEHOLDER.sub("", text)
        if "{{" in remainder or "}}" in remainder:
            raise PromptTemplateError(f"malformed placeholder in prompt file: {path}")
        return cls(path=path, text=text)

    def render(self, values: Mapping[str, str]) -> str:
        rendered = _PLACEHOLDER.sub(lambda match: values[match.group(1)], self.text).strip()
        if not rendered:
            raise PromptTemplateError(f"rendered prompt is empty: {self.path}")
        return rendered


@dataclass(frozen=True)
class PromptProfile:
    directory: Path
    system_inbound: _PromptTemplate
    system_outbound: _PromptTemplate
    greeting_inbound: _PromptTemplate
    greeting_outbound: _PromptTemplate
    farewell_inbound: _PromptTemplate
    farewell_outbound: _PromptTemplate
    greeting_instruction_template: _PromptTemplate
    voicemail_instruction_template: _PromptTemplate
    ivr_instruction_template: _PromptTemplate
    watchdog_instruction_template: _PromptTemplate
    watchdog_fallback_template: _PromptTemplate
    supervisor_template: _PromptTemplate

    @classmethod
    def load(cls, settings: Settings) -> PromptProfile:
        config = settings.config.voice.prompts
        directory = _resolve_prompt_directory(settings, config)
        return cls(
            directory=directory,
            system_inbound=_PromptTemplate.load(directory / config.system_inbound),
            system_outbound=_PromptTemplate.load(directory / config.system_outbound),
            greeting_inbound=_PromptTemplate.load(directory / config.greeting_inbound),
            greeting_outbound=_PromptTemplate.load(directory / config.greeting_outbound),
            farewell_inbound=_PromptTemplate.load(directory / config.farewell_inbound),
            farewell_outbound=_PromptTemplate.load(directory / config.farewell_outbound),
            greeting_instruction_template=_PromptTemplate.load(
                directory / config.greeting_instruction
            ),
            voicemail_instruction_template=_PromptTemplate.load(
                directory / config.voicemail_instruction
            ),
            ivr_instruction_template=_PromptTemplate.load(directory / config.ivr_instruction),
            watchdog_instruction_template=_PromptTemplate.load(
                directory / config.watchdog_instruction
            ),
            watchdog_fallback_template=_PromptTemplate.load(directory / config.watchdog_fallback),
            supervisor_template=_PromptTemplate.load(directory / config.supervisor),
        )

    @property
    def source_files(self) -> tuple[Path, ...]:
        return (
            self.system_inbound.path,
            self.system_outbound.path,
            self.greeting_inbound.path,
            self.greeting_outbound.path,
            self.farewell_inbound.path,
            self.farewell_outbound.path,
            self.greeting_instruction_template.path,
            self.voicemail_instruction_template.path,
            self.ivr_instruction_template.path,
            self.watchdog_instruction_template.path,
            self.watchdog_fallback_template.path,
            self.supervisor_template.path,
        )

    def system_prompt(self, call: CallRecord, language: str) -> str:
        template = (
            self.system_inbound if call.direction is CallDirection.INBOUND else self.system_outbound
        )
        return template.render(_template_values(call, language))

    def greeting(self, call: CallRecord, language: str) -> str:
        template = (
            self.greeting_inbound
            if call.direction is CallDirection.INBOUND
            else self.greeting_outbound
        )
        return template.render(_template_values(call, language))

    def farewell(self, call: CallRecord, language: str) -> str:
        template = (
            self.farewell_inbound
            if call.direction is CallDirection.INBOUND
            else self.farewell_outbound
        )
        return template.render(_template_values(call, language))

    def greeting_instruction(self, call: CallRecord, language: str) -> str:
        values = _template_values(call, language)
        values["greeting_json"] = json.dumps(self.greeting(call, language), ensure_ascii=False)
        return self.greeting_instruction_template.render(values)

    def watchdog_instruction(self, call: CallRecord, language: str) -> str:
        return self.watchdog_instruction_template.render(_template_values(call, language))

    def voicemail_instruction(self, call: CallRecord, language: str) -> str:
        return self.voicemail_instruction_template.render(_template_values(call, language))

    def ivr_instruction(self, call: CallRecord, language: str) -> str:
        return self.ivr_instruction_template.render(_template_values(call, language))

    def watchdog_fallback(self, call: CallRecord, language: str) -> str:
        return self.watchdog_fallback_template.render(_template_values(call, language))

    def supervisor_prompt(
        self,
        call: CallRecord,
        outcome: CallOutcome,
        language: str,
    ) -> str:
        values = _template_values(call, language)
        values["outcome_json"] = json.dumps(
            outcome.model_dump(mode="json"), ensure_ascii=False, indent=2
        )
        return self.supervisor_template.render(values)


def _resolve_prompt_directory(settings: Settings, config: PromptProfileConfig) -> Path:
    environment_directory = settings.CALLTOOL_PROMPT_DIR.strip()
    if environment_directory:
        directory = Path(environment_directory).expanduser()
        return directory if directory.is_absolute() else Path.cwd() / directory

    directory = config.directory
    if directory.is_absolute():
        return directory
    return settings.CALLTOOL_CONFIG.parent / directory


def _template_values(call: CallRecord, language: str) -> dict[str, str]:
    request = call.request
    context = request.context
    caller_name = context.get("caller_name")
    organization_name = context.get("organization_name")
    called_phone_number = context.get("called_number")
    return {
        "call_id": call.id,
        "called_phone_number": str(called_phone_number or "unbekannt"),
        "caller_name": str(caller_name or ""),
        "caller_phone_number": request.target.phone_number,
        "constraints_json": json.dumps(request.constraints, ensure_ascii=False, indent=2),
        "context_json": json.dumps(context, ensure_ascii=False, indent=2),
        "direction": call.direction.value,
        "greeting_json": "",
        "language": language,
        "may_accept_costs": str(request.permissions.may_accept_costs).lower(),
        "may_commit": str(request.permissions.may_commit).lower(),
        "may_disclose_json": json.dumps(request.permissions.may_disclose, ensure_ascii=False),
        "may_transfer": str(request.permissions.may_transfer).lower(),
        "objective": request.objective,
        "organization_name": str(organization_name or "unbekannte Organisation"),
        "outcome_json": "",
        "permissions_json": request.permissions.model_dump_json(indent=2),
        "target_name": request.target.name or "unbekannt",
        "target_phone_number": request.target.phone_number,
    }
