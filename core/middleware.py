from django.middleware.locale import LocaleMiddleware

from . import audit, i18n


class AuditContextMiddleware:
    """Кладёт текущий запрос в contextvar: сигналы аудита берут из него пользователя, IP и User-Agent."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = audit.set_request(request)
        try:
            return self.get_response(request)
        finally:
            audit.reset_request(token)


class LanguageMiddleware(LocaleMiddleware):
    """LocaleMiddleware без cookie/сессии: язык из Accept-Language; иначе ru.
    Для аутентифицированных запросов без заголовка язык уточняет core.auth.LanguageJWTAuthentication (User.language)."""

    def process_request(self, request):
        lang = i18n.header_language(request)
        request._lang_from_header = lang is not None
        i18n.activate(request, lang or i18n.DEFAULT_LANGUAGE)
