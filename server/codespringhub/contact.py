"""Emailing the BSR team from a chat (the email_bsr tool): human connection on request.

Three purposes: a question, setting up CodeSpringHub for a lab, or asking for a meeting about an
analysis. Claude may add a short summary of the conversation. Nothing is sent until the person has
seen the exact email and said yes (user_confirmed). It goes to BSR's support contacts with a copy to
the person (replies go to them), through the cluster's own `mail`, and is logged as a BSR review
ticket so it also shows in the portal's Inbox.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess

from . import agent, config, knowledge, store
from .errors import Forbidden, HubError
from .permissions import can_access_lab, display_name, is_member

PURPOSES = {
    "question": "A question for BSR",
    "lab_setup": "Please set up CodeSpringHub for my lab",
    "meeting": "Request for a meeting about an analysis",
}
MAX_MESSAGE = 4000
MAX_SUMMARY = 6000
EMAIL_DOMAIN = os.environ.get("CODESPRINGHUB_EMAIL_DOMAIN", "cshl.edu")
_PERSONAL = re.compile(r"(?:doc|ds):[a-z0-9-]+/~([A-Za-z0-9_.-]+)")
_INTERNAL = re.compile(r"(?:doc|ds):([a-z0-9-]+)/internal/")


def _check_private(user: str, text: str):
    """The email goes to BSR: no one else's personal items, no other core's internal items."""
    others = {m for m in _PERSONAL.findall(text) if m != user}
    if others:
        raise Forbidden("The email mentions someone else's personal items; leave those out.")
    labs = knowledge.load_labs()
    for core_id in set(_INTERNAL.findall(text)):
        core = labs.get(core_id)
        if core_id != "bsr" and not (core and is_member(user, core)):
            raise Forbidden(f"The email mentions {core_id}'s internal items; leave those out.")


def _send(to: list[str], cc: str, subject: str, body: str):
    mailer = os.environ.get("CODESPRINGHUB_MAILER") or shutil.which("mail")
    if not mailer:
        raise HubError("This computer can't send email (no `mail` command). Please write to BSR directly.")
    # mailx: -c copy, -S replyto so BSR's reply reaches the person, not the cluster account.
    r = subprocess.run([mailer, "-s", subject, "-c", cc, "-S", f"replyto={cc}", *to], input=body, text=True,
                       capture_output=True, timeout=60)
    if r.returncode:
        raise HubError(f"The email couldn't be sent ({(r.stderr or '').strip()[:200] or 'mail failed'}). "
                       "Please write to BSR directly.")


def email_bsr(user, purpose, message, chat_summary="", lab_id=None, dataset_ids=None, user_confirmed=False) -> dict:
    if user != getpass.getuser():
        raise Forbidden("Emails can only be sent by the person whose account runs this.")
    if purpose not in PURPOSES:
        raise HubError("purpose must be 'question', 'lab_setup' or 'meeting'.")
    message, chat_summary = (message or "").strip(), (chat_summary or "").strip()
    if not message:
        raise HubError("Write a few lines in the person's words: what they'd like from BSR.")
    if len(message) > MAX_MESSAGE or len(chat_summary) > MAX_SUMMARY:
        raise HubError("That's long for an email; shorten the message or the summary.")
    _check_private(user, message + "\n" + chat_summary + "\n" + " ".join(dataset_ids or []))
    labs = knowledge.load_labs()
    lab = labs.get(lab_id) if lab_id else None
    if lab_id and (lab is None or not can_access_lab(user, lab)):
        raise HubError(f"'{lab_id}' isn't one of your labs; leave lab_id out or use one from list_labs.")

    contacts = [c for c in agent.support()["contacts"] if c.get("email")]
    to = [c["email"] for c in contacts]
    me = f"{user}@{EMAIL_DOMAIN}"
    name = display_name(user)
    subject = f"[BSR's Agent] {PURPOSES[purpose]}: {name} ({user})" + (f", {lab.name}" if lab else "")
    parts = [message]
    if chat_summary:
        parts += ["", "Summary of my conversation with BSR's Agent (written by Claude, checked by me):", chat_summary]
    if dataset_ids:
        parts += ["", "Hub datasets: " + ", ".join(dataset_ids)]
    parts += ["", "--", f"{name} ({me})" + (f", {lab.name}" if lab else ""),
              "Sent from CodeSpringHub (BSR's Agent) at my request. Reply to this email to reach me."]
    body = "\n".join(parts) + "\n"
    draft = {"to": [f"{c['name']} <{c['email']}>" for c in contacts], "cc": me, "subject": subject, "body": body}
    if user_confirmed is not True:
        return {
            "draft": draft,
            "next": ("Show this draft to the person exactly as it would be sent (to, copy, subject, the full body), "
                     "then ask 'Shall I send it?'. Only after they say yes, call email_bsr again with the same "
                     "arguments and user_confirmed=true. If they want changes, change the text and show it again."),
        }
    _send(to, me, subject, body)
    ticket = store.add_review(user, lab_id or "bsr", f"[email: {purpose}] " + message[:500], list(dataset_ids or []))
    return {
        "sent": True, "to": draft["to"], "cc": me, "ticket": ticket,
        "say": (f"Sent. The BSR team ({', '.join(c['name'] for c in contacts)}) has your note, and a copy went to "
                f"{me}; their reply will come straight to you."),
    }
