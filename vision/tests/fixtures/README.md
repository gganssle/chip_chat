# `generated-vocabulary.py.txt`

A verbatim copy of what `chip_chat.catalog.vocabulary.render_module` wrote for the
catalogue package's own fixture site — the output of:

```bash
python -m chip_chat.catalog --landing <fixtures> --offline --vocabulary <path>
```

It is here so that `chip_chat.vision.vocabulary` is tested against the **real
generator's output** and not only against `vocabulary_module_source`, the mirror of
it that ships in `chip_chat.vision.testing`. The two agreeing is the point: the
mirror exists because `chip-chat-vision` deliberately does not depend on
`chip-chat-catalog` (the generated module is loaded by dotted name at runtime, never
imported), and a mirror nobody checks is a fork.

It is a `.txt` rather than a `.py` so that neither pytest's collector nor an import
in a test can pick it up as a module by accident. Loading it is
`vocabulary_module(path.read_text())`, which is what a deployment does to the module
the build step wrote.

**Do not edit it.** If the generator's output shape changes, regenerate this copy
from `catalog/tests/fixtures/vision-vocabulary.py.txt` and expect
`vision/tests/test_vocabulary.py` to tell you whether the mirror needs the same
change.
