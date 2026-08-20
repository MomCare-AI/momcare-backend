SUPPORTED_LANGUAGES = [
    {"code": "en", "name": "English"},
    {"code": "ur", "name": "Urdu"},
]

_SUPPORTED_LANGUAGES_BY_CODE = {lang["code"]: lang for lang in SUPPORTED_LANGUAGES}


def is_supported_language(code: str) -> bool:
    return code in _SUPPORTED_LANGUAGES_BY_CODE
