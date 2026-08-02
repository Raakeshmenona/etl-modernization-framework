"""Legacy source parsing: Perl heuristics, SQL semantics, dependency graph.

Named `parsers` rather than `parser` on purpose: `parser` is a built-in module
on Python 3.9, and built-ins win over sys.path, so a package called `parser`
here is silently unimportable on the oldest interpreter this project supports.
"""
