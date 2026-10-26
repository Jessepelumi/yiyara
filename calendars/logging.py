import logging
import re


class RedactCalendarCallback(logging.Filter):
    """Keep authorization codes and state out of Django request/access logs."""

    def filter(self, record):
        message = record.getMessage()
        if "/api/calendar/google/callback/" in message:
            record.msg = re.sub(
                r"(/api/calendar/google/callback/)\?[^\s\"]*",
                r"\1?[redacted]",
                message,
            )
            record.args = ()
        return True
