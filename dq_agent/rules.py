"""Data-quality rules from rules/*.yaml (ranges and keys)."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dq_agent.schema import Schema

BASES = {"rule", "assumption"}


class RulesError(ValueError):
    pass


@dataclass(frozen=True)
class RangeRule:
    table: str
    column: str
    min: float | None
    max: float | None
    basis: str
    note: str = ""

    def describe(self) -> str:
        if self.min is not None and self.max is not None:
            return f"{self.column} in [{_fmt(self.min)}, {_fmt(self.max)}]"
        if self.min is not None:
            return f"{self.column} >= {_fmt(self.min)}"
        return f"{self.column} <= {_fmt(self.max)}"


@dataclass(frozen=True)
class KeyRule:
    table: str
    id_column: str | None
    natural_key: tuple[str, ...]


@dataclass
class Rules:
    ranges: list[RangeRule] = field(default_factory=list)
    keys: dict[str, KeyRule] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return ", ".join(self.sources) if self.sources else "none"

    def ranges_for(self, table: str, column: str | None = None) -> list[RangeRule]:
        return [
            r for r in self.ranges
            if r.table == table and (column is None or r.column == column)
        ]

    def key_for(self, table: str) -> KeyRule | None:
        return self.keys.get(table)

    def validate_against(self, schema: Schema) -> list[str]:
        """Rules that point at missing or non-numeric columns."""
        problems = []
        for r in self.ranges:
            if not schema.has_column(r.table, r.column):
                problems.append(f"ranges.yaml: column {r.table}.{r.column} does not exist")
            elif not schema.require_column(r.table, r.column).is_numeric:
                problems.append(f"ranges.yaml: column {r.table}.{r.column} is not numeric")
        for k in self.keys.values():
            for column in filter(None, (k.id_column, *k.natural_key)):
                if not schema.has_column(k.table, column):
                    problems.append(f"keys.yaml: column {k.table}.{column} does not exist")
        return problems

    @classmethod
    def load(cls, rules_dir: Path) -> "Rules":
        rules = cls()
        ranges_path = rules_dir / "ranges.yaml"
        keys_path = rules_dir / "keys.yaml"
        if ranges_path.exists():
            rules.ranges = _parse_ranges(_read_yaml(ranges_path).get("ranges") or [])
            rules.sources.append(f"rules/{ranges_path.name}")
        if keys_path.exists():
            rules.keys = _parse_keys(_read_yaml(keys_path).get("keys") or {})
            rules.sources.append(f"rules/{keys_path.name}")
        return rules


def _read_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise RulesError(f"{path.name}: top level must be a mapping")
    return data


def _parse_ranges(items: list) -> list[RangeRule]:
    parsed = []
    for i, item in enumerate(items):
        where = f"ranges.yaml item {i}"
        try:
            rule = RangeRule(
                table=str(item["table"]),
                column=str(item["column"]),
                min=_number(item.get("min")),
                max=_number(item.get("max")),
                basis=str(item.get("basis", "")),
                note=str(item.get("note", "")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RulesError(f"{where}: {error}") from error
        if rule.basis not in BASES:
            raise RulesError(f"{where}: basis must be one of {sorted(BASES)}")
        if rule.min is None and rule.max is None:
            raise RulesError(f"{where}: at least one of min / max is required")
        if rule.min is not None and rule.max is not None and rule.min > rule.max:
            raise RulesError(f"{where}: min > max")
        parsed.append(rule)
    return parsed


def _parse_keys(items: dict) -> dict[str, KeyRule]:
    parsed = {}
    for table, spec in items.items():
        spec = spec or {}
        natural = tuple(str(c) for c in spec.get("natural") or ())
        id_column = spec.get("id")
        if not id_column and not natural:
            raise RulesError(f"keys.yaml {table}: define id and/or natural")
        parsed[str(table)] = KeyRule(str(table), str(id_column) if id_column else None, natural)
    return parsed


def _number(value) -> float | None:
    return None if value is None else float(value)


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)
