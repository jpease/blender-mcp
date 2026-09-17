r"""
Enumerate the characters that could slip a path separator or a disguise past the addon.

Needs no Blender and no network::

    .venv/bin/python scripts/text_hygiene_enumeration.py

A homoglyph blocklist cannot say what it misses; one loop over Unicode can, and can
be rerun for a new Unicode version. Sections:

1. Code points outside `UNSAFE_CATEGORIES` whose NFKC form contains `/`, `\\` or
   `:`. A category filter passes them; a consumer that normalises sees a separator.
2. What `client_safe_leaf` does with each of them.
3. What `safe_relative_link` does with link shapes that defeat a blocklist.
4. Whether NFKC leaves every character `LINK_COMPONENT_ALLOWED` admits unchanged; if
   so, `safe_relative_link` needs no normalisation pass.
5. Confusables the leaf allowlist admits but NFKC changes, which only `is_confusable`
   catches.
6. Homoglyphs NFKC leaves alone, which `is_confusable` misses.
7. That a decomposed (NFD) name is published, because the check compares NFKC with NFC.
8. What comparing with NFC gives up: the code points it newly admits, how many of
   them `_is_admissible_leaf` would publish, and U+212B and U+2126 as examples.
"""

import importlib.util
import pathlib
import unicodedata

# Loaded by path because the addon package's `__init__.py` imports `bpy`. That works
# only while `text_hygiene.py` imports nothing from its own package.
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
    "literal ..": "//../../clients/acme/canon.blend",
    "U+FE68, NFKC -> backslash": "//..\ufe68..\ufe68clients\ufe68acme\ufe68canon.blend",
    "U+29F8 BIG SOLIDUS": "//..\u29f8..\u29f8clients\u29f8acme\u29f8canon.blend",
    "no homoglyph at all, rooted //": "///Users/victim/clients/acme-merger/lib/canon.blend",
    "ZWSP hides .. from a raw gate": "//.\u200b./.\u200b./clients/acme/canon.blend",
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
# Each probe is `canon.blend` with one letter swapped for a homoglyph. Written out,
# because a computed swap can silently leave the probe unchanged and still look valid.
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
# `NFKC(x) == NFKC(NFC(x))` compares folded forms, but `is_confusable` compares NFKC
# with NFC, so only the flip set shows which code points' answers changed.
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
    # Flipped: confusable under the old test `NFKC(x) != x`, but not under the
    # current `NFKC(x) != NFC(x)`.
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
