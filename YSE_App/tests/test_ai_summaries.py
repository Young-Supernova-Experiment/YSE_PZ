"""AI summaries (#294: #295 field + history + editing, #296 summariser service, #297 embeddings + search).

Pins:

* the gathered context (public comments only, even for a staff viewer; batch = collaboration-wide),
  the deterministic prompt and its length cap, the inputs hash, the template summary;
* ``set_summary``: every version kept, ``is_current`` flags, ``summary_modified``, the embedding refresh;
* the provider adapters against mocked HTTP (OpenAI-compatible, Anthropic Messages incl. refusal and
  the absence of sampling parameters), the key lookup through EncryptedCredential;
* the queue path end to end (template and mocked provider), failures landing on the run + a
  notification, the daily cap, the running-run guard, the per-user opt-in and the group audience;
* the detail-page card fragment and its actions, the DRF endpoints, the table tooltip;
* the embedding search (ranking, visibility, text fallback, the timing budget), the nightly batch,
  the cron gate and the management command. Both database vendors run this module.
"""

from __future__ import annotations

import datetime
import json
import time
from io import StringIO
from unittest import mock

import numpy as np
import requests
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from YSE_App.jobs import run_pass
from YSE_App.models import (
    EncryptedCredential,
    ExternalService,
    ExternalServiceRun,
    FollowupStatus,
    Job,
    Notification,
    Transient,
    TransientFollowup,
    TransientSummaryEmbedding,
    TransientSummaryHistory,
    TransientSummaryPreference,
)
from YSE_App.services import llm
from YSE_App.services import summaries as svc
from YSE_App.tests.fixtures_minimal import (
    attach_synthetic_log,
    attach_synthetic_photometry,
    attach_synthetic_spectrum,
    audit_fields,
    create_minimal_transient,
    create_test_user,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


OPENAI_REPLY = {"id": "chatcmpl-1", "model": "gpt-test-1", "usage": {"prompt_tokens": 500, "completion_tokens": 80},
                "choices": [{"index": 0, "message": {"role": "assistant",
                                                     "content": "2026sum is a young Type II supernova at z=0.05."}}]}
ANTHROPIC_REPLY = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test-1",
                   "stop_reason": "end_turn", "content": [{"type": "text", "text": "2026sum is a Type II supernova."}],
                   "usage": {"input_tokens": 500, "output_tokens": 40}}
ANTHROPIC_REFUSAL = {"id": "msg_2", "type": "message", "role": "assistant", "model": "claude-test-1",
                     "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "general_harms"},
                     "content": []}


def posts(*replies):
    """A ``requests.post`` stand-in returning the given replies in order and recording each call."""
    queue = list(replies)
    calls = []

    def _post(url, json=None, headers=None, timeout=None, **kwargs):
        calls.append({"url": url, "json": json, "headers": headers or {}, "timeout": timeout})
        reply = queue.pop(0) if queue else replies[-1]
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, FakeResponse) else FakeResponse(reply)

    _post.calls = calls
    return _post


@override_settings(JOB_RUNNER_INLINE=True, NOTIFICATION_EMAIL_ENABLED=False, LLM_PROVIDER="template",
                   LLM_MODEL="", LLM_CREDENTIAL="llm", LLM_EMBEDDING_API_BASE="", LLM_EMBEDDING_MODEL="")
class SummaryBase(TestCase):
    def setUp(self):
        svc.invalidate_search_cache()
        self.staff = create_test_user("sum_staff", is_staff=True, is_superuser=True)
        self.user = create_test_user("sum_user", is_staff=False)
        self.other = create_test_user("sum_other", is_staff=False)
        self.yse, _ = Group.objects.get_or_create(name="YSE")
        self.secret, _ = Group.objects.get_or_create(name="Secret")
        self.user.groups.add(self.yse)
        self.transient = create_minimal_transient(self.staff, name="2026sum", status_name="Following",
                                                  obs_group_name="YSE", ra=35.2, dec=12.4)
        self.transient.disc_date = timezone.now() - datetime.timedelta(days=9)
        self.transient.redshift = 0.05
        self.transient.redshift_source = "host spectrum"
        self.transient.save()
        attach_synthetic_photometry(self.staff, self.transient)
        self.spectrum = attach_synthetic_spectrum(self.staff, self.transient)
        self.public_comment = attach_synthetic_log(self.staff, self.transient, "Blue continuum, looks young; Type II?")
        self.private_comment = attach_synthetic_log(self.staff, self.transient, "PRIVATE: embargoed host redshift 0.051")
        self.private_comment.is_public = False
        self.private_comment.save()
        self.private_comment.groups.add(self.secret)
        audit = audit_fields(self.staff)
        status, _ = FollowupStatus.objects.get_or_create(name="Requested", defaults=audit)
        TransientFollowup.objects.create(transient=self.transient, status=status, valid_start=timezone.now(),
                                         valid_stop=timezone.now() + datetime.timedelta(days=3), **audit)
        self.service = svc.ensure_service(self.staff)
        self.client = Client()

    def opt_in(self, user):
        svc.set_opt_in(user, True)

    def credential(self, name="llm", key="sk-test-key"):
        cred = EncryptedCredential(name=name, service="llm", kind=EncryptedCredential.KIND_GENERIC,
                                   created_by=self.staff, modified_by=self.staff)
        cred.set_secret({"api_key": key})
        cred.save()
        return cred


class ContextAndPromptTests(SummaryBase):
    def test_context_has_public_comment_only_even_for_staff_viewer(self):
        for viewer in (self.staff, self.user, None):
            ctx = svc.gather_context(self.transient, viewer)
            texts = [c["text"] for c in ctx["comments"]]
            self.assertIn("Blue continuum, looks young; Type II?", texts, viewer)
            self.assertFalse(any("PRIVATE" in t for t in texts), viewer)
        self.assertNotIn("PRIVATE", json.dumps(svc.gather_context(self.transient, self.staff)))

    def test_context_fields(self):
        ctx = svc.gather_context(self.transient, self.staff)
        self.assertEqual(ctx["name"], "2026sum")
        self.assertEqual(ctx["status"], "Following")
        self.assertEqual(ctx["redshift"], 0.05)
        self.assertEqual(ctx["redshift_source"], "host spectrum")
        self.assertEqual(len(ctx["spectra"]), 1)
        self.assertEqual(ctx["followups"][0]["status"], "Requested")
        self.assertIsNotNone(ctx["photometry"])
        self.assertEqual(ctx["photometry"]["detections"], 12)
        self.assertIsNone(ctx["current_summary"])

    def test_private_spectrum_excluded_from_context(self):
        self.spectrum.groups.add(self.secret)
        self.assertEqual(svc.gather_context(self.transient, self.staff)["spectra"], [])
        self.assertEqual(svc.gather_context(self.transient, None)["spectra"], [])

    def test_inputs_hash_is_stable_and_tracks_changes(self):
        h1 = svc.inputs_hash(svc.gather_context(self.transient, None))
        h2 = svc.inputs_hash(svc.gather_context(self.transient, None))
        self.assertEqual(h1, h2)
        attach_synthetic_log(self.staff, self.transient, "Now fading in r.")
        self.assertNotEqual(h1, svc.inputs_hash(svc.gather_context(self.transient, None)))

    def test_prompt_is_deterministic_and_capped(self):
        ctx = svc.gather_context(self.transient, None)
        system, user = svc.build_prompt(ctx)
        self.assertEqual((system, user), svc.build_prompt(ctx))
        self.assertIn("Name: 2026sum", user)
        self.assertIn("Blue continuum", user)
        self.assertNotIn("PRIVATE", user)
        for i in range(30):
            attach_synthetic_log(self.staff, self.transient, "comment %d " % i + "x" * 400)
        ctx = svc.gather_context(self.transient, None, max_comments=40)
        _, capped = svc.build_prompt(ctx, max_chars=3000)
        self.assertLessEqual(len(capped), 3000)
        self.assertIn("Comments (newest first)", capped)
        # comments are dropped oldest first: the newest one survives
        self.assertIn("comment 29", capped)

    def test_template_summary_is_deterministic_and_factual(self):
        ctx = svc.gather_context(self.transient, None)
        text = svc.render_template_summary(ctx)
        self.assertEqual(text, svc.render_template_summary(ctx))
        self.assertTrue(text.startswith("2026sum is a following transient not yet spectroscopically classified"))
        self.assertIn("redshift is 0.05", text)
        self.assertIn("1 spectrum has been taken", text)
        self.assertIn("12 detections", text)
        self.assertIn("Follow-up: 1 requested", text)
        self.assertIn("Latest comment (sum_staff", text)
        self.assertIn("Open questions: classification.", text)
        self.assertNotIn("PRIVATE", text)


class WriteAndHistoryTests(SummaryBase):
    def test_set_summary_keeps_every_version(self):
        v1 = svc.set_summary(self.transient, "First text.", self.staff)
        v2 = svc.set_summary(self.transient, "Second text.", self.user, source=svc.SOURCE_AI, provider="openai",
                             model_name="gpt-test-1", prompt_version="1", inputs_hash_value="ab" * 32)
        self.transient.refresh_from_db()
        self.assertEqual(self.transient.summary, "Second text.")
        self.assertIsNotNone(self.transient.summary_modified)
        versions = list(TransientSummaryHistory.objects.filter(transient=self.transient).order_by("pk"))
        self.assertEqual([v.pk for v in versions], [v1.pk, v2.pk])
        self.assertEqual([v.is_current for v in versions], [False, True])
        self.assertEqual(versions[1].author, "gpt-test-1")
        self.assertEqual(versions[0].author, "sum_staff")
        self.assertEqual(svc.current_version(self.transient).pk, v2.pk)
        self.assertEqual(self.transient.summary_first_line, "Second text.")

    def test_empty_text_rejected(self):
        with self.assertRaises(svc.SummaryError):
            svc.set_summary(self.transient, "   \n ", self.staff)
        self.assertFalse(TransientSummaryHistory.objects.exists())

    def test_embedding_refreshed_on_every_write(self):
        svc.set_summary(self.transient, "A young Type II supernova with a Keck spectrum.", self.staff)
        emb = TransientSummaryEmbedding.objects.get(transient=self.transient)
        self.assertEqual(emb.model_name, llm.LOCAL_EMBEDDER_NAME)
        self.assertEqual(emb.dim, llm.LOCAL_EMBEDDER_DIM)
        self.assertEqual(len(emb.values), llm.LOCAL_EMBEDDER_DIM)
        self.assertAlmostEqual(float(np.linalg.norm(emb.values)), 1.0, places=5)
        first_hash = emb.text_hash
        svc.set_summary(self.transient, "Actually a Type Ia near peak.", self.staff)
        emb.refresh_from_db()
        self.assertNotEqual(emb.text_hash, first_hash)
        self.assertEqual(TransientSummaryEmbedding.objects.filter(transient=self.transient).count(), 1)

    def test_write_does_not_bump_transient_modified_date(self):
        before = Transient.objects.get(pk=self.transient.pk).modified_date
        svc.set_summary(self.transient, "Text.", self.staff)
        self.assertEqual(Transient.objects.get(pk=self.transient.pk).modified_date, before)


class ProviderTests(SummaryBase):
    def test_defaults(self):
        cfg = llm.chat_config()
        self.assertEqual(cfg["provider"], "template")
        self.assertEqual(llm.chat_available(self.service), (True, ""))
        with override_settings(LLM_PROVIDER="anthropic"):
            self.assertEqual(llm.chat_config()["model"], "claude-opus-5-5")
            self.assertEqual(llm.chat_config()["api_base"], llm.ANTHROPIC_DEFAULT_BASE)
            ok, reason = llm.chat_available(self.service)
            self.assertFalse(ok)
            self.assertIn("no API key", reason)
        with override_settings(LLM_PROVIDER="openai"):
            ok, reason = llm.chat_available(self.service)
            self.assertIn("no model", reason)
        self.assertEqual(llm.chat_config({"provider": "openai", "model": "m", "api_base": "http://x/v1/"})["api_base"], "http://x/v1")
        with self.assertRaises(llm.LLMNotConfigured):
            llm.chat("s", "u", service=self.service)

    def test_key_from_service_credential_or_named_credential(self):
        self.assertEqual(llm.api_key(self.service), "")
        named = self.credential(name="llm", key="named-key")
        self.assertEqual(llm.api_key(self.service), "named-key")
        attached = self.credential(name="Anthropic key", key="attached-key")
        self.service.credential = attached
        self.service.save()
        self.assertEqual(llm.api_key(self.service), "attached-key")
        named.refresh_from_db()
        self.assertIsNotNone(attached.last_used_at or named.last_used_at)

    @override_settings(LLM_PROVIDER="openai", LLM_MODEL="gpt-test-1", LLM_API_BASE="https://llm.example/v1", LLM_TEMPERATURE=0.2)
    def test_openai_chat(self):
        self.credential()
        post = posts(OPENAI_REPLY)
        with mock.patch.object(llm.requests, "post", post) if hasattr(llm, "requests") else mock.patch("requests.post", post):
            text, meta = llm.chat("sys", "usr", service=self.service)
        self.assertEqual(text, "2026sum is a young Type II supernova at z=0.05.")
        self.assertEqual(meta["model"], "gpt-test-1")
        call = post.calls[0]
        self.assertEqual(call["url"], "https://llm.example/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk-test-key")
        self.assertEqual(call["json"]["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(call["json"]["temperature"], 0.2)

    @override_settings(LLM_PROVIDER="anthropic", LLM_TEMPERATURE=0.2)
    def test_anthropic_chat_headers_and_body(self):
        self.credential()
        post = posts(ANTHROPIC_REPLY)
        with mock.patch("requests.post", post):
            text, meta = llm.chat("sys", "usr", service=self.service, params={"model": "claude-sonnet-5-5"})
        self.assertEqual(text, "2026sum is a Type II supernova.")
        self.assertEqual(meta["provider"], "anthropic")
        call = post.calls[0]
        self.assertEqual(call["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call["headers"]["x-api-key"], "sk-test-key")
        self.assertEqual(call["headers"]["anthropic-version"], llm.ANTHROPIC_VERSION)
        self.assertEqual(call["json"]["model"], "claude-sonnet-5-5")
        self.assertEqual(call["json"]["system"], "sys")
        self.assertEqual(call["json"]["messages"], [{"role": "user", "content": "usr"}])
        self.assertNotIn("temperature", call["json"])
        self.assertNotIn("thinking", call["json"])

    @override_settings(LLM_PROVIDER="anthropic")
    def test_anthropic_refusal_and_http_errors(self):
        self.credential()
        with mock.patch("requests.post", posts(ANTHROPIC_REFUSAL)):
            with self.assertRaises(llm.LLMError) as ctx:
                llm.chat("s", "u", service=self.service)
        self.assertIn("declined", str(ctx.exception))
        with mock.patch("requests.post", posts(FakeResponse({"error": {"message": "bad key"}}, 401))):
            with self.assertRaises(llm.LLMError) as ctx:
                llm.chat("s", "u", service=self.service)
        self.assertIn("HTTP 401", str(ctx.exception))
        with mock.patch("requests.post", posts(requests.ConnectionError("refused"))):
            with self.assertRaises(llm.LLMError):
                llm.chat("s", "u", service=self.service)
        with mock.patch("requests.post", posts(FakeResponse("<html>", 200))):
            with self.assertRaises(llm.LLMError):
                llm.chat("s", "u", service=self.service)

    def test_local_embedder_is_deterministic_and_lexical(self):
        emb = llm.HashedEmbedder()
        a, b = emb.embed(["young Type II supernova with a Keck spectrum", "young Type II supernova with a Keck spectrum"])
        self.assertEqual(a, b)
        c = emb.embed(["quasar variability in the nucleus"])[0]
        self.assertGreater(llm.cosine(a, emb.embed(["Type II with spectrum from Keck"])[0]), llm.cosine(a, c))
        self.assertEqual(llm.embedder_name(self.service), llm.LOCAL_EMBEDDER_NAME)

    @override_settings(LLM_EMBEDDING_API_BASE="https://emb.example/v1", LLM_EMBEDDING_MODEL="text-embedding-test")
    def test_remote_embedder(self):
        self.credential()
        reply = {"data": [{"index": 1, "embedding": [0.0, 3.0, 4.0]}, {"index": 0, "embedding": [1.0, 0.0, 0.0]}]}
        post = posts(reply)
        with mock.patch("requests.post", post):
            vectors, name = llm.embed_texts(["a", "b"], service=self.service)
        self.assertEqual(name, "text-embedding-test")
        self.assertEqual(vectors[0], [1.0, 0.0, 0.0])
        self.assertAlmostEqual(vectors[1][1], 0.6)
        self.assertEqual(post.calls[0]["url"], "https://emb.example/v1/embeddings")
        # without a key the local embedder takes over
        EncryptedCredential.objects.all().delete()
        self.assertEqual(llm.embedder_name(self.service), llm.LOCAL_EMBEDDER_NAME)


class RunTests(SummaryBase):
    def url(self, name):
        return reverse(name, kwargs={"transient_id": self.transient.pk})

    def test_template_run_through_the_queue(self):
        self.opt_in(self.user)
        self.client.force_login(self.user)
        resp = self.client.post(self.url("transient_summary_generate"))
        self.assertEqual(resp.status_code, 200, resp.content)
        run = ExternalServiceRun.objects.get(uuid=resp.json()["run"]["uuid"])
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED, run.error)
        self.assertEqual(run.result["provider"], "template")
        self.assertEqual(run.result["viewer"], "sum_user")
        self.transient.refresh_from_db()
        self.assertTrue(self.transient.summary.startswith("2026sum is a following transient"))
        version = svc.current_version(self.transient)
        self.assertEqual((version.source, version.provider, version.model_name, version.prompt_version),
                         ("ai", "template", "template-v1", "1"))
        self.assertEqual(len(version.inputs_hash), 64)
        self.assertEqual(version.run_id, run.pk)
        self.assertEqual(version.created_by, self.user)
        self.assertTrue(Job.objects.filter(kind="external_service.run", status=Job.DONE).exists())

    @override_settings(LLM_PROVIDER="openai", LLM_MODEL="gpt-test-1")
    def test_provider_run_writes_provenance(self):
        self.credential()
        self.opt_in(self.staff)
        self.client.force_login(self.staff)
        post = posts(OPENAI_REPLY)
        with mock.patch("requests.post", post):
            resp = self.client.post(self.url("transient_summary_generate"))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.transient.refresh_from_db()
        self.assertEqual(self.transient.summary, "2026sum is a young Type II supernova at z=0.05.")
        version = svc.current_version(self.transient)
        self.assertEqual((version.provider, version.model_name), ("openai", "gpt-test-1"))
        self.assertNotIn("PRIVATE", post.calls[0]["json"]["messages"][1]["content"])
        self.assertIn("Blue continuum", post.calls[0]["json"]["messages"][1]["content"])
        run = ExternalServiceRun.objects.get(pk=version.run_id)
        self.assertEqual(run.result["usage"]["completion_tokens"], 80)

    @override_settings(LLM_PROVIDER="anthropic")
    def test_failure_lands_on_run_and_notifies(self):
        self.credential()
        self.opt_in(self.user)
        self.client.force_login(self.user)
        with mock.patch("requests.post", posts(FakeResponse({"error": "overloaded"}, 529))):
            resp = self.client.post(self.url("transient_summary_generate"))
        self.assertEqual(resp.status_code, 200)
        run = ExternalServiceRun.objects.get(uuid=resp.json()["run"]["uuid"])
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)
        self.assertIn("HTTP 529", run.error)
        self.transient.refresh_from_db()
        self.assertIsNone(self.transient.summary)
        note = Notification.objects.filter(recipient=self.user).order_by("-pk").first()
        self.assertIsNotNone(note)
        self.assertIn("failed", note.text)
        # the card shows the failure
        html = self.client.get(self.url("transient_detail_summary_fragment")).content.decode()
        self.assertIn("Last generation failed", html)
        self.assertIn("HTTP 529", html)

    def test_opt_in_and_group_gates(self):
        self.client.force_login(self.user)
        resp = self.client.post(self.url("transient_summary_generate"))
        self.assertEqual(resp.status_code, 403)
        self.assertIn("enable AI summaries", resp.json()["error"])
        # opt in through the card
        resp = self.client.post(self.url("transient_summary_optin"), {"enabled": "1"})
        self.assertEqual(resp.json(), {"ok": True, "enabled": True})
        self.assertTrue(TransientSummaryPreference.enabled_for(self.user))
        html = self.client.get(self.url("transient_detail_summary_fragment")).content.decode()
        self.assertIn('data-summary-action="generate"', html)
        # a group restriction on the service hides the button and blocks the POST
        self.service.groups.add(self.secret)
        html = self.client.get(self.url("transient_detail_summary_fragment")).content.decode()
        self.assertNotIn('data-summary-action="generate"', html)
        self.assertIn("limited to Secret", html)
        self.assertEqual(self.client.post(self.url("transient_summary_generate")).status_code, 403)
        self.user.groups.add(self.secret)
        self.assertEqual(self.client.post(self.url("transient_summary_generate")).status_code, 200)
        # opting out again
        self.client.post(self.url("transient_summary_optin"), {"enabled": "0"})
        self.assertFalse(TransientSummaryPreference.enabled_for(self.user))

    def test_running_guard_and_daily_cap(self):
        self.opt_in(self.user)
        self.client.force_login(self.user)
        pending = svc.request_summary(self.transient, self.user, dispatch=False)
        self.assertEqual(pending.status, ExternalServiceRun.STATUS_PENDING)
        resp = self.client.post(self.url("transient_summary_generate"))
        self.assertEqual(resp.status_code, 409)
        html = self.client.get(self.url("transient_detail_summary_fragment")).content.decode()
        self.assertIn("Generating a summary", html)
        self.assertIn('data-active="1"', html)
        pending.mark_cancelled("test")
        self.service.max_runs_per_user_per_day = 1
        self.service.save()
        self.assertEqual(self.client.post(self.url("transient_summary_generate")).status_code, 200)
        resp = self.client.post(self.url("transient_summary_generate"))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["limit"], 1)
        # staff are exempt from the cap
        self.opt_in(self.staff)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self.url("transient_summary_generate")).status_code, 200)
        self.assertEqual(self.client.post(self.url("transient_summary_generate")).status_code, 200)

    def test_stale_runs_fail(self):
        run = svc.request_summary(self.transient, self.staff, batch=True, dispatch=False)
        ExternalServiceRun.objects.filter(pk=run.pk).update(created_date=timezone.now() - datetime.timedelta(hours=2))
        self.assertEqual(svc.fail_stale_runs(30), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, ExternalServiceRun.STATUS_FAILED)

    def test_not_configured_provider_is_reported(self):
        with override_settings(LLM_PROVIDER="anthropic"):
            self.opt_in(self.user)
            self.client.force_login(self.user)
            html = self.client.get(self.url("transient_detail_summary_fragment")).content.decode()
            self.assertIn("Summariser not configured", html)
            resp = self.client.post(self.url("transient_summary_generate"))
            self.assertEqual(resp.status_code, 403)
            self.assertIn("not configured", resp.json()["error"])
            self.assertFalse(ExternalServiceRun.objects.exists())

    def test_edit_view_keeps_history(self):
        self.client.force_login(self.user)
        resp = self.client.post(self.url("transient_summary_edit"), {"text": "Human text one."})
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self.client.post(self.url("transient_summary_edit"), data=json.dumps({"text": "Human text two."}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["summary"], "Human text two.")
        self.assertEqual(TransientSummaryHistory.objects.filter(transient=self.transient).count(), 2)
        self.assertEqual(self.client.post(self.url("transient_summary_edit"), {"text": ""}).status_code, 400)
        html = self.client.get(self.url("transient_detail_summary_fragment")).content.decode()
        self.assertIn("Human text two.", html)
        self.assertIn("Written by sum_user", html)
        self.assertIn("History (2)", html)
        self.assertIn("Human text one.", html)
        with override_settings(SUMMARY_EDIT_STAFF_ONLY=True):
            self.assertEqual(self.client.post(self.url("transient_summary_edit"), {"text": "x"}).status_code, 403)
            self.client.force_login(self.staff)
            self.assertEqual(self.client.post(self.url("transient_summary_edit"), {"text": "x"}).status_code, 200)

    def test_hidden_transient_is_404(self):
        hidden = create_minimal_transient(self.staff, name="2026hidden", obs_group_name="YSE", ra=1.0, dec=1.0)
        self.client.force_login(self.user)
        url = reverse("transient_detail_summary_fragment", kwargs={"transient_id": hidden.pk})
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(reverse("transient_summary_edit", kwargs={"transient_id": hidden.pk}),
                                          {"text": "x"}).status_code, 404)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url.replace(str(hidden.pk), "999999")).status_code, 404)

    def test_anonymous_redirects(self):
        resp = Client().get(self.url("transient_detail_summary_fragment"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Client().get(reverse("summary_search")).status_code, 302)

    def test_detail_page_has_the_card(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("transient_detail", kwargs={"slug": self.transient.slug}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('id="ai_summary_container"', html)
        self.assertIn("yseLoadSummaryCard", html)
        self.assertIn(self.url("transient_detail_summary_fragment"), html)


class APITests(SummaryBase):
    def test_summary_endpoints(self):
        self.client.force_login(self.staff)
        base = "/api/transients/%d/summary/" % self.transient.pk
        resp = self.client.get(base)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["summary"], "")
        self.assertIsNone(resp.json()["current"])
        resp = self.client.patch(base, data=json.dumps({"text": "API text."}), content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertEqual(data["summary"], "API text.")
        self.assertEqual(data["current"]["source"], "human")
        self.assertEqual(data["current"]["created_by"], "sum_staff")
        self.assertEqual(len(data["history"]), 1)
        resp = self.client.patch(base, data=json.dumps({"text": ""}), content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        # the generic transient endpoint shows the summary but does not accept it
        resp = self.client.get("/api/transients/%d/" % self.transient.pk)
        self.assertEqual(resp.json()["summary"], "API text.")
        # generate: opt-in required, then 202 and a run
        resp = self.client.post(base + "generate/")
        self.assertEqual(resp.status_code, 403)
        self.opt_in(self.staff)
        resp = self.client.post(base + "generate/")
        self.assertEqual(resp.status_code, 202, resp.content)
        run = ExternalServiceRun.objects.get(uuid=resp.json()["run"])
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED)
        data = self.client.get(base).json()
        self.assertEqual(data["current"]["source"], "ai")
        self.assertEqual(data["current"]["provider"], "template")
        self.assertEqual(len(data["history"]), 2)
        self.assertEqual(data["run"]["status"], "succeeded")

    def test_api_permissions(self):
        hidden = create_minimal_transient(self.staff, name="2026hid2", obs_group_name="YSE", ra=2.0, dec=2.0)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/api/transients/%d/summary/" % hidden.pk).status_code, 404)
        self.assertEqual(self.client.get("/api/transients/%d/summary/" % self.transient.pk).status_code, 200)
        with override_settings(SUMMARY_EDIT_STAFF_ONLY=True):
            resp = self.client.patch("/api/transients/%d/summary/" % self.transient.pk,
                                     data=json.dumps({"text": "no"}), content_type="application/json")
            self.assertEqual(resp.status_code, 403)
        self.assertIn(Client().get("/api/summary_search/?q=x").status_code, (401, 403))


SUMMARIES = {
    "2026keck": "A young Type II supernova discovered two days after explosion; a spectrum from Keck LRIS shows a blue "
                "continuum with flash-ionisation features. Redshift 0.021 from host narrow lines. Rising at 0.3 mag/day.",
    "2026ia": "A normal Type Ia supernova near peak at z=0.08 classified from a Lick Shane spectrum; light curve declining.",
    "2026agn": "Nuclear transient in an AGN-like host; WISE colours flag it as AGN-like, no spectrum yet, redshift unknown.",
    "2026tde": "Blue nuclear flare with a hot blackbody, candidate tidal disruption event; spectrum from Keck LRIS pending.",
}


class SearchTests(SummaryBase):
    def setUp(self):
        super().setUp()
        self.by_name = {}
        for i, (name, text) in enumerate(SUMMARIES.items()):
            t = create_minimal_transient(self.staff, name=name, obs_group_name="YSE", ra=10.0 + i, dec=5.0)
            attach_synthetic_photometry(self.staff, t)
            svc.set_summary(t, text, self.staff)
            self.by_name[name] = t

    def test_query_ranks_matching_transient_first(self):
        result = svc.search_summaries("young Type II with a spectrum from Keck", self.staff)
        self.assertEqual(result["mode"], "embedding")
        names = [r["transient"].name for r in result["results"]]
        self.assertEqual(names[0], "2026keck")
        self.assertGreater(result["results"][0]["score"], result["results"][1]["score"])
        self.assertIn("Keck", result["results"][0]["snippet"])
        result = svc.search_summaries("tidal disruption flare", self.staff)
        self.assertEqual(result["results"][0]["transient"].name, "2026tde")
        self.assertEqual(svc.search_summaries("", self.staff)["results"], [])
        self.assertEqual(svc.search_summaries("nothing matches xyzzy", self.staff)["results"], [])

    def test_visibility_and_limit(self):
        hidden = create_minimal_transient(self.staff, name="2026keck2", obs_group_name="YSE", ra=20.0, dec=5.0)
        svc.set_summary(hidden, SUMMARIES["2026keck"], self.staff)  # no photometry: invisible to non-staff
        names = [r["transient"].name for r in svc.search_summaries("Keck LRIS spectrum", self.user)["results"]]
        self.assertNotIn("2026keck2", names)
        self.assertIn("2026keck", names)
        names = [r["transient"].name for r in svc.search_summaries("Keck LRIS spectrum", self.staff)["results"]]
        self.assertIn("2026keck2", names)
        self.assertEqual(len(svc.search_summaries("Keck LRIS spectrum", self.staff, limit=1)["results"]), 1)

    @override_settings(SUMMARY_SEARCH_MODE="text")
    def test_text_fallback(self):
        result = svc.search_summaries("Keck LRIS", self.staff)
        self.assertEqual(result["mode"], "text")
        self.assertEqual({r["transient"].name for r in result["results"]}, {"2026keck", "2026tde"})

    def test_fallback_when_no_vectors_for_the_embedder(self):
        TransientSummaryEmbedding.objects.all().delete()
        svc.invalidate_search_cache()
        result = svc.search_summaries("Keck LRIS", self.staff)
        self.assertEqual(result["mode"], "text")
        self.assertTrue(result["results"])

    def test_page_and_api(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("summary_search"), {"q": "young Type II with a spectrum from Keck"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('data-transient="2026keck"', html)
        self.assertLess(html.index('data-transient="2026keck"'), html.index('data-transient="2026ia"'))
        self.assertIn("cosine similarity", html)
        resp = self.client.get(reverse("summary_search"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("with a summary", resp.content.decode())
        resp = self.client.get("/api/summary_search/", {"q": "young Type II with a spectrum from Keck", "limit": 2})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["mode"], "embedding")
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["results"][0]["name"], "2026keck")
        self.assertIn("snippet", data["results"][0])
        # the header box on every page points at the search
        resp = self.client.get(reverse("summary_search"))
        self.assertIn('id="yse-header-summary-q"', resp.content.decode())

    def test_ranking_budget(self):
        rng = np.random.default_rng(1)
        matrix = rng.standard_normal((5000, 256)).astype("<f4")
        matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
        query = matrix[123] + 0.01 * rng.standard_normal(256).astype("<f4")
        start = time.perf_counter()
        top = svc.rank_vectors(query, matrix, 25)
        elapsed = time.perf_counter() - start
        self.assertEqual(top[0][0], 123)
        self.assertEqual(len(top), 25)
        self.assertLess(elapsed, 0.5)
        # the DB path: 300 stored vectors are loaded, cached and ranked well inside the budget
        for i in range(300):
            t = create_minimal_transient(self.staff, name="2026bulk%03d" % i, obs_group_name="YSE", ra=float(i % 360), dec=-10.0)
            Transient.objects.filter(pk=t.pk).update(summary="bulk %d" % i)
            t.summary = "bulk %d" % i
            svc.refresh_embedding(t)
        start = time.perf_counter()
        result = svc.search_summaries("young Type II with a spectrum from Keck", self.staff)
        first = time.perf_counter() - start
        start = time.perf_counter()
        svc.search_summaries("tidal disruption", self.staff)
        second = time.perf_counter() - start
        self.assertEqual(result["results"][0]["transient"].name, "2026keck")
        self.assertLess(second, 0.5)
        self.assertLess(first, 2.0)

    def test_snippet(self):
        text = "word " * 100 + "Keck LRIS" + " tail" * 100
        s = svc.snippet(text, "keck", width=120)
        self.assertIn("Keck LRIS", s)
        self.assertTrue(s.startswith("...") and s.endswith("..."))
        self.assertEqual(svc.snippet("short text", "x"), "short text")

    def test_table_tooltip(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("search"), {"q": "2026keck"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('title="A young Type II supernova discovered two days after explosion', resp.content.decode())


class BatchTests(SummaryBase):
    def test_stale_transients(self):
        quiet = create_minimal_transient(self.staff, name="2026quiet", obs_group_name="YSE", ra=50.0, dec=1.0)
        Transient.objects.filter(pk=quiet.pk).update(modified_date=timezone.now() - datetime.timedelta(days=5))
        done = create_minimal_transient(self.staff, name="2026done", obs_group_name="YSE", ra=51.0, dec=1.0)
        attach_synthetic_log(self.staff, done, "new comment")
        svc.set_summary(done, "Already summarised after the comment.", self.staff)
        names = {t.name for t in svc.stale_transients(hours=24)}
        self.assertIn("2026sum", names)
        self.assertNotIn("2026quiet", names)
        self.assertNotIn("2026done", names)
        attach_synthetic_log(self.staff, done, "another comment after the summary")
        self.assertIn("2026done", {t.name for t in svc.stale_transients(hours=24)})
        self.assertEqual(len(svc.stale_transients(hours=24, limit=1)), 1)

    def test_refresh_job_queues_batch_runs(self):
        result = svc.refresh_stale({})
        self.assertGreaterEqual(result["queued"], 1)
        run = ExternalServiceRun.objects.get(transient=self.transient, service=self.service)
        self.assertEqual(run.status, ExternalServiceRun.STATUS_SUCCEEDED, run.error)
        self.assertTrue(run.request_payload["batch"])
        self.assertIsNone(run.request_payload["viewer_id"])
        self.assertIsNone(run.result["viewer"])
        self.transient.refresh_from_db()
        self.assertTrue(self.transient.summary)
        self.assertEqual(svc.current_version(self.transient).created_by, self.service.created_by)
        # nothing is stale any more, and a disabled service queues nothing
        self.assertEqual(svc.refresh_stale({})["queued"], 0)
        self.service.enabled = False
        self.service.save()
        self.assertIn("skipped", svc.refresh_stale({}))

    @override_settings(JOB_RUNNER_INLINE=False)
    def test_cron_gate_and_dedup(self):
        from YSE_App.data_ingest.Summary_Jobs import SummaryRefresh

        self.assertEqual(SummaryRefresh().do(), "disabled")
        self.assertFalse(Job.objects.filter(kind=svc.BATCH_JOB_KIND).exists())
        with override_settings(SUMMARY_BATCH_CRON_ENABLED=True):
            self.assertTrue(SummaryRefresh().do().startswith("queued job"))
            self.assertIn("already queued", SummaryRefresh().do())
        self.assertEqual(Job.objects.filter(kind=svc.BATCH_JOB_KIND).count(), 1)
        with override_settings(JOB_RUNNER_INLINE=True):
            run_pass()
        job = Job.objects.get(kind=svc.BATCH_JOB_KIND)
        self.assertEqual(job.status, Job.DONE, job.error)
        self.assertGreaterEqual(job.result["queued"], 1)
        self.assertIn("YSE_App.data_ingest.Summary_Jobs.SummaryRefresh", __import__("django.conf").conf.settings.CRON_CLASSES)


class CommandAndAdminTests(SummaryBase):
    def test_register_command(self):
        ExternalService.objects.all().delete()
        out = StringIO()
        call_command("register_summary_service", "--group", "YSE", "--cap", "5", "--provider", "anthropic", stdout=out)
        service = ExternalService.objects.get(slug=svc.SERVICE_SLUG)
        self.assertEqual(service.kind, ExternalService.KIND_SUMMARY)
        self.assertEqual(service.max_runs_per_user_per_day, 5)
        self.assertEqual([g.name for g in service.groups.all()], ["YSE"])
        self.assertEqual(service.default_params["provider"], "anthropic")
        self.assertIn("NOT ready", out.getvalue())
        cred = self.credential(name="Anthropic key")
        out = StringIO()
        call_command("register_summary_service", "--credential", "Anthropic key", "--rebuild-embeddings", stdout=out)
        service.refresh_from_db()
        self.assertEqual(service.credential, cred)
        self.assertIn("ready", out.getvalue())
        self.assertIn("embeddings rebuilt", out.getvalue())

    def test_admin_pages(self):
        svc.set_summary(self.transient, "Admin text.", self.staff)
        svc.set_opt_in(self.user, True)
        self.client.force_login(self.staff)
        for name in ("transientsummaryhistory", "transientsummaryembedding", "transientsummarypreference"):
            resp = self.client.get(reverse("admin:YSE_App_%s_changelist" % name))
            self.assertEqual(resp.status_code, 200, name)
        version = TransientSummaryHistory.objects.get()
        self.assertEqual(self.client.get(reverse("admin:YSE_App_transientsummaryhistory_change", args=[version.pk])).status_code, 200)
