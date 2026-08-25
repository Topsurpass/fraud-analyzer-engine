"""Request and response models for flag rules.

Validation here is about the *shape* of a rule, which is knowable without a
result set: an operator that reads a value has one, ``between`` has both bounds,
a rule has at least one condition, and rule names inside a query are distinct.

What cannot be validated here is whether ``column_name`` exists. The engine
never knows the target schema, and the same rule is legitimately written before
the query has ever run. A column that turns out not to be in the result is
reported as a warning at evaluation time instead, which is the same treatment
``build_chart`` gives a chart field pointing at a missing column.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import (
    BINARY_OPERATORS,
    NULLARY_OPERATORS,
    FlagOperator,
    FlagSeverity,
)
from app.schemas.types import UtcDatetime


class FlagConditionBase(BaseModel):
    column_name: str = Field(min_length=1, max_length=255)
    operator: FlagOperator
    value: str | None = None
    value2: str | None = None

    @model_validator(mode="after")
    def _check_operator_arity(self) -> "FlagConditionBase":
        operator = self.operator

        if operator in NULLARY_OPERATORS:
            # Not an error to send a stray value, but keeping it would let the
            # editor round-trip a value that nothing reads and that a later
            # operator change would silently start honouring.
            self.value = None
            self.value2 = None
            return self

        if self.value is None or self.value == "":
            raise ValueError(
                f"operator {operator.value!r} needs a value; only "
                f"{', '.join(sorted(o.value for o in NULLARY_OPERATORS))} "
                f"may omit one"
            )

        if operator in BINARY_OPERATORS:
            if self.value2 is None or self.value2 == "":
                raise ValueError(
                    f"operator {operator.value!r} needs both value and value2"
                )
        else:
            self.value2 = None

        return self


class FlagConditionRead(FlagConditionBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    position: int


class FlagRuleBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    severity: FlagSeverity = FlagSeverity.MEDIUM
    enabled: bool = True
    #: An upper bound rather than an opinion about analysis. It stops a single
    #: request from storing an unbounded rule and keeps evaluation per row
    #: bounded by something a reader can see.
    conditions: list[FlagConditionBase] = Field(min_length=1, max_length=20)


class FlagRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    query_id: str
    name: str
    severity: FlagSeverity
    enabled: bool
    position: int
    conditions: list[FlagConditionRead]
    created_at: UtcDatetime
    updated_at: UtcDatetime


class FlagRuleSetUpdate(BaseModel):
    """Whole-set replace.

    The editor edits every rule at once, so replacing the set is both what the
    UI actually does and what makes ordering trivial: position is the index in
    this list, and no separate reorder call has to exist.
    """

    rules: list[FlagRuleBase] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _names_are_distinct(self) -> "FlagRuleSetUpdate":
        seen: set[str] = set()
        for rule in self.rules:
            key = rule.name.strip().lower()
            if key in seen:
                raise ValueError(f"duplicate rule name {rule.name!r}")
            seen.add(key)
        return self


class FlagRuleSetRead(BaseModel):
    query_id: str
    rules: list[FlagRuleRead]


# ---------------------------------------------------------------------------
# Evaluation output, carried on every run and preview
# ---------------------------------------------------------------------------


class RowFlagRead(BaseModel):
    """One flagged row. ``index`` is its position in ``rows``."""

    index: int
    rule_ids: list[str]
    #: sha256 of the row's values, and how a dismissal addresses it. The index
    #: cannot: it is a position in one run's result and points at a different
    #: row after the query runs again. Absent only on a payload cached before
    #: this field existed.
    fingerprint: str | None = None


class RuleHitRead(BaseModel):
    id: str
    name: str
    severity: FlagSeverity
    matched: int


class FlagOutcomeRead(BaseModel):
    """Only flagged rows appear in ``rows``; unflagged rows are absent."""

    flagged_count: int = 0
    rows: list[RowFlagRead] = Field(default_factory=list)
    rules: list[RuleHitRead] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    #: Rows that matched a rule but have been reviewed and dismissed. They are
    #: absent from ``rows`` and not counted in ``flagged_count``.
    dismissed_count: int = 0


class FlagDismissalRequest(BaseModel):
    """Rows to stop showing in the flagged view, by content fingerprint."""

    # Bounded because the client can send a whole section at once ("dismiss
    # all") and the row limit caps how many a single run can produce.
    fingerprints: list[str] = Field(default_factory=list, max_length=10000)

    @field_validator("fingerprints")
    @classmethod
    def _look_like_hashes(cls, value: list[str]) -> list[str]:
        # These are echoed back from the flagged view, never typed. Rejecting
        # anything that is not a sha256 hex digest keeps arbitrary strings out
        # of a table whose whole contract is "this is a hash of a row".
        for fingerprint in value:
            if len(fingerprint) != 64 or not all(
                character in "0123456789abcdef" for character in fingerprint
            ):
                raise ValueError("each fingerprint must be a sha256 hex digest")
        return value


class FlagDismissalResult(BaseModel):
    query_id: str
    #: Rows newly dismissed, or newly restored. Zero means it was already so.
    changed: int


class FlaggedTallyRead(BaseModel):
    """One line of the flagged summary."""

    connection_id: str
    #: Carried so a notification can name the connection without a second
    #: request. A notification listing a uuid is not a notification.
    connection_name: str | None = None
    query_id: str | None = None
    flagged_count: int
    severity: FlagSeverity
    #: When the newest of these first appeared.
    newest_first_seen_at: UtcDatetime | None = None


class FlaggedSummaryRead(BaseModel):
    """Flagged totals across everything, in one request.

    What the navigation badges and the notification bell read. ``flagged_count``
    answers "is there a queue"; ``newest_first_seen_at`` answers "has anything
    new arrived", which a count cannot - dismiss two and gain two and the total
    has not moved.
    """

    connections: list[FlaggedTallyRead] = Field(default_factory=list)
    queries: list[FlaggedTallyRead] = Field(default_factory=list)
    flagged_count: int = 0
    newest_first_seen_at: UtcDatetime | None = None


class FlaggedRowRead(BaseModel):
    """One stored finding, as the flagged view shows it."""

    #: Display ordinal within its section, not a position in any result.
    index: int
    rule_ids: list[str]
    rule_names: list[str]
    values: list[Any]
    #: Hash of the values, and how a dismissal addresses it.
    fingerprint: str
    severity: FlagSeverity
    #: Typed, so these carry UTC on the wire. An untyped dict sends them naive
    #: on the SQLite backend, and `new Date("...")` then reads them as local
    #: time - see app.schemas.types.
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime


class FlaggedQueryRead(BaseModel):
    """One query's section of a connection's flagged view."""

    query_id: str
    query_name: str
    columns: list[str]
    rows: list[FlaggedRowRead] = Field(default_factory=list)
    rules: list[RuleHitRead] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    flagged_count: int = 0
    dismissed_count: int = 0
    executed_at: UtcDatetime | None = None
    #: Nothing stored and nothing ever run, as opposed to "matched nothing".
    stale: bool = False
    error_code: str | None = None
    error_message: str | None = None


class ConnectionFlaggedRead(BaseModel):
    connection_id: str
    queries: list[FlaggedQueryRead] = Field(default_factory=list)
    flagged_count: int = 0
    dismissed_count: int = 0
    refreshed: bool = False
    #: True when FAE_FLAGGED_REFRESH_MAX_QUERIES capped a refresh.
    refresh_truncated: bool = False
