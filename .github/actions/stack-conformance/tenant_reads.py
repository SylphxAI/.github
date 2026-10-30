"""Offline aggregate-function declaration checks; no database or credential I/O."""
import json
import pathlib
import re
import tomllib

DECLARATION = "ops/tenant-read-functions.json"
CONTRACT = ("https://github.com/SylphxAI/cloud/blob/main/"
            "docs/runbooks/shared-tenant-read-only.md#declaration")


def has_database(scope):
    resources = scope.get("resources", {})
    if isinstance(resources, dict) and isinstance(resources.get("database"), dict):
        return True
    if isinstance(resources, list) and any(
            isinstance(resource, dict) and resource.get("kind") in (
                "postgres", "POSTGRES", "MANAGED_RESOURCE_KIND_POSTGRES")
            for resource in resources):
        return True
    database = scope.get("database")
    # The existing migrations table configures a migration, not a resource.
    return isinstance(database, dict) and (not database or bool(set(database) - {"migrations"}))


def production_database(data):
    """Base resources apply to production; preview-only overlays do not."""
    environments = data.get("environments", {})
    production = environments.get("production", {}) if isinstance(environments, dict) else {}
    return has_database(data) or isinstance(production, dict) and has_database(production)


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate declaration field")
        result[name] = value
    return result


def validate(text):
    """Match the resource PATCH validator's bounded, exact signature format."""
    data = json.loads(text, object_pairs_hook=unique_object)
    if (not isinstance(data, dict) or set(data) - {"kind", "deskReportFunctions"}
            or data.get("kind", "database") != "database"):
        raise ValueError("expected a database deskReportFunctions resource PATCH body")
    functions = data.get("deskReportFunctions")
    if not isinstance(functions, list) or not 1 <= len(functions) <= 64:
        raise ValueError("deskReportFunctions must contain one to 64 exact signatures")
    seen = set()
    for function in functions:
        if not isinstance(function, dict) or set(function) - {"name", "args"}:
            raise ValueError("a function declaration accepts only name and args")
        name, args = function.get("name"), function.get("args", "")
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name)
                or name.startswith("pg_") or not isinstance(args, str)
                or len(args) > 200 or not re.fullmatch(r"[a-z0-9_ ,()\[\]]*", args)):
            raise ValueError("a function needs a lowercase name and plain argument types")
        signature = (name, args)
        if signature in seen:
            raise ValueError("duplicate desk report function signature")
        seen.add(signature)


def check(tree, path, text):
    """Return the missing declaration diagnostic, or validate its exact blob."""
    if not production_database(tomllib.loads(text)):
        return None
    declaration = str(pathlib.PurePosixPath(path).parent / DECLARATION)
    diagnostic = ("Production database needs " + declaration + " with deskReportFunctions "
                  "exact signatures; add the aggregate functions and declare them through "
                  "PATCH /v1/resources/{id}")
    if tree is None:
        raise ValueError("tenant read declarations require the containing Git tree")
    value = tree.read(declaration)
    if value is None:
        if declaration in tree.paths():
            raise ValueError("tenant read declaration is unreadable: " + declaration)
        return diagnostic
    try:
        validate(value)
    except json.JSONDecodeError:
        raise ValueError(declaration + ": invalid JSON declaration") from None
    except (ValueError, TypeError) as error:
        # Existing unsupported declarations are coverage debt too. They can be
        # adopted once, then replaced without letting the resource set grow.
        return diagnostic + "; invalid declaration: " + str(error)
    return None
