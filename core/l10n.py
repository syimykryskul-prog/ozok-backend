"""Двуязычные справочники OZOK: поля `<field>` (ru) + `<field>_ky`.

Модель-справочник объявляет `*_ky` поля; сериализатор с BilingualMixin отдаёт локализованное значение
в обычном поле, а также `<field>_ru` и `<field>_ky` для форм настроек; запись принимает оба варианта."""
from django.utils import translation
from rest_framework import serializers


def is_ky():
    return (translation.get_language() or "ru").startswith("ky")


def loc(obj, field):
    """Значение поля на языке запроса; пустой перевод -> ru. Для None возвращает None."""
    if obj is None:
        return None
    base = getattr(obj, field)
    if is_ky():
        ky = getattr(obj, f"{field}_ky", "") or ""
        if ky:
            return ky
    return base


def loc_text(ru, ky):
    return ky if (is_ky() and ky) else ru


class L10nField(serializers.Field):
    """Только чтение: `source="product.name"` -> локализованное значение (`product.name_ky` на языке ky)."""

    def __init__(self, **kwargs):
        kwargs["read_only"] = True
        super().__init__(**kwargs)

    def get_attribute(self, instance):
        obj = instance
        for attr in self.source_attrs[:-1]:
            obj = getattr(obj, attr, None)
            if obj is None:
                if self.default is not serializers.empty:
                    return self.default
                return None
        return loc(obj, self.source_attrs[-1])

    def to_representation(self, value):
        return value


class BilingualMixin:
    """ModelSerializer: `bilingual = ("name", ...)`.

    Вывод: поле = локализованное значение, `<f>_ru` = базовое, `<f>_ky` = перевод.
    Ввод: `<f>` или `<f>_ru` (русский), `<f>_ky` (кыргызский)."""
    bilingual = ()

    def get_fields(self):
        fields = super().get_fields()
        for f in self.bilingual:
            src = fields.get(f)
            max_length = getattr(src, "max_length", None)
            fields[f"{f}_ky"] = serializers.CharField(required=False, allow_blank=True, max_length=max_length)
        return fields

    def to_internal_value(self, data):
        if self.bilingual and hasattr(data, "get"):
            data = data.copy() if hasattr(data, "copy") else dict(data)
            for f in self.bilingual:
                if f"{f}_ru" in data and f not in data:
                    data[f] = data[f"{f}_ru"]
                data.pop(f"{f}_ru", None)
        return super().to_internal_value(data)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for f in self.bilingual:
            ru = getattr(instance, f)
            ky = getattr(instance, f"{f}_ky", "") or ""
            data[f"{f}_ru"] = ru
            data[f"{f}_ky"] = ky
            data[f] = ky if (is_ky() and ky) else ru
        return data
