#!/usr/bin/env python3
"""Every message must exist in both languages.

Messages are written in French and translated through a catalogue: English in
sweeper/i18n_en.py for the server, I18N_EN in webui/i18n.js for the browser.
A message missing from its catalogue would silently show up in French to an
English-speaking user; this test turns that into a failure. It also checks that
placeholders survive translation, and hunts for French text that was never
wrapped in t() at all.

    python3 test_i18n.py
"""

from __future__ import annotations

import ast
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from sweeper.i18n_en import EN as PY_EN  # noqa: E402

PY_FILES = [ROOT / "sweeper.py", *sorted((ROOT / "sweeper").glob("*.py"))]
PY_FILES = [p for p in PY_FILES if p.name not in ("i18n.py", "i18n_en.py")]
FRENCH = re.compile(r"[éèàùâêîôûçÉÀ]|\b(le|la|les|des|du|une|pas|dans|pour|sans|avec|jamais|aucun|aucune)\b")
PLACEHOLDER = re.compile(r"\{(\w+)[^{}]*\}")

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    if not condition:
        failures.append(label)


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


# ---------------------------------------------------------------- Python

def docstring_nodes(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def is_t_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t"


py_keys: dict[str, str] = {}
untranslated: list[str] = []
for path in PY_FILES:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = docstring_nodes(tree)
    # module-level constants may be translated by name: t(DESCRIPTION)
    constants = {
        target.id: node.value.value
        for node in tree.body if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets if isinstance(target, ast.Name)
    }
    parent: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    for node in ast.walk(tree):
        if not is_t_call(node):
            continue
        if not node.args:
            continue
        first = node.args[0]
        literals = []
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            literals = [first.value]
        elif isinstance(first, ast.Name) and first.id in constants:
            literals = [constants[first.id]]
        elif isinstance(first, ast.IfExp):
            literals = [b.value for b in (first.body, first.orelse)
                        if isinstance(b, ast.Constant) and isinstance(b.value, str)]
        else:
            failures.append(f"{path.name}:{node.lineno}: t() needs a literal message")
        for text in literals:
            py_keys[text] = f"{path.name}:{node.lineno}"
            check(not isinstance(first, ast.JoinedStr), f"{path.name}:{node.lineno}: f-string inside t()")

    def inside_t(node: ast.AST) -> bool:
        current = parent.get(id(node))
        while current is not None:
            if is_t_call(current):
                return True
            current = parent.get(id(current))
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docs and not isinstance(parent.get(id(node)), ast.JoinedStr)):
            text = node.value
        else:
            continue
        if " " not in text.strip() or not FRENCH.search(text) or inside_t(node):
            continue
        holder = parent.get(id(node))
        if isinstance(holder, ast.Dict) and node in holder.keys:
            continue  # JSON field names stay as they are
        if (isinstance(holder, ast.Call) and isinstance(holder.func, ast.Name)
                and holder.func.id == "Reason"):
            continue
        if text in constants.values():
            continue  # a constant translated by name
        untranslated.append(f"{path.name}:{node.lineno}: {text[:70]!r}")

for text, where in sorted(py_keys.items(), key=lambda kv: kv[1]):
    if text in ("{}",):
        continue
    check(text in PY_EN, f"missing English for {where}: {text!r}")
    if text in PY_EN:
        check(placeholders(text) == placeholders(PY_EN[text]),
              f"placeholders differ for {text!r}: {sorted(placeholders(text))} vs {sorted(placeholders(PY_EN[text]))}")
        check(not FRENCH.search(PY_EN[text]) or "«" in text, f"English still looks French: {PY_EN[text]!r}")
for line in untranslated:
    failures.append(f"French text outside t(): {line}")
for text in sorted(set(PY_EN) - set(py_keys)):
    failures.append(f"unused English entry (server): {text[:70]!r}")


# ---------------------------------------------------------------- browser

js = (ROOT / "webui/i18n.js").read_text(encoding="utf-8")
match = re.search(r"const I18N_EN = (\{.*?\n\});", js, re.S)
check(match is not None, "webui/i18n.js: I18N_EN not found")
JS_EN: dict[str, str] = json.loads(match.group(1)) if match else {}

STRING = r"""'((?:\\.|[^'\\])*)'|"((?:\\.|[^"\\])*)"|`((?:\\.|[^`\\$])*)`"""
T_CALL = re.compile(r"(?<![\w.$])t\(\s*(?:" + STRING + r")")


def js_unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), text)


def strip_comments(source: str) -> str:
    """Blank out // and /* */ comments, leaving strings alone (line numbers kept)."""
    out, i, quote = [], 0, None
    while i < len(source):
        ch = source[i]
        if quote:
            out.append(ch)
            if ch == "\\":
                out.append(source[i + 1:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
            out.append(ch)
        elif source.startswith("//", i):
            end = source.find("\n", i)
            end = len(source) if end == -1 else end
            i = end
            continue
        elif source.startswith("/*", i):
            end = source.find("*/", i) + 2
            out.append("\n" * source.count("\n", i, end))
            i = end
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


js_keys: dict[str, str] = {}
for name in ("app.js", "login.js"):
    source = strip_comments((ROOT / "webui" / name).read_text(encoding="utf-8"))
    for m in T_CALL.finditer(source):
        text = js_unescape(next(g for g in m.groups() if g is not None))
        js_keys[text] = f"{name}:{source.count(chr(10), 0, m.start()) + 1}"
    # a French literal that never went through t()
    for lineno, code in enumerate(source.splitlines(), 1):
        for m in re.finditer(STRING, code):
            text = next((g for g in m.groups() if g is not None), "")
            before = code[max(0, m.start() - 3):m.start()]
            if "t(" in before or not FRENCH.search(text) or " " not in text:
                continue
            failures.append(f"French text outside t(): {name}:{lineno}: {text[:70]!r}")


class Texts(HTMLParser):
    """Visible text and translatable attributes of a page."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for key, value in attrs:
            if key in ("title", "placeholder", "aria-label") and value:
                self.found.append(" ".join(value.split()))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        text = " ".join(data.split())
        if not self.skip and text and re.search(r"[A-Za-zÀ-ÿ]", text):
            self.found.append(text)


for page in ("index.html", "login.html"):
    parser = Texts()
    parser.feed((ROOT / "webui" / page).read_text(encoding="utf-8"))
    for text in parser.found:
        if text in ("sweeper",) or text.startswith(("~/", "/")):
            continue
        js_keys.setdefault(text, page)

for text, where in sorted(js_keys.items(), key=lambda kv: kv[1]):
    check(text in JS_EN, f"missing English for {where}: {text!r}")
    if text in JS_EN:
        check(placeholders(text) == placeholders(JS_EN[text]),
              f"placeholders differ for {text!r}")
# job kinds arrive from the server as data and go through t(job.type)
JOB_KINDS = ("suppression", "seed-seul", "adoption", "rangement", "planifié")
for text in sorted(set(JS_EN) - set(js_keys)):
    if text in JOB_KINDS:
        continue
    failures.append(f"unused English entry (browser): {text[:70]!r}")


print(f"server: {len(py_keys)} messages · browser: {len(js_keys)} messages")
if failures:
    print(f"{len(failures)} problem(s):")
    for line in failures:
        print("  " + line)
    sys.exit(1)
print("all messages translated")
