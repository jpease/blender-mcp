r"""
Enumerate every character that can smuggle a path separator past the addon.

Run it - it needs no Blender and no network::

    .venv/bin/python scripts/text_hygiene_enumeration.py

**Why this is committed rather than pasted into a docstring.** Three repair
cycles of Task 3 each blocked the homoglyph the previous critic had used, and
each shipped the class. The question a blocklist can never answer is "what else
is there?", and that question is one loop over Unicode. This script is that
loop, so the answer is reproducible instead of asserted, and so a future
Unicode version can be re-measured rather than re-reasoned about.

It reports five things:

1. Every code point outside `UNSAFE_CATEGORIES` whose NFKC form contains `/`,
   `\\` or `:` - the characters that are inert to a category filter and become a
   real separator in any consumer that normalises.
2. What `client_safe_leaf` does with each of them, so "the allowlist catches
   them" is a measurement rather than a claim.
3. What `safe_relative_link` does with the link shapes that defeated the three
   previous blocklists.
4. That NFKC is the identity on every character `LINK_COMPONENT_ALLOWED` admits,
   which is why `safe_relative_link` needs no normalisation pass and why adding
   one could only widen what it publishes.
5. The confusables `is_confusable` exists for: characters the leaf allowlist
   *does* admit and NFKC still changes, which is the case neither allowlist
   covers.
6. **The confusables it does not catch**, measured rather than implied: the
   classic homoglyph class NFKC leaves untouched. This is the limit
   `is_confusable`'s docstring names, and it is printed here so the claim is a
   reading of this script's output rather than a sentence somebody wrote.
7. That the check is NFKC-against-NFC, so a canonically decomposed name - what
   macOS produced for years - is published rather than reduced.
8. **What composing first costs.** `NFKC(x) == NFKC(NFC(x))` holds over every
   assigned code point - and that is a property of the *NFKC form*, while
   `is_confusable` compares NFKC against NFC, so the identity does not answer
   the question the section used to head itself with. The flip set is printed
   instead: the code points the NFC change newly admits, how many of those
   `_is_admissible_leaf` will actually publish, and U+212B / U+2126 as worked
   cases.
"""

import importlib.util
import pathlib
import unicodedata

# Loaded by path, not imported: `bundled/addon/__init__.py` imports `bpy`, so
# the package cannot be imported outside Blender. This module deliberately
# imports nothing from its own package, which is what makes that possible - the
# same property `tests/conftest.load_addon_source_module` relies on.
_MODULE = pathlib.Path(__file__).resolve().parents[1] / "src/blender_mcp/bundled/addon/text_hygiene.py"
_spec = importlib.util.spec_from_file_location("addon_text_hygiene", _MODULE)
if _spec is None or _spec.loader is None:
    raise SystemExit(f"could not load {_MODULE}")
text_hygiene = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(text_hygiene)

MAX_LINK_CHARS = 256

print("=== 1. code points outside UNSAFE_CATEGORIES whose NFKC form holds / \\ or : ===")
smugglers: list[str] = []
for code_point in range(0x20, 0x110000):
    character = chr(code_point)
    if unicodedata.category(character) in text_hygiene.UNSAFE_CATEGORIES:
        continue
    folded = unicodedata.normalize("NFKC", character)
    if folded != character and any(separator in folded for separator in ("/", "\\", ":")):
        smugglers.append(character)
print(f"  found: {len(smugglers)}")

print("\n=== 2. what client_safe_leaf does with each of them ===")
for character in smugglers:
    name = unicodedata.name(character, "<unnamed>")
    category = unicodedata.category(character)
    published = text_hygiene.client_safe_leaf(f"/shots/a{character}b.blend")
    verdict = "REDUCED" if published == text_hygiene.UNNAMEABLE else "published"
    print(f"  U+{ord(character):04X} {category} {name[:34]:34s} -> {verdict}: {published!r}")

print("\n=== 2b. the two Sm slashes NFKC leaves alone, which is why this is an allowlist ===")
for character in ("\u2044", "\u2215"):
    published = text_hygiene.client_safe_leaf(f"/shots/a{character}b.blend")
    verdict = "REDUCED" if published == text_hygiene.UNNAMEABLE else "published"
    print(
        f"  U+{ord(character):04X} {unicodedata.category(character)} "
        f"NFKC unchanged={unicodedata.normalize('NFKC', character) == character} -> {verdict}: {published!r}"
    )

print("\n=== 3. the four link shapes that defeated the three previous blocklists ===")
LINK_CASES = {
    "cycle-2: literal ..": "//../../clients/acme/canon.blend",
    "cycle-3 F1: U+FE68, NFKC -> backslash": "//..\ufe68..\ufe68clients\ufe68acme\ufe68canon.blend",
    "cycle-3 F2: U+29F8 BIG SOLIDUS": "//..\u29f8..\u29f8clients\u29f8acme\u29f8canon.blend",
    "cycle-3 F3: no homoglyph at all, rooted //": "///Users/victim/clients/acme-merger/lib/canon.blend",
    "cycle-3 F4: ZWSP hides .. from a raw gate": "//.\u200b./.\u200b./clients/acme/canon.blend",
    "benign, must still publish whole": "//libs/canon.blend",
}
for label, filepath in LINK_CASES.items():
    whole = text_hygiene.safe_relative_link(filepath, MAX_LINK_CHARS)
    relative = text_hygiene.relative_link_body(text_hygiene.strip_unsafe(filepath)) is not None
    published = whole if whole is not None else text_hygiene.client_safe_leaf(filepath)
    print(f"  {label:44s} is_relative={relative!s:5s} whole={whole is not None!s:5s} published={published!r}")

print("\n=== 4. NFKC is the identity on every character the link allowlist admits ===")
UNSTABLE = sorted(c for c in text_hygiene.LINK_COMPONENT_ALLOWED if unicodedata.normalize("NFKC", c) != c)
print(f"  admitted characters: {len(text_hygiene.LINK_COMPONENT_ALLOWED)}   NFKC-unstable among them: {len(UNSTABLE)}")
print(f"  so a link this gate returns is already NFKC-stable: {not UNSTABLE}")

print("\n=== 5. confusables the LEAF allowlist admits and NFKC still changes ===")
for name, probe in (
    ("fullwidth n (Ll, admitted)", "ca\uff4eon.blend"),
    ("fi ligature (Ll, admitted)", "\ufb01nal.blend"),
    ("roman numeral twelve (Nl)", "shot\u216b.blend"),
    ("plain ascii, must survive", "canon.blend"),
):
    category = unicodedata.category(next((c for c in probe if unicodedata.normalize("NFKC", c) != c), "a"))
    published = text_hygiene.client_safe_leaf(f"/shots/{probe}")
    verdict = "REDUCED" if published == text_hygiene.UNNAMEABLE else "published"
    print(f"  {name:30s} {category}  confusable={text_hygiene.is_confusable(probe)!s:5s} -> {verdict}: {published!r}")

# The homoglyph is the one non-ASCII character in each probe below.
ASCII_MAX = 0x7F

print("\n=== 6. the confusables is_confusable does NOT catch: NFKC leaves them alone ===")
# Each probe is `canon.blend` with exactly one ASCII letter replaced by a
# homoglyph, written out rather than computed: a computed substitution silently
# produced an unsubstituted probe, which reads as evidence and is not.
for rendered_as, probe in (
    ("cyrillic small a", "c\u0430non.blend"),
    ("cyrillic small o", "can\u043en.blend"),
    ("cyrillic small ie", "canon-sc\u0435ne.blend"),
    ("cyrillic small es", "\u0441anon.blend"),
    ("greek small omicron", "can\u03bfn.blend"),
    ("cherokee letter a", "c\u13a0non.blend"),
):
    character = next(c for c in probe if ord(c) > ASCII_MAX)
    published = text_hygiene.client_safe_leaf(f"/shots/{probe}")
    print(
        f"  U+{ord(character):04X} {unicodedata.category(character)} {rendered_as:22s} "
        f"confusable={text_hygiene.is_confusable(probe)!s:5s} -> published={published!r}"
    )
print("  so `renders as something other than what it is` is FALSE of this function;")
print("  its scope is the COMPATIBILITY class, which is what its docstring now says.")

print("\n=== 7. NFKC-against-NFC: a canonically decomposed name is a name, not a disguise ===")
for label, probe in (
    ("NFD (what macOS produced)", "cafe\u0301.blend"),
    ("NFC (the same text)", "caf\u00e9.blend"),
    ("compatibility disguise, still refused", "ca\uff4eon.blend"),
):
    published = text_hygiene.client_safe_leaf(f"/shots/{probe}")
    verdict = "REDUCED" if published == text_hygiene.UNNAMEABLE else "published"
    print(f"  {label:38s} confusable={text_hygiene.is_confusable(probe)!s:5s} -> {verdict}: {published!r}")

print("\n=== 8. what composing first costs: NFKC(x) == NFKC(NFC(x)), and the set that flipped ===")
# Two different questions, and section 8 used to print the first and draw the
# second's conclusion. `NFKC(x) == NFKC(NFC(x))` is a property of the *NFKC
# form* - the folded results agree - and it is true. `is_confusable` does not
# compare NFKC forms: it compares NFKC against NFC. So the identity above says
# nothing about which code points the predicate's answer changed for, and the
# headline it carried ("composing first widens nothing") was a conclusion the
# measurement did not reach. The flip set below is that second question,
# measured.
checked = 0
disagreements: list[str] = []
flipped: list[int] = []
flipped_admissible: list[int] = []
flipped_singleton_canonical: list[int] = []
for code_point in range(0x110000):
    character = chr(code_point)
    if unicodedata.category(character) == "Cn":
        continue
    checked += 1
    folded = unicodedata.normalize("NFKC", character)
    composed = unicodedata.normalize("NFC", character)
    if folded != unicodedata.normalize("NFKC", composed):
        disagreements.append(f"U+{code_point:04X}")
    # The predicate before the NFC change was `NFKC(x) != x`; it is now
    # `NFKC(x) != NFC(x)`. A code point flips when the first said "confusable"
    # and the second says "not".
    if folded != character and folded == composed:
        flipped.append(code_point)
        if text_hygiene._is_admissible_leaf(character):
            flipped_admissible.append(code_point)
            decomposition = unicodedata.decomposition(character)
            if decomposition and not decomposition.startswith("<") and len(decomposition.split()) == 1:
                flipped_singleton_canonical.append(code_point)
print(f"  assigned code points checked: {checked}   NFKC-form disagreements: {len(disagreements)}  {disagreements[:8]}")
print(f"  code points the NFC change newly ADMITS: {len(flipped)}")
print(f"    of those, admissible as a leaf (so actually publishable): {len(flipped_admissible)}")
print(f"    of those, a singleton canonical decomposition: {len(flipped_singleton_canonical)}")
print("  the two worked cases - a canonical singleton IS a confusable pair (UTS #39):")
for code_point in (0x212B, 0x2126):
    character = chr(code_point)
    print(
        f"    U+{code_point:04X} {unicodedata.name(character):22s} "
        f"decomposes to U+{unicodedata.decomposition(character)}  "
        f"was={unicodedata.normalize('NFKC', character) != character!s:5s} "
        f"now={text_hygiene.is_confusable(character)!s:5s} "
        f"-> {text_hygiene.client_safe_leaf(f'/shots/{character}.blend')!r}"
    )
print("  so the NFC change is a WIDENING, not a no-op: it trades a false positive on")
print("  NFD `cafe\\u0301.blend` for these canonical homoglyphs, which join the")
print("  canonical-homoglyph class section 6 already measures. Bounded to single code")
print("  points: this loop asks the question one character at a time.")
