"""
Export the contract to JSON Schema.

  python -m api.schema_export            -> writes web/shared/schema.json
  python -m api.schema_export --check    -> non-zero exit if the file is stale

The `--check` mode is the guard against backend and frontend drifting apart:
wire it into CI, or just run it before a demo.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from api.schema import (
    AskRequest,
    ClarifyRequest,
    Coverage,
    Event,
    QuerySpec,
    extraction_schema,
)

OUT = Path(__file__).resolve().parent.parent / "web" / "shared" / "schema.json"


_REF = "#/definitions/{model}"


def _hoist(schema: dict, into: dict[str, dict]) -> dict:
    """Move a model's nested `$defs` up to the shared `definitions` block.

    Pydantic emits sub-models under each root schema's own `$defs`, but our refs
    all point at the top level. Left nested, json2ts fails with
    `Missing $ref pointer "#/definitions/ClarifyOption"` - the sub-models of the
    Event union are exactly the ones the frontend needs most.
    """
    nested = schema.pop("$defs", {})
    for name, definition in nested.items():
        into.setdefault(name, definition)
    return schema


def _force_discriminator_required(definitions: dict[str, dict]) -> None:
    """Mark each event's literal `type` field as required.

    Pydantic gives these fields a default (`type: Literal["rows"] = "rows"`), so
    they land outside `required` and json2ts emits `type?: Type4`. An optional
    property cannot discriminate a union, so `Extract<Event, {type:"rows"}>`
    collapses to `never` and every field access fails to compile.

    Making it required restores a real discriminated union - which is the point:
    the frontend reducer switches on it, and an unhandled event type then fails
    the build instead of silently doing nothing.
    """
    for schema in definitions.values():
        prop = (schema.get("properties") or {}).get("type")
        if not isinstance(prop, dict):
            continue
        is_literal = prop.get("const") is not None or (
            isinstance(prop.get("enum"), list) and len(prop["enum"]) == 1
        )
        if not is_literal:
            continue
        required = schema.setdefault("required", [])
        if "type" not in required:
            required.append("type")
        # json2ts prefers `enum` over `const` for literal-type generation.
        if prop.get("const") is not None and "enum" not in prop:
            prop["enum"] = [prop["const"]]


def build() -> dict:
    definitions: dict[str, dict] = {}

    roots = {
        "QuerySpec": QuerySpec.model_json_schema(ref_template=_REF),
        "Event": TypeAdapter(Event).json_schema(ref_template=_REF),
        "AskRequest": AskRequest.model_json_schema(ref_template=_REF),
        "ClarifyRequest": ClarifyRequest.model_json_schema(ref_template=_REF),
        "Coverage": Coverage.model_json_schema(ref_template=_REF),
    }
    for name, schema in roots.items():
        definitions[name] = _hoist(schema, definitions)

    _force_discriminator_required(definitions)

    # Not consumed by TypeScript - kept here so the schema that constrains the
    # model sits in the same artifact a reviewer reads.
    definitions["OllamaExtractionSchema"] = extraction_schema()

    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "TBXContract",
        "type": "object",
        "properties": {name: {"$ref": _REF.format(model=name)} for name in roots},
        "definitions": definitions,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if the written file is stale")
    args = ap.parse_args()

    payload = json.dumps(build(), indent=2, sort_keys=True) + "\n"

    if args.check:
        if not OUT.exists():
            print(f"MISSING {OUT} - run: python -m api.schema_export", file=sys.stderr)
            return 1
        if OUT.read_text() != payload:
            print(f"STALE {OUT} - run: python -m api.schema_export", file=sys.stderr)
            return 1
        print(f"up to date: {OUT}")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(payload)
    print(f"wrote {OUT}  ({len(payload):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
