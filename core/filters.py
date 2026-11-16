"""Фабрика FilterSet: короткая запись фильтров из раздела 6.2."""
from django_filters import rest_framework as df


def build_filterset(model, exact=(), ids=(), bools=(), date_field=None, **declared):
    """exact — точное совпадение (строки/choices), ids — по id связанного объекта,
    bools — булевы поля, date_field — добавляет date_from/date_to."""
    attrs = {}
    for n in exact:
        attrs[n] = df.CharFilter(field_name=n)
    for n in ids:
        attrs[n] = df.NumberFilter(field_name=f"{n}_id")
    for n in bools:
        attrs[n] = df.BooleanFilter(field_name=n)
    if date_field:
        attrs["date_from"] = df.DateFilter(field_name=date_field, lookup_expr="gte")
        attrs["date_to"] = df.DateFilter(field_name=date_field, lookup_expr="lte")
    attrs.update(declared)
    attrs["Meta"] = type("Meta", (), {"model": model, "fields": []})
    return type(f"{model.__name__}FilterSet", (df.FilterSet,), attrs)


def flag(method):
    """Булев фильтр с кастомным методом (?overdue=true)."""
    return df.BooleanFilter(method=method)


def product_filter(field="product"):
    """?product=<id> или ?product=<code>."""
    def method(qs, name, value):
        if not value:
            return qs
        return qs.filter(**{f"{field}_id": int(value)}) if str(value).isdigit() else qs.filter(**{f"{field}__code": value})
    return df.CharFilter(method=method)
