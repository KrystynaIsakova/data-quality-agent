"""Check results collected during one agent session."""
from dataclasses import dataclass, field

PASS, FAIL, INFO = "PASS", "FAIL", "INFO"

# basis -> report section for a FAIL
CONFIRMED_BASES = {"rule"}                         # section 1: confirmed issues
POSSIBLE_BASES = {"assumption", "heuristic", "user"}  # section 2: assumptions


@dataclass
class CheckResult:
    check: str              # tool / check name, e.g. "out_of_range"
    table: str
    column: str | None
    rule: str               # what is checked, human-readable
    expected: str
    actual: str
    status: str             # PASS / FAIL / INFO
    basis: str              # rule / assumption / heuristic / user / info
    rows: int = 0           # rows affected by the defect
    share: float | None = None
    condition: str = ""     # SQL condition that finds the defect
    systematic: str = ""    # systematic vs random verdict
    note: str = ""

    @property
    def key(self) -> tuple:
        return (self.check, self.table, self.column, self.rule)

    @property
    def target(self) -> str:
        return f"{self.table}.{self.column}" if self.column else self.table

    @property
    def is_confirmed(self) -> bool:
        return self.status == FAIL and self.basis in CONFIRMED_BASES

    @property
    def is_possible(self) -> bool:
        return self.status == FAIL and self.basis in POSSIBLE_BASES


@dataclass
class Exchange:
    request: str
    answer: str


@dataclass
class ResultStore:
    _results: dict[tuple, CheckResult] = field(default_factory=dict)
    tables: dict[str, dict] = field(default_factory=dict)   # overview facts per table
    questions: list[str] = field(default_factory=list)
    exchanges: list[Exchange] = field(default_factory=list)

    def add(self, result: CheckResult) -> None:
        """Add a result; re-running the same check replaces the old result."""
        self._results[result.key] = result

    def discard(self, key: tuple) -> None:
        """Drop a result that a re-run no longer produces."""
        self._results.pop(key, None)

    def set_fact(self, table: str, name: str, value) -> None:
        self.tables.setdefault(table, {})[name] = value

    def add_exchange(self, request: str, answer: str, questions: list[str]) -> None:
        self.exchanges.append(Exchange(request, answer))
        for q in questions:
            if q not in self.questions:
                self.questions.append(q)

    def all(self) -> list[CheckResult]:
        return list(self._results.values())

    def confirmed(self) -> list[CheckResult]:
        return [r for r in self.all() if r.is_confirmed]

    def possible(self) -> list[CheckResult]:
        return [r for r in self.all() if r.is_possible]

    @property
    def all_pass(self) -> bool:
        return bool(self._results) and not any(r.status == FAIL for r in self.all())

    def __len__(self) -> int:
        return len(self._results)
