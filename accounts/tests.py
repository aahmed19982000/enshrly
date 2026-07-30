from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import CustomerProfile, PasswordResetOTP, WhatsAppOTP

# accounts.utils._send_via_whatsapp_bot stays simulated (returns True, no network
# call) with an empty bot URL - overridden explicitly so the suite never depends
# on whatever a real .env might have configured. Django's test runner already
# swaps EMAIL_BACKEND to locmem automatically, so OTP emails need no override.
NO_WHATSAPP = override_settings(WHATSAPP_BOT_URL='')


class RateLimitedTestCase(TestCase):
    """
    check_rate_limit is backed by Django's process-level cache, which - unlike
    the DB - is NOT reset by TestCase's per-test transaction rollback. Without
    clearing it, a rate-limit counter set by one test leaks into the next.
    """
    def setUp(self):
        cache.clear()


@NO_WHATSAPP
class SignupTests(RateLimitedTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse('accounts:signup')

    def _signup(self, whatsapp='201000000001', email='a@example.com'):
        return self.client.post(self.url, data={
            'name': 'Ahmed', 'whatsapp': whatsapp, 'email': email, 'password': 'testpass123',
        })

    def test_successful_signup_creates_user_profile_and_otp(self):
        resp = self._signup()

        self.assertRedirects(resp, reverse('accounts:verify_otp'))
        user = User.objects.get(username='201000000001')
        profile = user.customer_profile
        self.assertFalse(profile.is_whatsapp_verified)
        self.assertEqual(WhatsAppOTP.objects.filter(customer=profile).count(), 1)

    def test_signup_rate_limited_per_ip_after_five(self):
        for i in range(5):
            self._signup(whatsapp=f'20100000000{i}', email=f'{i}@example.com')

        User.objects.all().delete()  # so the 6th attempt isn't blocked by "already registered" instead
        resp = self._signup(whatsapp='201099999999', email='new@example.com')

        self.assertRedirects(resp, reverse('accounts:signup'))
        self.assertFalse(User.objects.filter(username='201099999999').exists())

    def test_signup_rate_limited_per_phone_after_three(self):
        # Each call re-triggers the "exists but unverified -> resend OTP" branch,
        # which still counts against the same per-phone rate-limit key.
        for _ in range(3):
            self._signup(whatsapp='201055555555')

        otp_count_before = WhatsAppOTP.objects.filter(customer__whatsapp_number='201055555555').count()
        self._signup(whatsapp='201055555555')
        otp_count_after = WhatsAppOTP.objects.filter(customer__whatsapp_number='201055555555').count()

        self.assertEqual(otp_count_before, otp_count_after)


@NO_WHATSAPP
class OTPVerifyTests(RateLimitedTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username='201000000002', password='testpass123')
        self.profile = CustomerProfile.objects.create(user=self.user, whatsapp_number='201000000002')
        self.client.force_login(self.user)
        self.url = reverse('accounts:verify_otp')

    def test_correct_code_verifies_account(self):
        otp = WhatsAppOTP.objects.create(customer=self.profile)

        resp = self.client.post(self.url, data={'otp_code': otp.otp_code})

        self.assertRedirects(resp, reverse('accounts:dashboard'))
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.is_whatsapp_verified)

    def test_wrong_code_does_not_verify(self):
        WhatsAppOTP.objects.create(customer=self.profile)

        self.client.post(self.url, data={'otp_code': '0000'})

        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_whatsapp_verified)

    def test_expired_code_is_rejected_even_if_correct(self):
        otp = WhatsAppOTP.objects.create(customer=self.profile)
        otp.expires_at = timezone.now() - timedelta(minutes=1)
        otp.save(update_fields=['expires_at'])

        self.client.post(self.url, data={'otp_code': otp.otp_code})

        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_whatsapp_verified)

    def test_five_wrong_attempts_trip_rate_limit(self):
        otp = WhatsAppOTP.objects.create(customer=self.profile)
        for _ in range(5):
            self.client.post(self.url, data={'otp_code': '0000'})

        # Even the CORRECT code is now blocked until the rate-limit window passes.
        self.client.post(self.url, data={'otp_code': otp.otp_code})

        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_whatsapp_verified)


@NO_WHATSAPP
class LoginTests(RateLimitedTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username='201000000003', password='testpass123')
        self.profile = CustomerProfile.objects.create(
            user=self.user, whatsapp_number='201000000003', is_whatsapp_verified=True,
        )
        self.url = reverse('accounts:login')

    def test_correct_credentials_verified_profile_goes_to_dashboard(self):
        resp = self.client.post(self.url, data={'whatsapp': '201000000003', 'password': 'testpass123'})

        self.assertRedirects(resp, reverse('accounts:dashboard'))

    def test_correct_credentials_unverified_profile_goes_to_otp(self):
        self.profile.is_whatsapp_verified = False
        self.profile.save(update_fields=['is_whatsapp_verified'])

        resp = self.client.post(self.url, data={'whatsapp': '201000000003', 'password': 'testpass123'})

        self.assertRedirects(resp, reverse('accounts:verify_otp'))

    def test_five_failed_attempts_trip_rate_limit(self):
        for _ in range(5):
            self.client.post(self.url, data={'whatsapp': '201000000003', 'password': 'wrongpass'})

        # Even the CORRECT password is now blocked until the rate-limit window passes.
        resp = self.client.post(self.url, data={'whatsapp': '201000000003', 'password': 'testpass123'})

        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.wsgi_request.user.is_authenticated)


@NO_WHATSAPP
class PasswordResetTests(RateLimitedTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            username='201000000004', password='oldpass123', email='reset@example.com',
        )
        self.profile = CustomerProfile.objects.create(user=self.user, whatsapp_number='201000000004')

    def test_valid_reset_otp_changes_password(self):
        self.client.post(reverse('accounts:forgot_password'), data={'email': 'reset@example.com'})
        otp = PasswordResetOTP.objects.filter(customer=self.profile).latest('created_at')

        resp = self.client.post(reverse('accounts:reset_password'), data={
            'otp_code': otp.otp_code, 'new_password': 'newpass456',
        })

        self.assertRedirects(resp, reverse('accounts:login'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('newpass456'))

    def test_used_reset_otp_is_rejected(self):
        self.client.post(reverse('accounts:forgot_password'), data={'email': 'reset@example.com'})
        otp = PasswordResetOTP.objects.filter(customer=self.profile).latest('created_at')
        otp.is_used = True
        otp.save(update_fields=['is_used'])

        self.client.post(reverse('accounts:reset_password'), data={
            'otp_code': otp.otp_code, 'new_password': 'newpass456',
        })

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('oldpass123'))
