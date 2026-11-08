"""Bedrock's ``RetrievalFilter`` → an OpenSearch query.

The filter is a union: each node carries exactly one operator. String comparisons go to the
``.keyword`` subfield that dynamic mapping creates for every string attribute (Bedrock's docs
require keyword fields for filtering for the same reason); numbers and booleans compare on the
field itself.

==========================  =================================================
Bedrock                     OpenSearch
==========================  =================================================
equals / notEquals          term / must_not term — notEquals also matches
                            chunks that lack the key, as on AWS
greaterThan(OrEquals),      range
lessThan(OrEquals)
in / notIn                  terms / must_not terms
startsWith                  prefix on .keyword
stringContains              wildcard ``*value*`` on .keyword (any list member)
listContains                term (on .keyword for strings)
andAll / orAll              bool filter / bool should (minimum_should_match 1)
==========================  =================================================
"""

from __future__ import annotations

from typing import Any

from aws_ai.local_kb.errors import ValidationException

_COMPARISONS = {
    "greaterThan": "gt",
    "greaterThanOrEquals": "gte",
    "lessThan": "lt",
    "lessThanOrEquals": "lte",
}
_LEAVES = {
    "equals",
    "notEquals",
    "in",
    "notIn",
    "startsWith",
    "stringContains",
    "listContains",
    *_COMPARISONS,
}


def to_query(node: dict[str, Any]) -> dict[str, Any]:
    if len(node) != 1:
        raise ValidationException(
            f"A filter node takes exactly one operator; got {sorted(node) or 'none'}."
        )
    ((operator, operand),) = node.items()

    if operator in ("andAll", "orAll"):
        if not isinstance(operand, list) or len(operand) < 2:
            raise ValidationException(f"{operator} needs a list of at least 2 filters.")
        children = [to_query(child) for child in operand]
        if operator == "andAll":
            return {"bool": {"filter": children}}
        return {"bool": {"should": children, "minimum_should_match": 1}}

    if operator not in _LEAVES:
        raise ValidationException(f"Unknown filter operator {operator!r}.")
    if not isinstance(operand, dict) or "key" not in operand or "value" not in operand:
        raise ValidationException(f"{operator} needs {{'key': ..., 'value': ...}}.")
    key, value = operand["key"], operand["value"]

    if operator == "equals":
        return {"term": {_field(key, value): value}}
    if operator == "notEquals":
        return {"bool": {"must_not": [{"term": {_field(key, value): value}}]}}
    if operator in _COMPARISONS:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValidationException(f"{operator} compares numbers; got {value!r}.")
        return {"range": {key: {_COMPARISONS[operator]: value}}}
    if operator in ("in", "notIn"):
        if not isinstance(value, list) or not value:
            raise ValidationException(f"{operator} needs a non-empty list.")
        terms = {"terms": {_field(key, value[0]): value}}
        return terms if operator == "in" else {"bool": {"must_not": [terms]}}
    if operator == "listContains":
        return {"term": {_field(key, value): value}}
    if not isinstance(value, str):
        raise ValidationException(f"{operator} compares strings; got {value!r}.")
    if operator == "startsWith":
        return {"prefix": {f"{key}.keyword": value}}
    # stringContains: substring of a string, or of any member of a string list.
    escaped = value.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")
    return {"wildcard": {f"{key}.keyword": {"value": f"*{escaped}*"}}}


def _field(key: str, value: Any) -> str:
    return f"{key}.keyword" if isinstance(value, str) else key


# ------------------------------------------------------------------ RDS (pgvector)

# Bedrock's RetrievalFilter → a SQL condition over the custom metadata jsonb column, in the forms
# Bedrock ran on Aurora in Phase 0b (local-kb/fixtures/rds/postgresql-statements.json). Bedrock
# binds the jsonb operands as parameters and formats range bounds into the SQL with ``%f``.
#
# ==========================  =================================================
# Bedrock                     SQL over ``c`` (the custom metadata column)
# ==========================  =================================================
# equals (string)             ( c @> {"k": "v"} or c @> {"k": ["v"]} )
# equals (number, boolean)    c @> {"k": v}
# startsWith                  (c ->> 'k') like 'v%'
# greaterThan(OrEquals),      (c ->> 'k')::double precision >= 20261001.000000
# lessThan(OrEquals)
# listContains                c @> {"k": ["v"]}
# andAll                      ( a and b )
# notEquals, in, notIn,       GUESS: not captured; built from the same pieces
# stringContains, orAll
# ==========================  =================================================

_SQL_COMPARISONS = {
    "greaterThan": ">",
    "greaterThanOrEquals": ">=",
    "lessThan": "<",
    "lessThanOrEquals": "<=",
}


def to_sql(node: dict[str, Any], column: str) -> tuple[str, list[Any]]:
    """The condition and its parameters (jsonb operands as ``Jsonb``). Validates as
    ``to_query`` does, so both stores refuse the same filters."""
    to_query(node)
    return _sql(node, column)


def _sql(node: dict[str, Any], column: str) -> tuple[str, list[Any]]:
    from psycopg.types.json import Jsonb

    ((operator, operand),) = node.items()
    if operator in ("andAll", "orAll"):  # orAll: GUESS
        parts = [_sql(child, column) for child in operand]
        joiner = " and " if operator == "andAll" else " or "
        return (
            "( " + joiner.join(sql for sql, _ in parts) + " )",
            [p for _, params in parts for p in params],
        )

    key, value = operand["key"], operand["value"]

    def equals(v: Any) -> tuple[str, list[Any]]:
        if isinstance(v, str):
            return (
                f"( {column} @> %s or {column} @> %s )",
                [Jsonb({key: v}), Jsonb({key: [v]})],
            )
        return f"{column} @> %s", [Jsonb({key: v})]

    if operator == "equals":
        return equals(value)
    if operator == "notEquals":  # GUESS; also matches rows without the key, as on AWS
        sql, params = equals(value)
        return f"NOT ({sql})", params
    if operator in ("in", "notIn"):  # GUESS
        parts = [equals(v) for v in value]
        sql = "( " + " or ".join(s for s, _ in parts) + " )"
        params = [p for _, ps in parts for p in ps]
        return (sql if operator == "in" else f"NOT {sql}"), params
    if operator in _SQL_COMPARISONS:
        bound = f"{value:f}"  # formatted into the SQL with %f, not bound, as Bedrock does
        return (
            f"({column} ->> %s)::double precision {_SQL_COMPARISONS[operator]} {bound}",
            [key],
        )
    if operator == "listContains":
        return f"{column} @> %s", [Jsonb({key: [value]})]
    pattern = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    if operator == "startsWith":
        return f"({column} ->> %s) like %s", [key, pattern + "%"]
    # stringContains (GUESS): a substring of the string, or of any member of a string list.
    return (
        f"(({column} ->> %s) like %s or exists (select 1 from jsonb_array_elements_text("
        f"case when jsonb_typeof({column} -> %s) = 'array' then {column} -> %s "
        f"else '[]'::jsonb end) member where member like %s))",
        [key, f"%{pattern}%", key, key, f"%{pattern}%"],
    )
