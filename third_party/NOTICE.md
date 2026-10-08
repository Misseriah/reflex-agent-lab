# Third-party data

`examples/suites/bfcl-native-selection.jsonl` is a modified representation of
BFCL data from ShishirPatil/gorilla, maintained by the Berkeley Function Calling
Leaderboard contributors.

Source revision: `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8`.
Source files: `berkeley-function-call-leaderboard/bfcl_eval/data/BFCL_v4_multiple.json`
and `berkeley-function-call-leaderboard/bfcl_eval/data/possible_answer/BFCL_v4_multiple.json`.
Repository: https://github.com/ShishirPatil/gorilla
License: Apache-2.0; see `BFCL-LICENSE.txt`.

Modifications: converted to the local single-decision task format; separated
reference labels from model-visible inputs; normalized Python schema type aliases;
added an abstention option. This is not a BFCL leaderboard submission or the
REFLEX authors' selected benchmark subset. Native parameter-answer sets are not
used as model inputs. No original REFLEX author data is included.

Version 0.3 additionally includes modified BFCL_v4_simple_python.json (400),
its possible_answer file, and BFCL_v4_irrelevance.json (240) from the same
revision and license. The irrelevance labels come from the official category,
not a nonexistent possible_answer file. The v03/bfcl directory contains seeded,
disjoint routing/relevance subsets and unapproved review proposals, not
author-provided subsets or certified distractors.

The 2026-09-29 cardinality-final derivative selects 193 native function
specifications from that same revision and adds AI-attributed domain review,
pair sampling and six candidate-set sizes. These are local AI-assisted review
records, not the original authors' approvals or independent human certification.
Native task requests, reference function identities and schemas are retained;
the routing policy removes abstention for this positive-only selection task.

Local embedding evidence uses sentence-transformers/all-MiniLM-L6-v2 at
1110a243fdf4706b3f48f1d95db1a4f5529b4d41 (Apache-2.0 model). Model weights are
kept outside this deliverable in the task work directory; vectors, hashes and
dependency versions are stored with the derived data. Model card:
https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2
