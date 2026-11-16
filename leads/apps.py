from django.apps import AppConfig


class LeadsConfig(AppConfig):
    name = "leads"

    def ready(self):
        from . import services  # noqa: F401  регистрирует обработчики запросов на изменение
