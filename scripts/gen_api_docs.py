#!/usr/bin/env python3
"""Generate `docs/API.md` from the SDK's actual public `bot.*`/`sdk.*`/
`install.*` surface -- ticket #8
(https://github.com/blokboy/sc2-sdk/issues/8).

Why AST, not `inspect`/import
------------------------------
This walks each documented module's *source* with the stdlib `ast` module
rather than importing it and reflecting via `inspect`. Two reasons:

  1. The CI check this backs must NOT need the SC2 client, Docker, or even
     this project's own runtime dependencies (`burnysc2`, `mcp`) installed --
     it's meant to be a fast, static, docs-only gate, not something that
     waits on a ~10GB Docker build the way `.github/workflows/integration.yml`
     does. Parsing source with `ast` needs nothing beyond the Python
     standard library, so the check workflow doesn't even run `pip install`.
  2. It's a more literal reading of "generated from the SDK's actual public
     surface" -- it reflects exactly what the source *declares*, including
     the project's existing `#:` doc-comment convention on constants and
     dataclass fields (see e.g. `sdk.play.DEFAULT_MAP`, `sdk.outcomes`'s
     dataclasses), which plain `inspect`-based reflection can't see at all
     (comments aren't part of any runtime object).

What counts as "public"
-------------------------
For each module below, only names *defined in that module's own source*
(not merely imported into it) and not starting with `_` are documented --
the same "defined here, not just imported" rule
`sdk.script_runner.load_bot_class` already uses to find a bot script's own
class, applied here to every top-level class/function/constant, plus (for
classes) every method/property/field defined directly in the class body.
`__init__` is treated as public when a class defines its own (it's the
constructor signature, i.e. part of the public surface) even though its
name starts with `_`; every other dunder/underscore-prefixed name is
treated as a private implementation detail and skipped, matching the
convention this codebase already follows throughout (see e.g. `bot.py`'s
`_require_live`/`_advance`/`_resolve_units`).

Determinism
------------
Output depends only on the literal source text of the modules below (via
`ast.unparse`, which is a pure function of the parsed source) -- no
timestamps, no environment-dependent values, no import side effects. Two
runs against the same source produce byte-identical output; the CI check
(`--check`) relies on exactly that. `ast.unparse`'s exact formatting can
differ slightly across Python minor versions (e.g. string-quote choices in
edge cases); `.github/workflows/docs.yml` pins one Python version so CI's
own runs stay self-consistent regardless of what a contributor's local
interpreter version renders.

Usage
------
    python scripts/gen_api_docs.py            # regenerate docs/API.md in place
    python scripts/gen_api_docs.py --check     # exit 1 (with a diff) if docs/API.md is stale
"""

from __future__ import annotations

import argparse
import ast
import copy
import difflib
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "docs" / "API.md"

#: Every module whose top-level surface gets documented, in the order it
#: appears in the generated file. This is deliberately wider than just
#: `sdk.bot`/`sdk.observation`/`sdk.outcomes`: `sdk.play`/`sdk.runtime`/
#: `sdk.script_runner`/`sdk.mcp_server` are the other play-modality entry
#: points the README documents (raw connect-and-play, the run_bot_vs_
#: builtin_ai generalization, the autonomous bot-script runtime, and the
#: MCP execute_code server), and `install.*` is the client-acquisition
#: surface (ticket #2) an agent calls before any of the above works at
#: all -- all of it is part of "the SDK's actual public surface", not just
#: the verified-action tier.
MODULES: tuple[tuple[str, str], ...] = (
    ("sdk.bot", "src/sdk/bot.py"),
    ("sdk.observation", "src/sdk/observation.py"),
    ("sdk.outcomes", "src/sdk/outcomes.py"),
    ("sdk.play", "src/sdk/play.py"),
    ("sdk.runtime", "src/sdk/runtime.py"),
    ("sdk.script_runner", "src/sdk/script_runner.py"),
    ("sdk.selfplay", "src/sdk/selfplay.py"),
    ("sdk.mcp_server", "src/sdk/mcp_server.py"),
    ("sdk.join", "src/sdk/join.py"),
    ("install.cli", "src/install/cli.py"),
    ("install.battlenet", "src/install/battlenet.py"),
    ("install.headless", "src/install/headless.py"),
    ("install.maps", "src/install/maps.py"),
    ("install.paths", "src/install/paths.py"),
)

_HEADER = """\
<!--
GENERATED FILE -- do not hand-edit.

Produced by `scripts/gen_api_docs.py` from the actual source of the modules
listed there (via Python's `ast`, not hand-maintained prose) -- see that
script's module docstring for exactly what "public surface" means here and
why AST rather than `inspect`/import.

Regenerate after any change to a documented module's public surface:

    python scripts/gen_api_docs.py

A CI check (`.github/workflows/docs.yml`) runs `python scripts/gen_api_docs.py
--check` on every push/PR and fails if this file doesn't match what that
command would produce -- i.e. this file cannot silently drift from the real
`bot.*`/`sdk.*`/`install.*` surface.
-->

# sc2-sdk API reference

Generated reference for every public class, function, and constant `sc2-sdk`
itself defines across `sdk.*` (the verified `bot.*` action/observation layer,
raw `sdk.*` passthrough, and the three play-modality entry points: raw
connect-and-play, the MCP `execute_code` server, and the autonomous
bot-script runtime) and `install.*` (client detection/install + map pool
sync). It does **not** include python-sc2's own API (`sc2.*`) -- only this
project's surface.

For narrative usage (how these fit together, worked examples, how to run
things), see the root [`README.md`](../README.md) and
[`learnings/README.md`](../learnings/README.md).
"""


# -- source-level helpers ----------------------------------------------------


def _leading_doc_comment(lines: list[str], first_lineno: int) -> str | None:
    """Collect a contiguous run of `#:` doc-comment lines immediately above
    `first_lineno` (1-indexed), the project's existing convention for
    documenting constants and dataclass fields (see e.g. `sdk.play`'s
    `DEFAULT_MAP`, `sdk.observation`'s `UnitSnapshot`). Returns None if no
    such comment is present directly above."""
    i = first_lineno - 2  # 0-indexed line just above first_lineno
    collected: list[str] = []
    while i >= 0:
        stripped = lines[i].strip()
        if stripped.startswith("#:"):
            collected.append(stripped[2:].strip())
            i -= 1
        else:
            break
    if not collected:
        return None
    return " ".join(reversed(collected))


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[list[str], str]:
    """Return (decorator source lines, a `def name(args) -> ret` signature
    string) for a function/method node, built by unparsing a body-stripped
    clone of the node -- i.e. reconstructed straight from the source's own
    argument list and annotations, not a runtime-reflected signature."""
    decorators = [f"@{ast.unparse(d)}" for d in node.decorator_list]
    clone = copy.deepcopy(node)
    clone.decorator_list = []
    clone.body = [ast.Pass()]
    unparsed = ast.unparse(clone)
    def_line = unparsed.splitlines()[0]
    sig = def_line[:-1] if def_line.endswith(":") else def_line
    return decorators, sig


@dataclass
class FunctionDoc:
    decorators: list[str]
    signature: str
    docstring: str | None


@dataclass
class FieldDoc:
    name: str
    annotation: str | None
    default: str | None
    comment: str | None


@dataclass
class ClassDoc:
    name: str
    docstring: str | None
    fields: list[FieldDoc]
    methods: list[tuple[str, FunctionDoc]]  # (qualified "Class.method" name, doc)


@dataclass
class ConstantDoc:
    name: str
    annotation: str | None
    value: str
    comment: str | None


@dataclass
class ModuleDoc:
    dotted_name: str
    rel_path: str
    docstring: str | None
    # Top-level entries in source order; each is one of ClassDoc / FunctionDoc
    # (paired with its name) / ConstantDoc.
    entries: list[tuple[str, object]]  # ("class"|"function"|"constant", doc)


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _class_members(node: ast.ClassDef, lines: list[str]) -> tuple[list[FieldDoc], list[tuple[str, FunctionDoc]]]:
    fields: list[FieldDoc] = []
    methods: list[tuple[str, FunctionDoc]] = []
    for member in node.body:
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if member.name != "__init__" and not _is_public(member.name):
                continue
            decorators, sig = _signature(member)
            methods.append((member.name, FunctionDoc(decorators, sig, ast.get_docstring(member))))
        elif isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
            if not _is_public(member.target.id):
                continue
            first_lineno = member.lineno
            comment = _leading_doc_comment(lines, first_lineno)
            fields.append(
                FieldDoc(
                    name=member.target.id,
                    annotation=ast.unparse(member.annotation),
                    default=ast.unparse(member.value) if member.value is not None else None,
                    comment=comment,
                )
            )
        elif isinstance(member, ast.Assign) and len(member.targets) == 1 and isinstance(member.targets[0], ast.Name):
            name = member.targets[0].id
            if not _is_public(name):
                continue
            comment = _leading_doc_comment(lines, member.lineno)
            fields.append(FieldDoc(name=name, annotation=None, default=ast.unparse(member.value), comment=comment))
    return fields, methods


def parse_module(dotted_name: str, rel_path: str) -> ModuleDoc:
    path = REPO_ROOT / rel_path
    source = path.read_text()
    lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))
    module_docstring = ast.get_docstring(tree)

    entries: list[tuple[str, object]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if not _is_public(node.name):
                continue
            fields, methods = _class_members(node, lines)
            entries.append(("class", ClassDoc(node.name, ast.get_docstring(node), fields, methods)))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _is_public(node.name):
                continue
            decorators, sig = _signature(node)
            entries.append((node.name, FunctionDoc(decorators, sig, ast.get_docstring(node))))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not _is_public(node.target.id):
                continue
            comment = _leading_doc_comment(lines, node.lineno)
            entries.append(
                (
                    node.target.id,
                    ConstantDoc(
                        node.target.id,
                        ast.unparse(node.annotation),
                        ast.unparse(node.value) if node.value is not None else "...",
                        comment,
                    ),
                )
            )
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if not _is_public(name):
                continue
            comment = _leading_doc_comment(lines, node.lineno)
            entries.append((name, ConstantDoc(name, None, ast.unparse(node.value), comment)))

    return ModuleDoc(dotted_name=dotted_name, rel_path=rel_path, docstring=module_docstring, entries=entries)


# -- markdown rendering -------------------------------------------------------


def _render_function(heading_level: int, qualified_name: str, doc: FunctionDoc) -> list[str]:
    out = ["#" * heading_level + f" `{qualified_name}`", ""]
    if doc.decorators:
        out += ["```python", *doc.decorators, doc.signature, "```", ""]
    else:
        out += ["```python", doc.signature, "```", ""]
    if doc.docstring:
        out += [doc.docstring, ""]
    return out


def _render_field(field: FieldDoc) -> str:
    type_part = f": {field.annotation}" if field.annotation else ""
    default_part = f" = {field.default}" if field.default is not None else ""
    line = f"- `{field.name}{type_part}{default_part}`"
    if field.comment:
        line += f" -- {field.comment}"
    return line


def _render_class(heading_level: int, doc: ClassDoc) -> list[str]:
    out = ["#" * heading_level + f" class `{doc.name}`", ""]
    if doc.docstring:
        out += [doc.docstring, ""]
    if doc.fields:
        out += ["**Fields:**", ""]
        out += [_render_field(f) for f in doc.fields]
        out += [""]
    for method_name, method_doc in doc.methods:
        qualified = f"{doc.name}.{method_name}"
        out += _render_function(heading_level + 1, qualified, method_doc)
    return out


def _render_constant(heading_level: int, doc: ConstantDoc) -> list[str]:
    out = ["#" * heading_level + f" `{doc.name}`", ""]
    type_part = f": {doc.annotation}" if doc.annotation else ""
    out += ["```python", f"{doc.name}{type_part} = {doc.value}", "```", ""]
    if doc.comment:
        out += [doc.comment, ""]
    return out


def render_module(module: ModuleDoc) -> list[str]:
    out = [f"## `{module.dotted_name}`", "", f"*Source: [`{module.rel_path}`](../{module.rel_path})*", ""]
    if module.docstring:
        out += [module.docstring, ""]
    for name, entry in module.entries:
        if isinstance(entry, ClassDoc):
            out += _render_class(3, entry)
        elif isinstance(entry, FunctionDoc):
            out += _render_function(3, name, entry)
        elif isinstance(entry, ConstantDoc):
            out += _render_constant(3, entry)
    return out


def generate() -> str:
    lines: list[str] = [_HEADER, ""]
    lines += ["## Contents", ""]
    for dotted_name, _rel_path in MODULES:
        anchor = dotted_name.replace(".", "").replace("_", "").lower()
        lines.append(f"- [`{dotted_name}`](#{anchor})")
    lines.append("")
    for dotted_name, rel_path in MODULES:
        module = parse_module(dotted_name, rel_path)
        lines += render_module(module)
    text = "\n".join(lines).rstrip() + "\n"
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Don't write docs/API.md; exit 1 (printing a diff) if it's not already up to date.",
    )
    args = parser.parse_args(argv)

    generated = generate()

    if args.check:
        current = OUTPUT_PATH.read_text() if OUTPUT_PATH.is_file() else ""
        if current == generated:
            print(f"[gen_api_docs] {OUTPUT_PATH.relative_to(REPO_ROOT)} is up to date.")
            return 0
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            generated.splitlines(keepends=True),
            fromfile=f"{OUTPUT_PATH.relative_to(REPO_ROOT)} (committed)",
            tofile=f"{OUTPUT_PATH.relative_to(REPO_ROOT)} (regenerated)",
        )
        sys.stdout.writelines(diff)
        print(
            f"\n[gen_api_docs] {OUTPUT_PATH.relative_to(REPO_ROOT)} is STALE. "
            "Run `python scripts/gen_api_docs.py` and commit the result.",
            file=sys.stderr,
        )
        return 1

    OUTPUT_PATH.write_text(generated)
    print(f"[gen_api_docs] Wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
