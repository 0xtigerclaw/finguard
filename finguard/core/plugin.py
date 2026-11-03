"""What a capability is, and the rules it must meet before the program will start."""
from __future__ import annotations

import hashlib
import importlib
import json
import pkgutil
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .context import Context

_NAME = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
_SCHEMA_WORDS = {"type", "properties", "required", "additionalProperties", "items", "enum", "pattern",
                 "minLength", "maxLength", "minimum", "maximum", "description", "title"}
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "number": (int, float)}
_TYPE_WORDS = {"object": "an object", "array": "a list", "string": "text", "boolean": "true or false",
               "integer": "a whole number", "number": "a number"}


class LoadError(Exception):
    """A plugin broke a load-time rule. The program does not start."""


class Refused(Exception):
    """A capability said no, in plain words. The door returns `refused`."""


class Failed(Exception):
    """A capability could not finish, in plain words. The door returns `failed`."""


def hash_of(value: Any) -> str:
    """The sha256 of a JSON value, the same however its keys were ordered."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Capability:
    name: str
    summary: str
    kind: str  # "read" or "act"
    approval: str  # "none" or "person"
    verifies: str  # how code checks the result, in one plain sentence
    params: dict  # JSON Schema; only the words in _SCHEMA_WORDS are allowed
    run: Callable[["Context", dict], dict]
    subject: Callable[["Context", dict], str] | None = None  # the sha256 an approval binds to


def _check_schema(schema: Any, where: str) -> None:
    """A schema word this checker does not know would silently not be enforced, so it stops the load."""
    if not isinstance(schema, dict):
        raise LoadError(f"{where}: a schema must be an object.")
    unknown = set(schema) - _SCHEMA_WORDS
    if unknown:
        raise LoadError(f"{where}: the schema word {sorted(unknown)[0]} is not supported.")
    if "type" in schema and schema["type"] not in _TYPES:
        raise LoadError(f"{where}: the schema type {schema['type']} is not supported.")
    if not isinstance(schema.get("additionalProperties", False), bool):
        raise LoadError(f"{where}: additionalProperties must be true or false.")
    for child in schema.get("properties", {}).values():
        _check_schema(child, where)
    if "items" in schema:
        _check_schema(schema["items"], where)


def problem_with_params(schema: dict, value: Any, name: str = "params") -> str | None:
    """What is wrong with these params, in plain words, or None if they fit the schema."""
    kind = schema.get("type")
    if kind:
        fits = isinstance(value, _TYPES[kind]) and (kind == "boolean" or not isinstance(value, bool))
        if not fits:
            return f"{name} must be {_TYPE_WORDS[kind]}."
    if "enum" in schema and value not in schema["enum"]:
        return f"{name} must be one of: {', '.join(str(v) for v in schema['enum'])}."
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{name} is too short."
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{name} is too long: the limit is {schema['maxLength']:,} characters."
        if "pattern" in schema and not re.search(schema["pattern"], value):
            return f"{name} is not in the right form."
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{name} must be {schema['minimum']} or more."
        if "maximum" in schema and value > schema["maximum"]:
            return f"{name} must be {schema['maximum']} or less."
    if isinstance(value, list) and "items" in schema:
        for item in value:
            problem = problem_with_params(schema["items"], item, f"An item in {name}")
            if problem:
                return problem
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{key} is missing."
        for key, item in value.items():
            if key in props:
                problem = problem_with_params(props[key], item, key)
                if problem:
                    return problem
            elif schema.get("additionalProperties") is False:
                return "An unknown param was given."
    return None


class Registry:
    def __init__(self) -> None:
        self.capabilities: dict[str, Capability] = {}

    def add(self, cap: Capability) -> None:
        """Add a capability, or stop the program if it breaks a load-time rule."""
        label = cap.name or "A capability"
        if not isinstance(cap.name, str) or not _NAME.fullmatch(cap.name):
            raise LoadError(f'The name "{cap.name}" must look like area.verb.')
        if cap.name in self.capabilities:
            raise LoadError(f"{label} is declared twice.")
        if cap.kind not in ("read", "act"):
            raise LoadError(f'{label}: kind must be "read" or "act".')
        if cap.approval not in ("none", "person"):
            raise LoadError(f'{label}: approval must be "none" or "person".')
        if cap.kind == "act" and cap.approval != "person":
            raise LoadError(f"{label} acts, so a person must approve it.")
        if cap.approval == "person" and cap.subject is None:
            raise LoadError(f"{label} needs a subject for its approval to bind to.")
        if not (isinstance(cap.verifies, str) and cap.verifies.strip()):
            raise LoadError(f"{label} must state how its result is verified.")
        if not (isinstance(cap.summary, str) and cap.summary.strip()):
            raise LoadError(f"{label} needs a summary.")
        if not callable(cap.run):
            raise LoadError(f"{label} has nothing to run.")
        _check_schema(cap.params, label)
        if cap.params.get("type") != "object":
            raise LoadError(f"{label}: params must be an object.")
        self.capabilities[cap.name] = cap

    def get(self, name: str) -> Capability | None:
        return self.capabilities.get(name)

    def rules(self) -> list[dict]:
        return [{"name": c.name, "summary": c.summary, "kind": c.kind, "approval": c.approval,
                 "verifies": c.verifies, "params": c.params}
                for c in sorted(self.capabilities.values(), key=lambda c: c.name)]


def discover(package: str = "finguard.plugins") -> Registry:
    """Load every module in the package that has register(). A broken one stops the program."""
    registry = Registry()
    importlib.invalidate_caches()
    found = importlib.import_module(package)
    for name in sorted(m.name for m in pkgutil.iter_modules(found.__path__)):
        try:
            module = importlib.import_module(f"{package}.{name}")
        except Exception as e:
            raise LoadError(f"The plugin {name} could not load: {e}") from e
        register = getattr(module, "register", None)
        if register is None:
            continue
        try:
            register(registry)
        except LoadError:
            raise
        except Exception as e:
            raise LoadError(f"The plugin {name} could not register: {e}") from e
    return registry
