from django.core.management.base import BaseCommand

from network import partners


class Command(BaseCommand):
    help = "Применить запланированные смены статуса и уровня партнёров, у которых наступила дата effective_from."

    def handle(self, *args, **opts):
        n = partners.apply_due_changes()
        self.stdout.write(f"Партнёров обновлено: {n}.")
