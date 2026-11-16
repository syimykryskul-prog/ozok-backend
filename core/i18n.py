"""Язык ответа: Accept-Language -> User.language (аутентифицированный) -> ru."""
from django.conf import settings
from django.utils import translation
from django.utils.translation.trans_real import parse_accept_lang_header


def header_language(request):
    """Первый поддерживаемый язык из Accept-Language или None."""
    header = request.META.get("HTTP_ACCEPT_LANGUAGE", "")
    if not header:
        return None
    try:
        parsed = parse_accept_lang_header(header)
    except Exception:
        return None
    for lang, q in parsed:
        if lang == "*" or q <= 0:
            continue
        try:
            return translation.get_supported_language_variant(lang)
        except LookupError:
            continue
    return None


def activate(request, lang):
    translation.activate(lang)
    request.LANGUAGE_CODE = translation.get_language()


DEFAULT_LANGUAGE = settings.LANGUAGE_CODE
