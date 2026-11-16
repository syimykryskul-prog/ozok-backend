from rest_framework_simplejwt.authentication import JWTAuthentication

from . import i18n


class LanguageJWTAuthentication(JWTAuthentication):
    """JWT + язык: если Accept-Language не задан (или не поддерживается), берём User.language."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            user = result[0]
            lang = getattr(user, "language", None)
            if lang and not getattr(request, "_lang_from_header", False):
                i18n.activate(request, lang)
        return result
