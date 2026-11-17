"""BSR's Agent: one recognisable name and sign-off, offers to bring in a person, and email_bsr."""

import json
import os
import sys

import pytest

from codespringhub import agent, contact, store
from codespringhub import server as srv
from codespringhub.errors import Forbidden, HubError

from conftest import USER
from test_server import call


@pytest.fixture
def mailer(tmp_path, monkeypatch):
    log = tmp_path / "mail.log"
    fake = tmp_path / "fake_mail.py"
    fake.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, 'a').write(json.dumps({{'args': sys.argv[1:], 'body': sys.stdin.read()}}) + '\\n')\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CODESPRINGHUB_MAILER", str(fake))
    return log


def sent(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_one_name_and_a_sign_off_on_hub_replies():
    assert agent.SIGNATURE == "— BSR's Agent · Bioinformatics Shared Resource, CSHL · CodeSpring"
    assert agent.SIGNATURE in srv.INSTRUCTIONS and "One name: BSR's Agent" in srv.INSTRUCTIONS
    assert agent.SIGNATURE in agent.STYLE and "bench colleague" in agent.STYLE
    assert "Let's take a look at your data" in agent.GREETING


def test_a_person_is_offered_every_few_hub_calls(monkeypatch):
    monkeypatch.setattr(srv, "_calls_since_check_in", 0)
    seen = []
    for _ in range(srv.CHECK_IN_EVERY * 2):
        r = call("list_workflows")
        seen.append("bsr_check_in" in r)
    assert seen.count(True) == 2 and seen[srv.CHECK_IN_EVERY - 1] and not any(seen[: srv.CHECK_IN_EVERY - 1])
    text = None
    monkeypatch.setattr(srv, "_calls_since_check_in", srv.CHECK_IN_EVERY - 1)
    text = call("get_lab_context", lab_id="lab-a")
    assert "[bsr_check_in]" in text and "second pair of human eyes" in text


def test_someone_without_a_lab_is_offered_setup():
    assert call("list_labs", as_user="eve")["bsr_check_in"]["reason"] == "lab_setup"
    assert "bsr_check_in" not in call("list_labs", as_user="ann") or \
        call("list_labs", as_user="ann")["bsr_check_in"]["reason"] == "periodic"


def test_email_needs_the_persons_yes_and_shows_the_exact_draft(mailer):
    d = contact.email_bsr(USER, "meeting", "Could we talk about my DE results?", "We ran DESeq2; 55 genes changed.",
                          lab_id="lab-a", dataset_ids=["ds:bsr/shared/demo-rnaseq"])
    assert "draft" in d and not sent(mailer)
    assert d["draft"]["cc"] == f"{USER}@cshl.edu" and "gajri@cshl.edu" in " ".join(d["draft"]["to"])
    assert "Request for a meeting" in d["draft"]["subject"] and "55 genes changed" in d["draft"]["body"]
    out = contact.email_bsr(USER, "meeting", "Could we talk about my DE results?", "We ran DESeq2; 55 genes changed.",
                            lab_id="lab-a", dataset_ids=["ds:bsr/shared/demo-rnaseq"], user_confirmed=True)
    assert out["sent"] and out["ticket"]
    msg = sent(mailer)[0]
    assert msg["args"][:2] == ["-s", d["draft"]["subject"]] and ["-c", f"{USER}@cshl.edu"] == msg["args"][2:4]
    assert f"replyto={USER}@cshl.edu" in msg["args"] and "rouse@cshl.edu" in msg["args"]
    assert msg["body"] == d["draft"]["body"]
    assert store.read_events("reviews")[-1]["summary"].startswith("[email: meeting]")


def test_email_refuses_others_personal_items_and_bad_input(mailer):
    with pytest.raises(Forbidden, match="someone else's"):
        contact.email_bsr(USER, "question", "See doc:lab-a/~ann/notes/x", user_confirmed=True)
    contact.email_bsr(USER, "question", f"See my doc:lab-a/~{USER}/notes/x")  # own items are fine
    with pytest.raises(HubError, match="purpose"):
        contact.email_bsr(USER, "gossip", "hi")
    with pytest.raises(HubError, match="isn't one of your labs"):
        contact.email_bsr(USER, "lab_setup", "Set up please", lab_id="no-such-lab")
    with pytest.raises(Forbidden):
        contact.email_bsr("ann", "question", "hi", user_confirmed=True)
    assert not sent(mailer)


def test_email_drafts_are_not_written_to_the_usage_log(mailer):
    call("email_bsr", purpose="question", message="My secret plan", chat_summary="Very private summary")
    row = [r for r in store.read_events("audit") if r["tool"] == "email_bsr"][-1]
    assert "secret" not in row["args"] and "private summary" not in row["args"]
    assert json.loads(row["args"]) == {"purpose": "question", "user_confirmed": False}
