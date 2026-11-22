"""Validate model-supplied argument keys against the advertised tool schema."""


def argument_problem(args, schema):
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    if not isinstance(schema, dict):
        return None
    required = schema.get("required") or []
    properties = schema.get("properties")
    missing = [key for key in required if key not in args]
    extra = []
    if isinstance(properties, dict) and schema.get("additionalProperties") in (None, False) \
            and not schema.get("patternProperties"):
        extra = sorted(args.keys() - properties.keys())
    problems = []
    if missing:
        problems.append("missing required fields: " + ", ".join(missing))
    if extra:
        problems.append("unexpected fields: " + ", ".join(repr(key)[:80] for key in extra))
    if problems and isinstance(properties, dict):
        problems.append("accepted fields: " + (", ".join(properties) or "none"))
    return "; ".join(problems) or None


def input_error(name, problem):
    return f"Error: '{name}' did not run: {problem}. Re-issue a native tool call using its schema."
