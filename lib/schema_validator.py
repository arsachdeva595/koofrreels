from __future__ import annotations

import json
from pathlib import Path

import jsonschema


def load_schema(schemas_dir: str, artifact_name: str) -> dict:
    path = Path(schemas_dir) / "artifacts" / f"{artifact_name}.schema.json"
    if not path.exists():
        raise FileNotFoundError(f"Schema not found: {path}")
    return json.loads(path.read_text())


def validate(artifact: dict, schema: dict) -> list[str]:
    errors = []
    validator = jsonschema.Draft7Validator(schema)
    for error in validator.iter_errors(artifact):
        errors.append(f"{'.'.join(str(p) for p in error.absolute_path)}: {error.message}")
    return errors


def validate_artifact(schemas_dir: str, artifact_name: str, artifact: dict) -> list[str]:
    try:
        schema = load_schema(schemas_dir, artifact_name)
        return validate(artifact, schema)
    except FileNotFoundError:
        return []
