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
