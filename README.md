# LLM-Augmented Legacy ETL Modernization Framework

Translates legacy Perl and SQL ETL jobs to Python with an LLM, then **proves the
translation is behaviourally equivalent** by running the legacy job and its
replacement over the same data and diffing the output cell-for-cell. A CI gate
blocks promotion below 99% equivalence, and every scored run generates a pytest
regression suite pinned to the legacy behaviour.

## The problem

Enterprises hold an estimated $2.4tn of legacy COBOL and Perl/shell ETL debt.
Rewriting it by hand is slow and, worse, *quietly* risky: a rewrite that breaks
a business rule usually passes code review and surfaces months later in a
reconciliation break.

Commercial converters (AWS SCT, Ispirer) do **structural** translation — they
map syntax to syntax. None of them can tell you whether the translated job
*behaves* the same, so the burden of proof stays entirely with the reviewer.
That is the gap this framework closes.

The failure modes that matter are not syntax errors, which the compiler catches.
They are semantic:

| Legacy behaviour | What a naive translation does | Consequence |
|---|---|---|
| `sort` is a **string** sort | `sorted()` sorts numerically | every downstream positional consumer breaks |
| `split` drops **trailing empty fields** | `str.split()` keeps them | field counts and any derived denominator shift |
| `undef` numifies to **0** | pandas produces `NaN` | one null poisons every aggregate above it |
| `DD/MM/YYYY` | inferred as `MM/DD/YYYY` | silently wrong for every row where both ≤ 12 |
| `int()` truncates | `round()` rounds half to even | off-by-one on ~half the boundary rows |
| Oracle `(+)` marks the **optional** side | join direction flipped | rows silently dropped |
| `RANK()` lets ties share an ordinal | `ROW_NUMBER()` | the **row count** changes, not just the ranks |

## Architecture

```
legacy_corpus/ ──► parsers/ ──────────────► risk flags
  *.pl  *.sql      perl_ast_extractor          │
                   sql_semantic_parser         ▼
                   dependency_mapper      translator/ ──► translator/cache/*.py
                        │                 (Claude API,        (committed,
                        │                  cache-backed)    content-addressed)
                        ▼                                        │
                 migration waves                                 ▼
                                                     translation_validator
                                                     (static gate: imports,
                                                      signature, contract)
                                                                 │
        ┌────────────────────────────────────────────────────────┘
        ▼
   equivalence/dual_executor
        │                    ┌── legacy: perl subprocess, or replayed snapshot
        └── same inputs ────►┤
                             └── translated: in-process pandas
                                      │
                    output_differ (key-aligned, cell-level)
                                      │
                    equivalence_scorer (5 weighted components)
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
   cicd/equivalence_gate    regression_generator    great_expectations_suite
   (exit 1 below 99%)       (pytest, pinned to      (inferred from legacy,
              │              legacy behaviour)       run on BOTH sides)
              ▼
   reporting/snowflake_migration_log ──► Snowflake audit ──► Power BI
```

Orchestrated by [`airflow_dags/migration_pipeline_dag.py`](airflow_dags/migration_pipeline_dag.py);
all four entry points (local runner, CI gate, Airflow DAG, tests) drive the same
[`migration_pipeline.py`](migration_pipeline.py).

## Quickstart

No cloud services, no API key, no Perl required.

```bash
pip install -r requirements.txt
python data/generate_sample_data.py      # deterministic synthetic corpus
python data/capture_golden.py            # capture legacy output snapshots
python scripts/run_migration_local.py    # translate, execute, diff, score
```

Then the gate, and the regression suites it generated:

```bash
python -m cicd.equivalence_gate --junit junit/equivalence.xml
python -m pytest generated/regression -q
```

Useful individually:

```bash
python -m parsers.dependency_mapper                              # migration waves
python -m parsers.perl_ast_extractor legacy_corpus/perl/*.pl     # risk flags
python -m translator.llm_code_generator --unit fx_revenue_calc --print-prompt
python -m translator.translation_validator                       # static gate only
python -m reporting.snowflake_migration_log --promote            # audit trail
```

## Results

Measured on the 7-unit corpus (5 Perl, 2 legacy SQL) with **Perl 5.36 executing
the legacy side**. Reproduce with `python scripts/run_migration_local.py`.

| unit | language | risk flags found | score | gate |
|---|---|---|---:|---|
| `customer_dedupe` | perl | string_sort, inplace_edit | **100.000%** | PASS |
| `txn_daily_agg` | perl | ambiguous_date, string_sort, undef_arithmetic, sprintf_rounding | **100.000%** | PASS |
| `fx_revenue_calc` | perl | undef_arithmetic, string_sort, ambiguous_date, sprintf_rounding | **100.000%** | PASS |
| `account_status_derive` | perl | int_truncation | **100.000%** | PASS |
| `null_handling_report` | perl | **split_no_limit**, sprintf_rounding | **97.400%** | **BLOCKED** |
| `legacy_oracle_balances` | sql (oracle) | oracle_outer_join, nvl, decode, to_date, implicit_cross_join | **100.000%** | PASS |
| `legacy_teradata_ranking` | sql (teradata) | qualify, rank_vs_row_number, teradata_sel | **100.000%** | PASS |

| KPI | Target | Measured |
|---|---|---|
| Auto-translation rate (pass the gate with no manual fix) | ≥ 85% | **85.7%** (6/7) |
| Mean equivalence score | — | **99.63%** |
| Defects reaching "production" (i.e. past the gate) | 0 | **0** |
| Manual effort per script | 40 h → 4 h | **4 h review** on 6 units; the 7th gets a diff naming the defective column and row |
| Generated regression cases | — | **129** across 7 suites |
| End-to-end runtime | < 5 min | **~11 s** |

### The blocked unit

`null_handling_report.pl` uses `split(/\|/, $line)` with no LIMIT, so Perl
discards trailing empty fields and the completeness denominator is the number
of fields the record *actually carried*. The translation used `line.split("|")`,
which keeps them. The gate reports:

```
field_count:      52/500 cells differ (numeric comparison, max delta 5)
completeness_pct: 52/500 cells differ (numeric comparison, max delta 71.4)
```

52 of 500 records (10.4%) carry trailing empties, so 5.2% of cells diverge and
the score lands at 97.400% — below the 99% gate. **This is a defect a code
review does not catch**: both versions read naturally, both produce plausible
percentages, and the output only diverges on the subset of records that end
early. It is caught here in three ways, independently:

1. `parsers/perl_ast_extractor.py` flags `split_no_limit` at line 26 **before**
   translation, and the flag is injected into the prompt.
2. `translator/translation_validator.py` raises a `naive_split` warning on the
   generated module, before it executes.
3. The dual-execution diff names the exact columns and rows.

The unit stays on the legacy stack; the other six are promoted independently.

## Design decisions worth arguing about

**The score is five weighted components, not one percentage.**

| component | weight | why it is separate |
|---|---:|---|
| `schema` | 0.20 | columns present and in order |
| `row_set` | 0.15 | no rows dropped, none invented |
| `cell_value` | 0.50 | cell-for-cell agreement on aligned rows |
| `null_pattern` | 0.10 | Perl's `undef`, an empty CSV field and `NaN` all print as nothing |
| `ordering` | 0.05 | rows emitted in the same order |

A single blended "percentage of matching rows" hides the case that matters
most: a translation that agrees on every cell it emits while silently dropping
30% of rows scores 100% on cells and 0.70 on `row_set`. Splitting the
components is what lets the gate *explain* why it blocked, which is the
difference between a reviewer fixing it in an hour and arguing with it for a
day. Weights are versioned (`SCORER_VERSION`) and written to the audit log with
every run, so a score can be re-derived years later.

**A translation that crashes scores 0, not null.** Recording it as "unknown" is
how broken jobs slip past a dashboard.

**Capture-once, replay-forever for the legacy side.** CI agents do not get an
Oracle instance or a 1998 Perl runtime, so `data/golden/` holds captured
snapshots and `data/golden/_manifest.json` records the provenance of each —
including an `authoritative` flag that is `false` when the snapshot came from
the reference implementation rather than a live run. That flag propagates into
the Snowflake audit log and onto the Power BI landing page, because a reviewer
signing off needs to know which kind of evidence they are looking at.

**The reference implementations are verified, not asserted.** When `perl` is on
PATH, `test_reference_implementation_matches_real_perl` executes the original
Perl and asserts the reference output is byte-identical. It currently passes for
all 5 Perl units against Perl 5.36.

**Translations are cached and committed.** `translator/cache/*.py` is
content-addressed against the legacy source hash and regenerated only when that
hash changes. CI runs are deterministic and free, the migration is reproducible
years later, and the artefact under review is a file in the repo rather than
whatever the model happens to emit today. A stale cache is detected and warned
about, not silently used.

**Generated code is import-restricted.** It is model output, it executes
in-process in CI, and a prompt injected through a comment in a legacy source
file is a real path to code execution on the build agent. The validator enforces
an allow-list and rejects `subprocess`, `os`, `socket`, `eval` and friends
before the module is ever imported.

**Promotion is per unit, not per build.** One blocked translation must not stall
six good ones — that is how equivalence gates end up switched off.

**`parsers/`, not `parser/`.** `parser` is a built-in module on Python 3.9 and
built-ins win over `sys.path`, so a package with that name is silently
unimportable on the oldest interpreter this project supports.

## The research contribution

Two things here are not available in a commercial converter today:

1. **Dual-execution equivalence scoring.** Structural translation is a solved
   problem; proving semantic equivalence is not. The scoring model above —
   component-weighted, key-aligned, tolerance-aware, with provenance attached —
   turns "the reviewer thinks it looks right" into a number a release process
   can gate on.

2. **Automated regression generation from the observed divergence.** Every
   scored run emits a pytest suite pinned to the *legacy* behaviour, with cases
   selected adversarially rather than sampled: numeric boundaries, every
   distinct categorical value, rows carrying blanks, and — most valuable — the
   rows that actually diverged in this run, pinned to the legacy value so a
   future "fix" that reintroduces the defect fails loudly. 129 cases across the
   corpus, generated with no human input. This is the artefact that outlives the
   migration, when the Perl has been deleted and nobody remembers that the
   completeness denominator was supposed to be the observed field count.

Publishable as a technical talk or paper (PyCon India, ODSC).

## Layout

| Path | Purpose |
|---|---|
| `legacy_corpus/` | 7 legacy scripts + `manifest.yaml` + Perl-semantics reference impls |
| `parsers/perl_ast_extractor.py` | Regex/heuristic extraction of Perl ETL logic and 8 risk classes |
| `parsers/sql_semantic_parser.py` | sqlglot parse/transpile with a regex feature-detection fallback |
| `parsers/dependency_mapper.py` | Declared vs inferred DAG, migration waves, undocumented coupling |
| `translator/llm_code_generator.py` | Claude API (`claude-opus-4-8`), cache-backed, repair loop |
| `translator/translation_validator.py` | Static gate: syntax, signature, import allow-list, smells |
| `translator/prompt_templates/` | pandas / PySpark / SQL / repair prompt chain |
| `translator/cache/` | Committed generated translations, content-addressed |
| `equivalence/perl_semantics.py` | Exact Perl 5 shims (`perl_split`, `perl_num`, `perl_sort`, …) |
| `equivalence/dual_executor.py` | Perl subprocess / golden replay / reference runners |
| `equivalence/output_differ.py` | Key-aligned cell diff, numeric vs string per column |
| `equivalence/equivalence_scorer.py` | The 5-component weighted score |
| `equivalence/great_expectations_suite.py` | Suite inferred from legacy output, run on both sides |
| `equivalence/regression_generator.py` | Adversarial pytest regression generation |
| `cicd/equivalence_gate.py` | Azure DevOps gate, JUnit output, per-unit promotion |
| `airflow_dags/migration_pipeline_dag.py` | Nightly DAG with per-unit TaskGroup fan-out |
| `pyspark_jobs/` | PySpark deployment target (decimal money, explicit ordering) |
| `reporting/snowflake_migration_log.py` | Append-only audit trail, JSONL fallback |
| `reporting/migration_audit.sql` | Snowflake DDL + 5 reporting views |
| `powerbi/README.md` | Data model, 4 pages, DAX measures, alerting |
| `tests/test_framework.py` | 53 unit tests |

## Testing

```bash
python -m pytest tests -q                    # 53 framework tests
python -m pytest generated/regression -q     # generated suites (1 unit fails, by design)
```

The framework tests are weighted toward proving the harness can still **fail**:
a gate that has quietly stopped detecting divergence passes every build and is
worse than no gate at all. Several tests feed the scorer deliberately broken
output — a dropped row, a reordered result set, a swapped column, a single
wrong cell — and assert it is rejected, and
`test_the_planted_defect_is_still_caught` fails loudly if
`null_handling_report` ever starts passing.

## Optional dependencies

Everything below degrades gracefully; the quickstart works with none of them.

| Package | Without it |
|---|---|
| `anthropic` | translations served from `translator/cache/`; regeneration unavailable |
| `sqlglot` | SQL feature detection still runs (regex); no transpile output |
| `great-expectations` | built-in evaluator runs the same expectations (also used when GE ≥ 1.x, which drops `from_pandas`) |
| `perl` | legacy side replays `data/golden/` and `authoritative` goes false |
| `pyspark` | pandas translations only |
| `snowflake-connector-python` | audit trail written to `reports/audit/*.jsonl` |
| `apache-airflow` | local runner and CI gate cover the same pipeline |

## Honest limitations

- **The corpus is synthetic.** The scripts are modelled on real Perl→Python
  migration work and carry real defect classes, but 7 units is a demonstration,
  not an estate. The 85.7% auto-translation rate is a property of this corpus.
- **The two SQL units are scored against reference-implementation snapshots**,
  not a live Oracle/Teradata run — `authoritative: false` in the golden
  manifest. The 5 Perl units are scored against real Perl.
- **`translator/cache/` was produced by the prompt chain in this repo and is
  committed rather than regenerated per run.** That is a deliberate
  reproducibility decision, but it does mean the reported score reflects those
  specific translations. `--force` regenerates against the live API.
- **Equivalence is proven on sample data, not proven in general.** The harness
  demonstrates behavioural agreement over a corpus; it is not a formal proof.
  The generated regression suites and inferred expectation suites are what
  extend that evidence to data the translation has not seen — which is why they
  are part of the deliverable rather than a nice-to-have.
"# etl-modernization-framework" 
