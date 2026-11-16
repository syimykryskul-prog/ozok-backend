from django.core.management.base import BaseCommand, CommandError

from core.exceptions import BusinessError
from integrations.simulator import simulate
from network.models import Product


class Command(BaseCommand):
    help = "Сгенерировать события интеграции продукта (payment.succeeded) для проверки"

    def add_arguments(self, parser):
        parser.add_argument("--product", required=True, help="код продукта (сферы)")
        parser.add_argument("--count", type=int, default=5)

    def handle(self, *args, **opts):
        try:
            product = Product.objects.get(code=opts["product"])
        except Product.DoesNotExist:
            raise CommandError(f"Продукт «{opts['product']}» не найден.")
        try:
            results = simulate(product, opts["count"])
        except BusinessError as e:
            raise CommandError(str(e.detail))
        counts = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        self.stdout.write(f"Отправлено событий: {len(results)}; итог: {counts}")
