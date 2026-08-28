#!/usr/bin/env python3
"""Send a short plain-text alert about the pipeline itself. No model, no deps.

Silence used to be ambiguous: when an edition did not arrive there was nothing to
distinguish "nothing happened today" from "the pipeline is broken". The reader
found out by not receiving a brief. This makes a miss say so.
"""

import os
import smtplib
import sys
from email.message import EmailMessage


def main() -> None:
    subject, body = sys.argv[1], sys.argv[2]
    msg = EmailMessage()
    msg["Subject"] = f"[SPREAD] {subject}"
    msg["From"] = os.environ["GMAIL_ADDRESS"]
    msg["To"] = os.environ["RECIPIENT"]
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)
    print(f"alert sent: {subject}")


if __name__ == "__main__":
    main()
