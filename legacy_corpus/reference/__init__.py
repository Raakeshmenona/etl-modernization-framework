"""Perl-semantics reference implementations of the legacy corpus.

These exist for one reason: to capture golden output when a Perl interpreter
(or the legacy Oracle/Teradata instance) is not reachable from the build
agent - which, in a real migration programme, is the normal case.

Every function here is written against `equivalence.perl_semantics` and mirrors
the legacy source statement for statement, including the parts that are bugs.
Each returns the CSV text the legacy job writes to stdout, so replayed output
is byte-comparable with a real subprocess run.

They are NOT the translation. The translations under `translator/cache/` were
produced by the LLM from the legacy source alone and are what the equivalence
gate actually scores.
"""
