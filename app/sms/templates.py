"""Outgoing SMS texts. Kept ASCII/GSM-7 (straight apostrophes)."""
from __future__ import annotations

LANG_NAMES = {"dav": "Kidaw'ida", "swh": "Kiswahili", "eng": "English"}


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


def task_sms(code: str, lang: str, candidate: str | None, source: str) -> str:
    if candidate is None:
        return (f'{code} Translate into {lang_name(lang)}:\n"{source}"\n'
                f"Reply: {code} <your translation>. Add SHARE to allow research use.")
    return (f"{code} Rate 1-5 & correct ({lang_name(lang)}):\n\"{candidate}\"\n"
            f"Reply: {code} 5  or  {code} 3 <your version>. "
            f"Add SHARE to allow research use.")


def filled_sms(code: str) -> str:
    return f"Task {code} is filled, thank you. No reply needed."


def late_reply_sms(code: str) -> str:
    return (f"Thanks! Task {code} was already filled by 2 replies, so this reply "
            f"is saved but not paid.")


def too_fast_sms(code: str) -> str:
    return (f"Task {code}: reply came too fast to be a careful check, so it was "
            f"not accepted. Please read the full sentence next time.")


def thanks_sms(code: str, amount: int) -> str:
    return f"Thank you! Task {code} received. KSh {amount} airtime is on its way."


def thanks_pending_sms(code: str) -> str:
    return f"Thank you! Task {code} received."


def opt_in_prompt() -> str:
    return ("Lugha Verify: earn airtime checking translations by SMS. Reply YES to "
            "join, STOP to leave. Each task is paid to the first 2 people who "
            "reply; later replies are saved but not paid.")


def opted_in_sms() -> str:
    return "You're in. Tasks arrive by SMS. First 2 replies per task get paid. STOP to leave."


def opted_out_sms() -> str:
    return "You have left Lugha Verify. You will get no more tasks. Reply YES to rejoin."


def help_sms() -> str:
    return ("Reply <CODE> <1-5> to rate, or <CODE> <1-5> <your version> to correct. "
            "Add SHARE to allow research use. STOP to leave.")


def unknown_code_sms(code: str) -> str:
    return f"We have no open task {code} for you. Check the code. HELP for help."


def parse_error_sms() -> str:
    return "Sorry, we could not read that. " + help_sms()


def not_opted_in_sms() -> str:
    return "You are not signed up. Reply YES to join Lugha Verify."


def closed_sms(code: str) -> str:
    return f"Task {code} is closed, thank you. No reply needed."
