"""Secret references and write-only secret fields.

Secrets live only in the SecretStore (Vault). Everything else — metadata rows, job
specs, config, lineage events, API responses — holds a *reference* such as
``vault://kv/dataplat/connections/42#password`` that is resolved at the moment the
value is needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import PlainSerializer, SecretStr, WithJsonSchema

_REF_RE = re.compile(r"^vault://(?P<mount>[a-z0-9_-]+)/(?P<path>[A-Za-z0-9_./-]+)#(?P<key>[A-Za-z0-9_.-]+)$")


class SecretNotFound(KeyError):
    pass


@dataclass(frozen=True)
class SecretRef:
    mount: str
    path: str
    key: str

    @classmethod
    def parse(cls, ref: str) -> SecretRef:
        m = _REF_RE.match(ref)
        if not m or ".." in m["path"].split("/"):
            raise ValueError(f"not a valid secret reference: {ref!r}")
        return cls(m["mount"], m["path"], m["key"])

    @staticmethod
    def is_ref(value: Any) -> bool:
        return isinstance(value, str) and _REF_RE.match(value) is not None

    def __str__(self) -> str:
        return f"vault://{self.mount}/{self.path}#{self.key}"


def _refuse_serialization(_: SecretStr) -> str:
    raise ValueError("write-only secret fields must never be serialized")


# Use for any API/model field that carries a secret value *into* the platform.
# Validation accepts it, serialization raises, and the JSON schema marks it writeOnly,
# so a secret can never leak back out through an API response or a stored model.
WriteOnlySecret = Annotated[
    SecretStr,
    PlainSerializer(_refuse_serialization, when_used="always"),
    WithJsonSchema({"type": "string", "format": "password", "writeOnly": True}),
]
