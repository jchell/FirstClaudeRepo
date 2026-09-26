"""Data Vault 2.0 object definitions and naming conventions.

Objects are named by kind: ``hub_<entity>``, ``link_<relationship>``,
``sat_<parent>_<topic>``, ``pit_<hub>``, ``bridge_<name>``. Each is a Delta table under
the vault lake root with the same name.

  hub     hk_<entity>, <business keys...>, load_date, record_source, _batch_id
  link    hk_<relationship>, hk_<role> per hub, load_date, record_source, _batch_id
  sat     <parent hash key>, load_date, hashdiff, [multi-active key], <attributes...>,
          record_source, _batch_id
  status  (satellite with ``status: true``) as sat, with the single attribute is_deleted
  pit     <hub hash key>, snapshot_date, <sat>_ldts per satellite
  bridge  <hub hash key>, hk_<link> and hk_<role> for every link on the path
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

IDENT = r"^[a-z][a-z0-9_]{0,62}$"
_IDENT = re.compile(IDENT)
PREFIX = {"hub": "hub_", "link": "link_", "sat": "sat_", "pit": "pit_", "bridge": "bridge_"}
Kind = Literal["hub", "link", "sat", "pit", "bridge"]

# Columns every raw-vault table carries besides its keys and attributes.
LOAD_DATE, RECORD_SOURCE, HASHDIFF, BATCH = "load_date", "record_source", "hashdiff", "_batch_id"


def suffix(name: str) -> str:
    for p in PREFIX.values():
        if name.startswith(p):
            return name[len(p) :]
    return name


def hash_key_column(name: str) -> str:
    """hub_customer -> hk_customer; link_customer_order -> hk_customer_order."""
    return f"hk_{suffix(name)}"


class HubDef(BaseModel):
    business_keys: list[str] = Field(min_length=1, max_length=8)

    @field_validator("business_keys")
    @classmethod
    def _idents(cls, v: list[str]) -> list[str]:
        return _check_idents(v, "business key")


class LinkHub(BaseModel):
    hub: str
    # Distinguishes two references to the same hub (e.g. a same-as link).
    role: str | None = None

    def role_name(self) -> str:
        return self.role or suffix(self.hub)


class LinkDef(BaseModel):
    hubs: list[LinkHub] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def _roles(self) -> LinkDef:
        roles = [h.role_name() for h in self.hubs]
        if len(set(roles)) != len(roles):
            raise ValueError("a link that references a hub twice needs a distinct role for each reference")
        _check_idents(roles, "link role")
        return self


class SatDef(BaseModel):
    parent: str  # a hub or link name
    attributes: list[str] = []
    # Multi-active satellites hold several current rows per parent key, told apart by these
    # attributes (e.g. phone type). Each must also appear in attributes.
    multi_active_key: list[str] = []
    # Status-tracking satellite: records deletes (is_deleted) seen in CDC changes.
    status: bool = False

    @model_validator(mode="after")
    def _check(self) -> SatDef:
        if self.status:
            self.attributes, self.multi_active_key = ["is_deleted"], []
        if not self.attributes:
            raise ValueError("a satellite needs at least one attribute")
        _check_idents(self.attributes, "attribute")
        if missing := [k for k in self.multi_active_key if k not in self.attributes]:
            raise ValueError(f"multi-active key {missing} must also be attributes")
        reserved = {LOAD_DATE, RECORD_SOURCE, HASHDIFF, BATCH}
        if clash := [a for a in self.attributes if a in reserved or a.startswith("hk_")]:
            raise ValueError(f"attribute names {clash} are reserved")
        return self


class PitDef(BaseModel):
    hub: str
    satellites: list[str] = Field(min_length=1)


class BridgeDef(BaseModel):
    hub: str
    links: list[str] = Field(min_length=1)


DEFS: dict[str, type[BaseModel]] = {"hub": HubDef, "link": LinkDef, "sat": SatDef, "pit": PitDef, "bridge": BridgeDef}


def validate_object(kind: str, name: str, definition: dict[str, Any]) -> dict[str, Any]:
    if kind not in DEFS:
        raise ValueError(f"unknown vault object kind {kind!r}")
    if not _IDENT.match(name) or not name.startswith(PREFIX[kind]) or len(name) <= len(PREFIX[kind]):
        raise ValueError(f"{kind} names look like {PREFIX[kind]}<name> (lower case, digits, underscores)")
    return DEFS[kind].model_validate(definition).model_dump(exclude_none=True)


def _check_idents(values: list[str], what: str) -> list[str]:
    for v in values:
        if not _IDENT.match(v):
            raise ValueError(f"{what} {v!r} must be lower case letters, digits and underscores")
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {what}s")
    return values


# ------------------------------------------------------------------ resolved objects


class Resolved(BaseModel):
    """An object plus what the loader needs from the objects it references."""

    kind: Kind
    name: str
    definition: dict[str, Any]
    # link: role -> (hub name, hub business keys); sat: the parent's resolved form
    parts: dict[str, Any] = {}

    @property
    def hk(self) -> str:
        return hash_key_column(self.name)


def key_parts(obj: Resolved) -> list[tuple[str, list[str]]]:
    """The (role, business keys) a hub or link hash is built from, in order.

    Hub keys are mapped as ``{bk: column}``; link keys as ``{"<role>.<bk>": column}``.
    """
    if obj.kind == "hub":
        return [("", obj.definition["business_keys"])]
    if obj.kind == "link":
        return [(role, bks) for role, (_, bks) in obj.parts["roles"].items()]
    raise ValueError(f"{obj.kind} has no business keys")


def mapping_keys(obj: Resolved) -> list[str]:
    """The keys a mapping for ``obj`` (or for a satellite of ``obj``) must provide."""
    return [f"{role}.{bk}" if role else bk for role, bks in key_parts(obj) for bk in bks]


# ------------------------------------------------------------------ wizard: "Add to Raw Vault"


class QuickHub(BaseModel):
    name: str = Field(pattern=r"^hub_[a-z0-9_]{1,58}$")
    # business key -> source column
    keys: dict[str, str] = Field(min_length=1)


class QuickLink(BaseModel):
    """A relationship from the job's hub to another hub, keyed by other source columns."""

    name: str = Field(pattern=r"^link_[a-z0-9_]{1,57}$")
    hub: QuickHub


class RawVaultSpec(BaseModel):
    """What the ingestion wizard's "Add to Raw Vault" step saves in a JobSpec."""

    hub: QuickHub
    # source columns described by the hub (empty: no descriptive satellite)
    attributes: list[str] = []
    satellite: str | None = Field(default=None, pattern=r"^sat_[a-z0-9_]{1,58}$")
    # add a status-tracking satellite that records deletes (CDC)
    track_deletes: bool = False
    links: list[QuickLink] = []

    @model_validator(mode="after")
    def _names(self) -> RawVaultSpec:
        entity = suffix(self.hub.name)
        if self.attributes and not self.satellite:
            self.satellite = f"sat_{entity}_details"[:62]
        for bk in self.hub.keys:
            _check_idents([bk], "business key")
        for link in self.links:
            for bk in link.hub.keys:
                _check_idents([bk], "business key")
        return self
