from django.utils.translation import gettext_lazy
from rest_framework import status
from rest_framework.exceptions import APIException


class BusinessError(APIException):
    """Нарушение бизнес-правила -> 400 {"detail": "..."}."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = gettext_lazy("Операция невозможна.")
    default_code = "business_error"
