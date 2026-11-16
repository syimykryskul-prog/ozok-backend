"""Собирает msgid из вызовов gettext()/gettext_lazy() в locale/ky/LC_MESSAGES/django.po (GNU gettext не нужен).

Существующие переводы сохраняются; новые строки добавляются с пустым msgstr, исчезнувшие — удаляются."""
import ast
import re
from pathlib import Path

from babel.messages import Catalog
from babel.messages.pofile import read_po, write_po
from django.conf import settings
from django.core.management.base import BaseCommand

CYRILLIC = re.compile("[А-Яа-я]")
NAMES = {"gettext", "gettext_lazy", "_"}
SKIP_DIRS = {".venv", "migrations", "tests", "node_modules", "__pycache__", "staticfiles", "media"}


def collect(root: Path):
    found = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if SKIP_DIRS & set(rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in NAMES
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                found.setdefault(node.args[0].value, []).append((rel.as_posix(), node.lineno))
    return found


class Command(BaseCommand):
    help = "Обновить locale/<lang>/LC_MESSAGES/django.po по строкам из кода (без GNU gettext)."

    def add_arguments(self, parser):
        parser.add_argument("--lang", default="ky")

    def handle(self, *args, lang="ky", **opts):
        root = Path(settings.BASE_DIR)
        po_path = root / "locale" / lang / "LC_MESSAGES" / "django.po"
        po_path.parent.mkdir(parents=True, exist_ok=True)
        old = {}
        if po_path.exists():
            with po_path.open("rb") as f:
                old = {m.id: m.string for m in read_po(f, locale=lang) if m.id}
        cat = Catalog(locale=lang, project="OZOK backend", charset="utf-8", fuzzy=False)
        found = collect(root)
        for msgid, locs in sorted(found.items()):
            cat.add(msgid, string=old.get(msgid, ""), locations=locs[:3])
        # английские msgid (встроенные сообщения DRF/simplejwt, добавленные вручную) сохраняем как есть
        for msgid, string in old.items():
            if msgid not in found and not CYRILLIC.search(msgid):
                cat.add(msgid, string=string)
        with po_path.open("wb") as f:
            write_po(f, cat, width=0, include_previous=False)
        missing = sum(1 for m in cat if m.id and not m.string)
        self.stdout.write(f"{po_path}: {sum(1 for m in cat if m.id)} строк, без перевода: {missing}")
