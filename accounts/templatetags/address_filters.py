from django import template

register = template.Library()

@register.filter
def before_comma(value):
    """Return the part of the string before the first comma."""
    if not value:
        return ""
    return value.split(",")[0].strip()
