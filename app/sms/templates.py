"""Outgoing SMS texts. Kept ASCII/GSM-7 (straight apostrophes).

On a shared shortcode AT only forwards messages that start with our keyword
(AT_KEYWORD), so every text that tells people what to type puts the keyword
first: "Reply: LUGHA K7P2 5". With no keyword the texts are unchanged.
"""
from __future__ import annotations

LANG_NAMES = {"dav": "Kidaw'ida", "swh": "Kiswahili", "eng": "English"}
SHORT = {"eng": "EN", "swh": "SW"}


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


class Templates:
    def __init__(self, keyword: str | None = None):
        self.k = f"{keyword.upper()} " if keyword else ""

    def task_sms(self, code: str, lang: str, candidate: str | None, source: str,
                 source_lang: str = "eng") -> str:
        k = self.k
        src = f'{SHORT.get(source_lang, source_lang.upper())}: "{source}"'
        if candidate is None:
            return (f"{code} Translate into {lang_name(lang)}:\n{src}\n"
                    f"Reply: {k}{code} <your translation>. Add SHARE to allow research use.")
        return (f"{code} Rate 1-5 & correct ({lang_name(lang)}):\n{src}\n"
                f"Machine: \"{candidate}\"\n"
                f"Reply: {k}{code} 5  or  {k}{code} 3 <your version>. "
                f"Add SHARE to allow research use.")

    def filled_sms(self, code: str) -> str:
        return f"Task {code} is filled, thank you. No reply needed."

    def late_reply_sms(self, code: str) -> str:
        return (f"Thanks! Task {code} was already filled by 2 replies, so this reply "
                f"is saved but not paid.")

    def too_fast_sms(self, code: str) -> str:
        return (f"Task {code}: reply came too fast to be a careful check, so it was "
                f"not accepted. Please read the full sentence next time.")

    def thanks_sms(self, code: str, amount: int) -> str:
        return f"Thank you! Task {code} received. KSh {amount} airtime is on its way."

    def thanks_pending_sms(self, code: str) -> str:
        return f"Thank you! Task {code} received."

    def opt_in_prompt(self) -> str:
        k = self.k
        return (f"Lugha Verify: earn airtime checking translations by SMS. Reply {k}YES "
                f"to join, {k}STOP to leave. Each task is paid to the first 2 people "
                "who reply; later replies are saved but not paid.")

    def opted_in_sms(self) -> str:
        return (f"You're in. Tasks arrive by SMS. First 2 replies per task get paid. "
                f"{self.k}STOP to leave.")

    def opted_out_sms(self) -> str:
        return (f"You have left Lugha Verify. You will get no more tasks. "
                f"Reply {self.k}YES to rejoin.")

    def help_sms(self) -> str:
        k = self.k
        return (f"Reply {k}<CODE> <1-5> to rate, or {k}<CODE> <1-5> <your version> to "
                f"correct. Add SHARE to allow research use. {k}STOP to leave.")

    def unknown_code_sms(self, code: str) -> str:
        return f"We have no open task {code} for you. Check the code. {self.k}HELP for help."

    def parse_error_sms(self) -> str:
        return "Sorry, we could not read that. " + self.help_sms()

    def not_opted_in_sms(self) -> str:
        return f"You are not signed up. Reply {self.k}YES to join Lugha Verify."

    def closed_sms(self, code: str) -> str:
        return f"Task {code} is closed, thank you. No reply needed."

    def expired_sms(self, code: str) -> str:
        return f"Task {code} has expired, thank you. No reply needed."

    def expired_reply_sms(self, code: str) -> str:
        return (f"Thanks! Task {code} had expired (30 min), so this reply is saved "
                f"but not paid.")

    def no_correction_sms(self, code: str) -> str:
        return (f"Task {code}: rating saved. Tasks are paid for 4-5 stars, or for "
                f"your corrected version: {self.k}{code} <stars> <your version>.")

    def recipient_stop_sms(self) -> str:
        return f"You will get no more messages from this sender. Reply {self.k}YES to rejoin."

    def recipient_yes_sms(self) -> str:
        return f"You will receive messages again. Reply {self.k}STOP to leave."


# Module-level functions = the texts with no keyword (dedicated shortcode).
_plain = Templates()
task_sms = _plain.task_sms
filled_sms = _plain.filled_sms
late_reply_sms = _plain.late_reply_sms
too_fast_sms = _plain.too_fast_sms
thanks_sms = _plain.thanks_sms
thanks_pending_sms = _plain.thanks_pending_sms
opt_in_prompt = _plain.opt_in_prompt
opted_in_sms = _plain.opted_in_sms
opted_out_sms = _plain.opted_out_sms
help_sms = _plain.help_sms
unknown_code_sms = _plain.unknown_code_sms
parse_error_sms = _plain.parse_error_sms
not_opted_in_sms = _plain.not_opted_in_sms
closed_sms = _plain.closed_sms
expired_sms = _plain.expired_sms
expired_reply_sms = _plain.expired_reply_sms
no_correction_sms = _plain.no_correction_sms
recipient_stop_sms = _plain.recipient_stop_sms
recipient_yes_sms = _plain.recipient_yes_sms
