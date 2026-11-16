"""Компилирует locale/*/LC_MESSAGES/django.po -> django.mo через babel (GNU gettext не нужен)."""
from pathlib import Path

from babel.messages.pofile import read_po
from babel.messages.mofile import write_mo
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Скомпилировать .po в .mo (babel) для всех языков из locale/."

    def handle(self, *args, **opts):
        base = Path(settings.BASE_DIR) / "locale"
        pos = sorted(base.glob("*/LC_MESSAGES/django.po"))
        if not pos:
            raise CommandError(f"Нет .po файлов в {base}")
        for po in pos:
            lang = po.parent.parent.name
            with po.open("rb") as f:
                catalog = read_po(f, locale=lang)
            total = sum(1 for m in catalog if m.id)
            done = sum(1 for m in catalog if m.id and m.string)
            mo = po.with_suffix(".mo")
            with mo.open("wb") as f:
                write_mo(f, catalog)
            self.stdout.write(f"{lang}: {done}/{total} строк переведено -> {mo.name}")
