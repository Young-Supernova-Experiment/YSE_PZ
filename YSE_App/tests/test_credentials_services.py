"""Encrypted credentials (#264) and external-service runs (#265)."""

import base64
import io
import json
import os
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import Group, User
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from YSE_App.models import EncryptedCredential, ExternalService, ExternalServiceRun
from YSE_App.services import credentials as cs
from YSE_App.services import external_services as svc
from YSE_App.tests.fixtures_minimal import create_minimal_transient, create_test_user
from YSE_App.tests.test_settings_hardening import _load_settings

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()
# A valid Fernet key whose first base64 character is '-' (6-bit value 62 ->
# first byte 0xf8..0xfb): argparse mistakes it for an option (#359).
DASH_KEY = base64.urlsafe_b64encode(b"\xf8" + b"\x01" * 31).decode()
assert DASH_KEY.startswith("-")
SECRET = {"username": "yse-bot", "password": "hunter2-very-secret", "token": "tok_abc123"}


def _cred(user, name="LCO 2026B", service="lco", secret=SECRET, **kwargs):
    cred = EncryptedCredential(name=name, service=service, created_by=user, modified_by=user, **kwargs)
    if secret is not None:
        cred.set_secret(secret)
    cred.save()
    return cred


@override_settings(CREDENTIALS_KEY=KEY_A)
class CredentialCryptoTests(TestCase):
    def setUp(self):
        self.user = create_test_user("cred_user")

    def test_roundtrip(self):
        cred = _cred(self.user)
        cred = EncryptedCredential.objects.get(pk=cred.pk)
        self.assertEqual(cred.get_secret(), SECRET)
        self.assertTrue(cred.has_secret)
        self.assertEqual(cred.key_fingerprint, cs.key_fingerprint(KEY_A))

    def test_ciphertext_does_not_contain_plaintext(self):
        cred = _cred(self.user)
        for value in SECRET.values():
            self.assertNotIn(value, cred.encrypted_payload)
        self.assertTrue(cred.encrypted_payload.startswith("gAAAA"))  # Fernet token

    def test_str_and_masked_display_never_leak(self):
        cred = _cred(self.user)
        shown = str(cred) + cred.masked_display() + json.dumps(cred.masked_secret())
        for value in SECRET.values():
            self.assertNotIn(value, shown)
        self.assertEqual(cred.masked_secret(), {k: cs.MASK for k in SECRET})
        self.assertEqual(cred.secret_keys(), sorted(SECRET))
        self.assertIn("password=" + cs.MASK, cred.masked_display())

    def test_no_secret_display(self):
        cred = _cred(self.user, secret=None)
        self.assertFalse(cred.has_secret)
        self.assertEqual(cred.get_secret(), {})
        self.assertEqual(cred.masked_display(), "(no secret stored)")

    def test_touch_records_last_used(self):
        cred = _cred(self.user)
        self.assertIsNone(cred.last_used_at)
        cred.get_secret(touch=True)
        cred.refresh_from_db()
        self.assertIsNotNone(cred.last_used_at)

    def test_wrong_key_fails_closed(self):
        cred = _cred(self.user)
        with override_settings(CREDENTIALS_KEY=KEY_B):
            with self.assertRaises(cs.CredentialDecryptError):
                cred.get_secret()
            self.assertEqual(cred.masked_secret(), {})
            self.assertIn("not decryptable", cred.masked_display())

    def test_multi_key_decrypts_old_and_encrypts_with_first(self):
        cred = _cred(self.user)  # written under KEY_A
        with override_settings(CREDENTIALS_KEY=KEY_B + "," + KEY_A):
            self.assertEqual(cred.get_secret(), SECRET)
            cred.set_secret({"x": "1"})
            self.assertEqual(cred.key_fingerprint, cs.key_fingerprint(KEY_B))
        with override_settings(CREDENTIALS_KEY=KEY_B):
            self.assertEqual(cred.get_secret(), {"x": "1"})

    def test_missing_key_at_use_raises(self):
        with override_settings(CREDENTIALS_KEY=""):
            with self.assertRaises(cs.CredentialKeyMissing):
                cs.encrypt_payload({"a": "b"})

    def test_invalid_key_raises(self):
        with override_settings(CREDENTIALS_KEY="not-a-key"):
            with self.assertRaises(cs.CredentialKeyInvalid):
                cs.encrypt_payload({"a": "b"})

    def test_payload_must_be_object(self):
        with self.assertRaises(TypeError):
            cs.encrypt_payload(["list"])

    def test_usable_by(self):
        group = Group.objects.create(name="cred-group")
        member = create_test_user("cred_member", is_staff=False)
        member.groups.add(group)
        outsider = create_test_user("cred_outsider", is_staff=False)
        owner = create_test_user("cred_owner", is_staff=False)
        cred = _cred(self.user, owner_group=group)
        self.assertTrue(cred.usable_by(self.user))  # staff
        self.assertTrue(cred.usable_by(member))
        self.assertFalse(cred.usable_by(outsider))
        personal = _cred(self.user, name="personal", owner_user=owner)
        self.assertTrue(personal.usable_by(owner))
        self.assertFalse(personal.usable_by(member))
        cred.is_active = False
        self.assertFalse(cred.usable_by(self.user))

    def test_auditlog_excludes_payload(self):
        from auditlog.models import LogEntry

        cred = _cred(self.user)
        entries = LogEntry.objects.get_for_object(cred)
        self.assertTrue(entries.exists())
        for entry in entries:
            self.assertNotIn("encrypted_payload", entry.changes or "")
            for value in SECRET.values():
                self.assertNotIn(value, entry.changes or "")


@override_settings(CREDENTIALS_KEY=KEY_A)
class CredentialAdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("cred_admin", "a@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.admin)
        self.cred = _cred(self.admin)

    def test_changelist_and_change_pages_are_masked(self):
        for url in (
            reverse("admin:YSE_App_encryptedcredential_changelist"),
            reverse("admin:YSE_App_encryptedcredential_change", args=[self.cred.pk]),
        ):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            body = resp.content.decode()
            for value in SECRET.values():
                self.assertNotIn(value, body, url)
            self.assertNotIn(self.cred.encrypted_payload, body, url)
            self.assertIn(cs.MASK, body, url)

    def test_change_form_writes_new_secret_and_blank_keeps_old(self):
        url = reverse("admin:YSE_App_encryptedcredential_change", args=[self.cred.pk])
        base = {"name": self.cred.name, "service": self.cred.service, "kind": "facility",
                "description": "", "owner_group": "", "owner_user": "", "is_active": "on"}
        resp = self.client.post(url, dict(base, secret_json=""), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.cred.refresh_from_db()
        self.assertEqual(self.cred.get_secret(), SECRET)
        resp = self.client.post(url, dict(base, secret_json='{"api_key": "new-one"}'), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.cred.refresh_from_db()
        self.assertEqual(self.cred.get_secret(), {"api_key": "new-one"})
        self.assertEqual(self.cred.modified_by, self.admin)
        resp = self.client.post(url, dict(base, secret_json="[1,2]"))
        self.assertContains(resp, "must be a JSON object", status_code=200)

    def test_add_form_creates_encrypted_row(self):
        url = reverse("admin:YSE_App_encryptedcredential_add")
        resp = self.client.post(url, {"name": "TNS bot", "service": "tns", "kind": "tns", "description": "",
                                      "owner_group": "", "owner_user": "", "is_active": "on",
                                      "secret_json": '{"api_key": "k"}'}, follow=True)
        self.assertEqual(resp.status_code, 200)
        cred = EncryptedCredential.objects.get(service="tns", name="TNS bot")
        self.assertEqual(cred.get_secret(), {"api_key": "k"})
        self.assertEqual(cred.created_by, self.admin)
        self.assertNotIn("k", cred.encrypted_payload[:5])


class CredentialsKeySettingsTests(SimpleTestCase):
    """settings.py: CREDENTIALS_KEY behaves like SECRET_KEY (#253)."""

    def test_debug_without_key_derives_from_secret_key(self):
        mod = _load_settings(debug="True")
        self.assertEqual(mod.CREDENTIALS_KEY, cs.derive_debug_key(mod.SECRET_KEY))
        Fernet(mod.CREDENTIALS_KEY.encode())  # valid key

    def test_placeholder_counts_as_unset(self):
        mod = _load_settings(debug="True", site_extra="SECRET_KEY: k\n[secrets]\ncredentials_key: <fernet key>")
        self.assertEqual(mod.CREDENTIALS_KEY, cs.derive_debug_key("k"))

    def test_production_without_key_is_improperly_configured(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _load_settings(debug="False", site_extra="SECRET_KEY: from-the-ini-file")
        self.assertIn("generate_credentials_key", str(ctx.exception))
        self.assertIn("YSE_CREDENTIALS_KEY", str(ctx.exception))

    def test_production_reads_ini_key(self):
        mod = _load_settings(debug="False",
                             site_extra="SECRET_KEY: k\n[secrets]\ncredentials_key: %s" % KEY_A)
        self.assertEqual(mod.CREDENTIALS_KEY, KEY_A)

    def test_env_wins_over_ini(self):
        mod = _load_settings(debug="False",
                             site_extra="SECRET_KEY: k\n[secrets]\ncredentials_key: %s" % KEY_A,
                             env={"YSE_CREDENTIALS_KEY": KEY_B})
        self.assertEqual(mod.CREDENTIALS_KEY, KEY_B)

    def test_public_settings_ini_documents_the_key(self):
        import YSE_PZ.settings as live

        with open(os.path.join(os.path.dirname(live.__file__), "public_settings.ini")) as fh:
            ini = fh.read()
        self.assertIn("[secrets]", ini)
        self.assertIn("credentials_key:", ini)
        self.assertIn("YSE_CREDENTIALS_KEY", ini)


class KeyCommandTests(TestCase):
    def setUp(self):
        self.user = create_test_user("rot_user")

    def test_generate_prints_valid_key(self):
        out = io.StringIO()
        call_command("generate_credentials_key", "--quiet", stdout=out)
        key = out.getvalue().strip()
        Fernet(key.encode())
        out = io.StringIO()
        call_command("generate_credentials_key", stdout=out)
        self.assertIn("[secrets]", out.getvalue())
        self.assertIn("credentials_key:", out.getvalue())

    def test_rotate_reencrypts_every_row(self):
        with override_settings(CREDENTIALS_KEY=KEY_A):
            a = _cred(self.user, name="a")
            b = _cred(self.user, name="b", secret={"k": "v"})
            empty = _cred(self.user, name="empty", secret=None)
        out = io.StringIO()
        call_command("rotate_credentials_key", f"--old={KEY_A}", f"--new={KEY_B}", stdout=out)
        self.assertIn("re-encrypted 2 credential(s); 1 without a secret skipped; 0 undecryptable", out.getvalue())
        with override_settings(CREDENTIALS_KEY=KEY_B):
            self.assertEqual(EncryptedCredential.objects.get(pk=a.pk).get_secret(), SECRET)
            self.assertEqual(EncryptedCredential.objects.get(pk=b.pk).get_secret(), {"k": "v"})
            self.assertEqual(EncryptedCredential.objects.get(pk=a.pk).key_fingerprint, cs.key_fingerprint(KEY_B))
            self.assertEqual(EncryptedCredential.objects.get(pk=empty.pk).encrypted_payload, "")
        with override_settings(CREDENTIALS_KEY=KEY_A):
            with self.assertRaises(cs.CredentialDecryptError):
                EncryptedCredential.objects.get(pk=a.pk).get_secret()

    def test_rotate_dry_run_changes_nothing(self):
        with override_settings(CREDENTIALS_KEY=KEY_A):
            a = _cred(self.user, name="a")
        before = a.encrypted_payload
        out = io.StringIO()
        call_command("rotate_credentials_key", f"--old={KEY_A}", f"--new={KEY_B}", "--dry-run", stdout=out)
        self.assertIn("would re-encrypt 1", out.getvalue())
        self.assertEqual(EncryptedCredential.objects.get(pk=a.pk).encrypted_payload, before)

    def test_rotate_refuses_when_a_row_cannot_be_decrypted(self):
        key_c = Fernet.generate_key().decode()
        with override_settings(CREDENTIALS_KEY=KEY_A):
            a = _cred(self.user, name="a")
        with override_settings(CREDENTIALS_KEY=key_c):
            stray = _cred(self.user, name="stray")
        before = (a.encrypted_payload, stray.encrypted_payload)
        out = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("rotate_credentials_key", f"--old={KEY_A}", f"--new={KEY_B}", stdout=out)
        self.assertIn("cannot decrypt: stray", out.getvalue())
        self.assertEqual((EncryptedCredential.objects.get(pk=a.pk).encrypted_payload,
                          EncryptedCredential.objects.get(pk=stray.pk).encrypted_payload), before)
        # Naming both old keys rotates everything.
        call_command("rotate_credentials_key", f"--old={KEY_A}", f"--old={key_c}", f"--new={KEY_B}", stdout=io.StringIO())
        with override_settings(CREDENTIALS_KEY=KEY_B):
            self.assertEqual(EncryptedCredential.objects.get(pk=stray.pk).get_secret(), SECRET)

    def test_rotate_validates_keys(self):
        with self.assertRaises(CommandError):
            call_command("rotate_credentials_key", "--old=junk", f"--new={KEY_B}")

    def test_rotate_accepts_keys_that_start_with_a_dash(self):
        """A Fernet key is URL-safe base64, so one in 32 starts with '-' (#359).

        Written as a separate word argparse takes it for an option; the
        ``--old=KEY`` form and the environment fallback both work.
        """
        with override_settings(CREDENTIALS_KEY=DASH_KEY):
            a = _cred(self.user, name="a")
        with self.assertRaises(CommandError):
            call_command("rotate_credentials_key", "--old", DASH_KEY, "--new", KEY_B, stdout=io.StringIO())
        self.assertEqual(EncryptedCredential.objects.get(pk=a.pk).key_fingerprint, cs.key_fingerprint(DASH_KEY))

        out = io.StringIO()
        call_command("rotate_credentials_key", f"--old={DASH_KEY}", f"--new={KEY_B}", stdout=out)
        self.assertIn("re-encrypted 1 credential(s)", out.getvalue())
        with override_settings(CREDENTIALS_KEY=KEY_B):
            self.assertEqual(EncryptedCredential.objects.get(pk=a.pk).get_secret(), SECRET)

        env = {"YSE_ROTATE_OLD_KEYS": KEY_B, "YSE_ROTATE_NEW_KEY": DASH_KEY}
        with mock.patch.dict(os.environ, env):
            out = io.StringIO()
            call_command("rotate_credentials_key", stdout=out)
        self.assertIn("re-encrypted 1 credential(s)", out.getvalue())
        with override_settings(CREDENTIALS_KEY=DASH_KEY):
            self.assertEqual(EncryptedCredential.objects.get(pk=a.pk).get_secret(), SECRET)

    def test_rotate_without_keys_names_the_flags_and_the_env_vars(self):
        with mock.patch.dict(os.environ, {"YSE_ROTATE_OLD_KEYS": "", "YSE_ROTATE_NEW_KEY": ""}):
            with self.assertRaises(CommandError) as ctx:
                call_command("rotate_credentials_key", f"--new={KEY_B}")
            self.assertIn("YSE_ROTATE_OLD_KEYS", str(ctx.exception))
            with self.assertRaises(CommandError) as ctx:
                call_command("rotate_credentials_key", f"--old={KEY_A}")
            self.assertIn("YSE_ROTATE_NEW_KEY", str(ctx.exception))
        with self.assertRaises(CommandError):
            call_command("rotate_credentials_key", "--old", KEY_A, "--new", KEY_A)


@override_settings(CREDENTIALS_KEY=KEY_A)
class ExternalServiceRunTests(TestCase):
    def setUp(self):
        self.staff = create_test_user("svc_staff")
        self.user = create_test_user("svc_user", is_staff=False)
        self.transient = create_minimal_transient(self.staff, name="svcrun01")
        self.cred = _cred(self.staff, service="fitter")
        self.service = ExternalService.objects.create(
            name="Light-curve fitter", slug="fitter", kind=ExternalService.KIND_ANALYSIS,
            base_url="https://fitter.example.org/run", credential=self.cred,
            default_params={"model": "salt2", "iterations": 100},
            created_by=self.staff, modified_by=self.staff,
        )

    def test_start_run_merges_defaults_and_hashes_token(self):
        run, token = svc.start_run(self.service, self.user, {"iterations": 5},
                                   transient=self.transient, dispatch=False)
        self.assertEqual(run.status, ExternalServiceRun.STATUS_PENDING)
        self.assertEqual(run.request_payload, {"model": "salt2", "iterations": 5})
        self.assertEqual(run.created_by, self.user)
        self.assertEqual(run.transient, self.transient)
        self.assertEqual(run.target_display, "svcrun01")
        self.assertNotEqual(run.callback_token_hash, token)
        self.assertTrue(svc.verify_callback_token(run, token))
        self.assertFalse(svc.verify_callback_token(run, token + "x"))
        self.assertFalse(svc.verify_callback_token(run, None))
        self.assertIn("/api/service_runs/%s/callback/" % run.uuid, svc.callback_url(run))
        self.assertIn(str(run.uuid)[:8], str(run))

    def test_generic_target_and_free_ref(self):
        run, _ = svc.start_run(self.service, self.user, target=self.cred, target_ref="ignored", dispatch=False)
        run = ExternalServiceRun.objects.get(pk=run.pk)
        self.assertEqual(run.target, self.cred)
        self.assertEqual(run.target_display, "encryptedcredential #%d" % self.cred.pk)
        run2, _ = svc.start_run(self.service, self.user, target_ref="mast:jw01234", dispatch=False)
        self.assertEqual(run2.target_display, "mast:jw01234")

    def test_lifecycle_success(self):
        run, _ = svc.start_run(self.service, self.user, dispatch=False)
        run.mark_running(external_id="job-7")
        self.assertEqual(run.attempts, 1)
        self.assertIsNotNone(run.started_at)
        svc.record_completion(run, "succeeded", result={"chi2": 1.2}, artifact_url="https://x/plot.png")
        run = ExternalServiceRun.objects.get(pk=run.pk)
        self.assertEqual(run.status, "succeeded")
        self.assertTrue(run.is_finished)
        self.assertEqual(run.result, {"chi2": 1.2})
        self.assertEqual(run.artifact_link, "https://x/plot.png")
        self.assertEqual(run.external_id, "job-7")
        self.assertIsNotNone(run.duration)
        with self.assertRaises(svc.InvalidTransition):
            svc.record_completion(run, "failed", error="late")
        svc.record_completion(run, "failed", error="forced", allow_repeat=True)
        self.assertEqual(ExternalServiceRun.objects.get(pk=run.pk).status, "failed")

    def test_lifecycle_failure_and_cancel(self):
        run, _ = svc.start_run(self.service, self.user, dispatch=False)
        svc.record_completion(run, "failed", error="boom")
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error, "boom")
        self.assertIsNotNone(run.started_at)
        run2, _ = svc.start_run(self.service, self.user, dispatch=False)
        run2.mark_cancelled("user asked")
        self.assertEqual(run2.status, "cancelled")
        with self.assertRaises(ValueError):
            svc.record_completion(run, "pending")

    def test_disabled_service_and_group_visibility(self):
        self.service.enabled = False
        self.service.save()
        with self.assertRaises(svc.ServiceDisabled):
            svc.start_run(self.service, self.user, dispatch=False)
        self.service.enabled = True
        self.service.save()
        group = Group.objects.create(name="fitters")
        self.service.groups.add(group)
        with self.assertRaises(svc.ServiceDisabled):
            svc.start_run(self.service, self.user, dispatch=False)
        self.user.groups.add(group)
        svc.start_run(self.service, self.user, dispatch=False)
        svc.start_run(self.service, self.staff, dispatch=False)  # staff always

    def test_per_user_daily_cap(self):
        self.service.max_runs_per_user_per_day = 2
        self.service.save()
        svc.start_run(self.service, self.user, dispatch=False)
        run, _ = svc.start_run(self.service, self.user, dispatch=False)
        with self.assertRaises(svc.RunLimitExceeded) as ctx:
            svc.start_run(self.service, self.user, dispatch=False)
        self.assertEqual(ctx.exception.limit, 2)
        svc.start_run(self.service, self.staff, dispatch=False)  # other user unaffected
        run.mark_cancelled()
        svc.start_run(self.service, self.user, dispatch=False)  # cancelled runs do not count

    def test_dispatch_enqueues_a_job(self):
        from YSE_App.models import Job

        run, _ = svc.start_run(self.service, self.user, transient=self.transient)
        jobs = Job.objects.filter(kind=svc.JOB_KIND)
        self.assertEqual(jobs.count(), 1)
        job = jobs.get()
        self.assertEqual(job.payload, {"run_id": run.pk})
        self.assertEqual(job.created_by, self.user)
        self.assertEqual(job.transient, self.transient)
        self.assertEqual(ExternalServiceRun.objects.get(pk=run.pk).status, "pending")

    def test_dispatch_inline_runs_handler_and_leaves_run_pending(self):
        from YSE_App.models import Job
        from YSE_App.services.job_queue import get_handler

        self.assertIsNotNone(get_handler(svc.JOB_KIND))
        with override_settings(JOB_RUNNER_INLINE=True):
            run, _ = svc.start_run(self.service, self.user)
        job = Job.objects.get(kind=svc.JOB_KIND)
        self.assertEqual(job.status, Job.DONE)
        self.assertEqual(job.result, {"run": str(run.uuid), "handled": False})
        self.assertEqual(ExternalServiceRun.objects.get(pk=run.pk).status, "pending")

    def test_dispatch_without_queue_returns_false(self):
        from unittest import mock

        with mock.patch.object(svc, "_enqueue", None):
            run, _ = svc.start_run(self.service, self.user)
            self.assertFalse(svc.dispatch_run(run))
        self.assertEqual(ExternalServiceRun.objects.get(pk=run.pk).status, "pending")

    def test_expire_runs(self):
        from datetime import timedelta

        from django.utils import timezone

        old, _ = svc.start_run(self.service, self.user, dispatch=False)
        svc.record_completion(old, "succeeded")
        ExternalServiceRun.objects.filter(pk=old.pk).update(finished_at=timezone.now() - timedelta(days=100))
        fresh, _ = svc.start_run(self.service, self.user, dispatch=False)
        svc.record_completion(fresh, "succeeded")
        pending, _ = svc.start_run(self.service, self.user, dispatch=False)
        out = io.StringIO()
        call_command("expire_service_runs", "--days", "90", "--dry-run", stdout=out)
        self.assertIn("would delete 1", out.getvalue())
        self.assertEqual(ExternalServiceRun.objects.count(), 3)
        call_command("expire_service_runs", "--days", "90", stdout=io.StringIO())
        self.assertEqual(set(ExternalServiceRun.objects.values_list("pk", flat=True)), {fresh.pk, pending.pk})

    def test_service_credential_roundtrip(self):
        self.assertEqual(self.service.credential.get_secret(), SECRET)
        self.cred.delete()
        self.service.refresh_from_db()
        self.assertIsNone(self.service.credential)


@override_settings(CREDENTIALS_KEY=KEY_A)
class CallbackEndpointTests(TestCase):
    def setUp(self):
        self.staff = create_test_user("cb_staff")
        self.service = ExternalService.objects.create(name="Cb", slug="cb", created_by=self.staff, modified_by=self.staff)
        self.run, self.token = svc.start_run(self.service, self.staff, target_ref="t", dispatch=False)
        self.url = reverse("external_service_run_callback", kwargs={"run_uuid": str(self.run.uuid)})
        self.client = Client()

    def _post(self, body, **extra):
        return self.client.post(self.url, data=json.dumps(body), content_type="application/json", **extra)

    def test_requires_token(self):
        resp = self._post({"status": "succeeded"})
        self.assertEqual(resp.status_code, 403)
        resp = self._post({"status": "succeeded"}, HTTP_AUTHORIZATION="Bearer wrong")
        self.assertEqual(resp.status_code, 403)
        resp = self._post({"status": "succeeded", "token": "wrong"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(ExternalServiceRun.objects.get(pk=self.run.pk).status, "pending")

    def test_bearer_token_success(self):
        resp = self._post({"status": "succeeded", "result": {"z": 0.03}, "artifact_url": "https://x/a.png",
                           "external_id": "abc"}, HTTP_AUTHORIZATION="Bearer " + self.token)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json(), {"uuid": str(self.run.uuid), "status": "succeeded"})
        run = ExternalServiceRun.objects.get(pk=self.run.pk)
        self.assertEqual(run.result, {"z": 0.03})
        self.assertEqual(run.artifact_url, "https://x/a.png")
        self.assertEqual(run.external_id, "abc")
        self.assertIsNotNone(run.finished_at)
        # A second completion is a conflict.
        resp = self._post({"status": "failed"}, HTTP_AUTHORIZATION="Bearer " + self.token)
        self.assertEqual(resp.status_code, 409)

    def test_header_and_body_tokens(self):
        resp = self._post({"status": "running", "external_id": "j1"}, HTTP_X_RUN_TOKEN=self.token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ExternalServiceRun.objects.get(pk=self.run.pk).status, "running")
        resp = self._post({"status": "failed", "error": "diverged", "token": self.token})
        self.assertEqual(resp.status_code, 200)
        run = ExternalServiceRun.objects.get(pk=self.run.pk)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error, "diverged")
        resp = self._post({"status": "running", "token": self.token})
        self.assertEqual(resp.status_code, 409)

    def test_bad_requests(self):
        resp = self._post({"status": "sideways"}, HTTP_AUTHORIZATION="Bearer " + self.token)
        self.assertEqual(resp.status_code, 400)
        resp = self._post({"status": "succeeded", "result": "text"}, HTTP_AUTHORIZATION="Bearer " + self.token)
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(self.url, data="not json", content_type="application/json",
                                HTTP_AUTHORIZATION="Bearer " + self.token)
        self.assertEqual(resp.status_code, 400)
        resp = self.client.get(self.url, HTTP_AUTHORIZATION="Bearer " + self.token)
        self.assertEqual(resp.status_code, 405)
        other = reverse("external_service_run_callback", kwargs={"run_uuid": "00000000-0000-0000-0000-000000000000"})
        resp = self.client.post(other, data="{}", content_type="application/json",
                                HTTP_AUTHORIZATION="Bearer " + self.token)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(ExternalServiceRun.objects.get(pk=self.run.pk).status, "pending")


@override_settings(CREDENTIALS_KEY=KEY_A)
class StaffRunPagesTests(TestCase):
    def setUp(self):
        self.staff = create_test_user("pg_staff")
        self.user = create_test_user("pg_user", is_staff=False)
        self.transient = create_minimal_transient(self.staff, name="pgrun01")
        self.cred = _cred(self.staff, service="pg")
        self.service = ExternalService.objects.create(name="Pager", slug="pg", credential=self.cred,
                                                      created_by=self.staff, modified_by=self.staff)
        self.run, _ = svc.start_run(self.service, self.staff, {"depth": 3}, transient=self.transient, dispatch=False)
        svc.record_completion(self.run, "succeeded", result={"answer": 42})
        self.other, _ = svc.start_run(self.service, self.staff, target_ref="free", dispatch=False)
        self.client = Client()

    def test_staff_only(self):
        for url in (reverse("external_service_runs"),
                    reverse("external_service_run_detail", args=[str(self.run.uuid)])):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.client.force_login(self.user)
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)  # staff_member_required redirects
            self.client.logout()

    def test_list_and_filters(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("external_service_runs"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("pgrun01", body)
        self.assertIn("free", body)
        self.assertIn('data-status="succeeded"', body)
        self.assertIn('data-status="pending"', body)
        resp = self.client.get(reverse("external_service_runs"), {"status": "pending", "service": "pg"})
        body = resp.content.decode()
        self.assertIn("free", body)
        self.assertNotIn("pgrun01", body)
        resp = self.client.get(reverse("external_service_runs"), {"service": "nope"})
        self.assertContains(resp, "No runs recorded")

    def test_detail_shows_payload_result_and_no_secret(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("external_service_run_detail", args=[str(self.run.uuid)]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('&quot;depth&quot;: 3', body)
        self.assertIn('&quot;answer&quot;: 42', body)
        self.assertIn("/api/service_runs/%s/callback/" % self.run.uuid, body)
        for value in SECRET.values():
            self.assertNotIn(value, body)
        self.assertNotIn(self.run.callback_token_hash, body)
        resp = self.client.get(reverse("external_service_run_detail", args=["00000000-0000-0000-0000-000000000000"]))
        self.assertEqual(resp.status_code, 404)

    def test_nav_link_for_staff_only(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("external_service_runs"))
        self.assertContains(resp, "Service Runs")
        self.client.force_login(self.user)
        resp = self.client.get(reverse("transient_tags"))
        self.assertNotContains(resp, "Service Runs")

    def test_admin_run_pages_render(self):
        admin = User.objects.create_superuser("pg_admin", "a@example.com", "pw")
        self.client.force_login(admin)
        for url in (reverse("admin:YSE_App_externalservice_changelist"),
                    reverse("admin:YSE_App_externalservice_change", args=[self.service.pk]),
                    reverse("admin:YSE_App_externalservicerun_changelist"),
                    reverse("admin:YSE_App_externalservicerun_change", args=[self.run.pk])):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            for value in SECRET.values():
                self.assertNotIn(value, resp.content.decode(), url)


class ExternalServiceAdminFormTests(TestCase):
    def test_default_params_round_trip_through_admin(self):
        admin = User.objects.create_superuser("svc_admin", "a@example.com", "pw")
        client = Client()
        client.force_login(admin)
        resp = client.post(reverse("admin:YSE_App_externalservice_add"), {
            "name": "Fitter", "slug": "fitter", "kind": "analysis", "description": "", "base_url": "",
            "credential": "", "enabled": "on", "default_params": '{"model": "salt2", "n": 3}',
            "max_runs_per_user_per_day": "0",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        service = ExternalService.objects.get(slug="fitter")
        self.assertEqual(service.default_params, {"model": "salt2", "n": 3})
        self.assertEqual(service.created_by, admin)
        resp = client.get(reverse("admin:YSE_App_externalservice_change", args=[service.pk]))
        self.assertContains(resp, "&quot;salt2&quot;")
        resp = client.post(reverse("admin:YSE_App_externalservice_change", args=[service.pk]), {
            "name": "Fitter", "slug": "fitter", "kind": "analysis", "description": "", "base_url": "",
            "credential": "", "enabled": "on", "default_params": "[1, 2]", "max_runs_per_user_per_day": "0",
        })
        self.assertContains(resp, "must be a JSON object")
