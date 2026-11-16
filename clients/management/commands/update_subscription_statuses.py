from django.core.management.base import BaseCommand

from clients import subscriptions


class Command(BaseCommand):
    help = "Пересчитать статусы подписок по paid_until, льготному периоду и сроку ограничения (§10.3)."

    def handle(self, *args, **opts):
        changed = subscriptions.update_all()
        total = sum(changed.values())
        self.stdout.write(f"Подписок изменено: {total}. " + ", ".join(f"{k}: {v}" for k, v in sorted(changed.items())))
