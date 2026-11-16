from django.apps import AppConfig


class ClientsConfig(AppConfig):
    name = "clients"

    def ready(self):
        from . import deals  # noqa: F401  регистрирует обработчик согласования скидок
