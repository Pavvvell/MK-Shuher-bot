"""Safe service-error explanations; never interpret user messages."""
from openai import APIStatusError


def quota_notice(exc, *, owner=False):
    if not isinstance(exc, APIStatusError):
        return None
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body)
    if not isinstance(error, dict):
        return None
    code = error.get("code") or getattr(exc, "code", None)
    if code == "credit_balance_exhausted":
        cause = "Шухер тимчасово не може відповісти: закінчився баланс OpenAI API."
        action = "Власнику потрібно поповнити баланс API."
    elif code == "insufficient_quota" or error.get("type") == "insufficient_quota":
        cause = "Шухер тимчасово не може відповісти: OpenAI API повідомив про вичерпаний бюджет або квоту."
        action = "Власнику потрібно перевірити баланс і ліміти API."
    else:
        return None
    if owner:
        action += " https://platform.openai.com/settings/organization/billing/"
    return cause + "\n" + action + "\nПовторення запиту до відновлення доступу не допоможе."
