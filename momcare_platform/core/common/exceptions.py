"""Project-wide DRF exception handling.

Ensures bad input always surfaces as a 4xx client error, never a 500. DRF's
default handler does not understand Django's own ``ValidationError``. We
translate those into DRF validation errors so the response is a clean 400.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.serializers import as_serializer_error
from rest_framework.views import exception_handler as drf_default_handler


def drf_exception_handler(exc, context):
    if isinstance(exc, DjangoValidationError):
        exc = DRFValidationError(detail=as_serializer_error(exc))
    return drf_default_handler(exc, context)
