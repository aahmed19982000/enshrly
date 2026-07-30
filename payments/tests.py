import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import CustomerProfile
from .models import DevicePairing, SubscriptionPackage, Transaction

# Every test in this file overrides WHATSAPP_BOT_URL='' so accounts.utils._send_via_whatsapp_bot
# stays in its simulated/no-op mode (returns True, only logs) instead of depending on whatever
# a real .env might have configured - the suite must never make a real outbound call.
NO_WHATSAPP = override_settings(WHATSAPP_BOT_URL='')


def _build_paymob_webhook_payload(merchant_order_id, success=True, paymob_tx_id=12345):
    """
    Builds an `obj` payload shaped like a real Paymob webhook body, using the
    exact same field set/order the view signs, so tests can compute a
    genuinely valid HMAC rather than mocking the comparison itself.
    """
    obj = {
        'amount_cents': 10000,
        'created_at': '2026-01-01T00:00:00.000000',
        'currency': 'EGP',
        'error_occured': False,
        'has_parent_transaction': False,
        'id': paymob_tx_id,
        'integration_id': 5792603,
        'is_3d_secure': True,
        'is_auth': False,
        'is_capture': False,
        'is_voided': False,
        'is_refunded': False,
        'owner': 998877,
        'pending': False,
        'source_data': {'pan': '1234', 'sub_type': 'MasterCard', 'type': 'card'},
        'success': success,
        'order': {'merchant_order_id': str(merchant_order_id)},
    }
    return obj


def _sign_paymob_obj(obj, hmac_key):
    source_data = obj.get('source_data', {})
    str_to_sign = (
        f"{obj.get('amount_cents')}{obj.get('created_at')}{obj.get('currency')}{obj.get('error_occured')}"
        f"{obj.get('has_parent_transaction')}{obj.get('id')}{obj.get('integration_id')}{obj.get('is_3d_secure')}"
        f"{obj.get('is_auth')}{obj.get('is_capture')}{obj.get('is_voided')}{obj.get('is_refunded')}{obj.get('owner')}{obj.get('pending')}"
        f"{source_data.get('pan', '')}{source_data.get('sub_type', '')}{source_data.get('type', '')}{obj.get('success')}"
    )
    return hmac.new(hmac_key.encode('utf-8'), str_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()


class PaymentsTestBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='201000000001', password='testpass123', first_name='Ahmed')
        self.profile = CustomerProfile.objects.create(
            user=self.user, whatsapp_number='201000000001', is_whatsapp_verified=True,
        )
        self.package = SubscriptionPackage.objects.create(
            name='Basic', price=Decimal('10.00'), daily_limit=5, features='x',
        )


@NO_WHATSAPP
class PaymobWebhookTests(PaymentsTestBase):
    def setUp(self):
        super().setUp()
        self.transaction = Transaction.objects.create(
            customer=self.profile, package=self.package, amount=Decimal('10.00'),
            currency='EGP', gateway='paymob', status='pending',
        )
        self.url = reverse('payments:paymob_webhook')
        self.hmac_key = 'test-hmac-secret'

    @override_settings(PAYMOB_HMAC_KEY='test-hmac-secret')
    def test_valid_signature_completes_transaction(self):
        obj = _build_paymob_webhook_payload(self.transaction.transaction_id, success=True)
        valid_hmac = _sign_paymob_obj(obj, self.hmac_key)

        resp = self.client.post(
            f"{self.url}?hmac={valid_hmac}",
            data=json.dumps({'obj': obj}), content_type='application/json',
        )

        self.assertEqual(resp.status_code, 200)
        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.status, 'completed')
        self.assertTrue(self.transaction.verified_transaction_id.startswith('PAYMOB-'))

    @override_settings(PAYMOB_HMAC_KEY='test-hmac-secret')
    def test_missing_hmac_param_rejected(self):
        obj = _build_paymob_webhook_payload(self.transaction.transaction_id, success=True)

        resp = self.client.post(self.url, data=json.dumps({'obj': obj}), content_type='application/json')

        self.assertEqual(resp.status_code, 401)
        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.status, 'pending')

    @override_settings(PAYMOB_HMAC_KEY='test-hmac-secret')
    def test_tampered_signature_rejected(self):
        obj = _build_paymob_webhook_payload(self.transaction.transaction_id, success=True)
        # Sign with the WRONG key, simulating a forged/tampered webhook call.
        bad_hmac = _sign_paymob_obj(obj, 'not-the-real-key')

        resp = self.client.post(
            f"{self.url}?hmac={bad_hmac}",
            data=json.dumps({'obj': obj}), content_type='application/json',
        )

        self.assertEqual(resp.status_code, 401)
        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.status, 'pending')

    @override_settings(PAYMOB_HMAC_KEY='')
    def test_missing_server_hmac_key_rejected(self):
        obj = _build_paymob_webhook_payload(self.transaction.transaction_id, success=True)

        resp = self.client.post(
            f"{self.url}?hmac=anything",
            data=json.dumps({'obj': obj}), content_type='application/json',
        )

        self.assertEqual(resp.status_code, 500)
        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.status, 'pending')

    @override_settings(PAYMOB_HMAC_KEY='test-hmac-secret')
    def test_redelivered_webhook_does_not_double_issue_token(self):
        from syndicator.models import WPConnectionToken

        obj = _build_paymob_webhook_payload(self.transaction.transaction_id, success=True)
        valid_hmac = _sign_paymob_obj(obj, self.hmac_key)

        self.client.post(f"{self.url}?hmac={valid_hmac}", data=json.dumps({'obj': obj}), content_type='application/json')
        self.client.post(f"{self.url}?hmac={valid_hmac}", data=json.dumps({'obj': obj}), content_type='application/json')

        self.assertEqual(WPConnectionToken.objects.filter(customer=self.profile).count(), 1)


@NO_WHATSAPP
class PaymobCallbackTests(PaymentsTestBase):
    def setUp(self):
        super().setUp()
        self.transaction = Transaction.objects.create(
            customer=self.profile, package=self.package, amount=Decimal('10.00'),
            currency='EGP', gateway='paymob', status='pending',
        )
        self.client.force_login(self.user)

    def test_forged_success_query_param_never_completes_transaction(self):
        url = reverse('payments:paymob_callback')

        resp = self.client.get(f"{url}?merchant_order_id={self.transaction.transaction_id}&success=true")

        self.assertEqual(resp.status_code, 302)
        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.status, 'pending')

    def test_already_completed_transaction_redirects_to_success(self):
        self.transaction.status = 'completed'
        self.transaction.save(update_fields=['status'])
        url = reverse('payments:paymob_callback')

        resp = self.client.get(f"{url}?merchant_order_id={self.transaction.transaction_id}")

        self.assertRedirects(resp, reverse('payments:payment_success', kwargs={'transaction_id': self.transaction.transaction_id}))


@NO_WHATSAPP
class CheckoutViewTests(PaymentsTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse('payments:checkout', kwargs={'package_id': self.package.id})

    def test_local_gateway_creates_one_pending_transaction(self):
        resp = self.client.post(self.url, data={'gateway': 'local', 'currency': 'USD', 'billing_period': 'monthly'})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Transaction.objects.filter(customer=self.profile, gateway='local').count(), 1)

    def test_repeat_local_checkout_reuses_pending_transaction(self):
        self.client.post(self.url, data={'gateway': 'local', 'currency': 'USD', 'billing_period': 'monthly', 'sender_phone': '201111111111'})
        self.client.post(self.url, data={'gateway': 'local', 'currency': 'USD', 'billing_period': 'monthly', 'sender_phone': '201111111111'})

        self.assertEqual(Transaction.objects.filter(customer=self.profile, gateway='local').count(), 1)

    def test_disabled_gateway_is_rejected(self):
        from syndicator.models import AISettings
        ai_settings = AISettings.get_settings()
        ai_settings.enable_paymob_gateway = False
        ai_settings.save(update_fields=['enable_paymob_gateway'])

        resp = self.client.post(self.url, data={'gateway': 'paymob', 'currency': 'USD', 'billing_period': 'monthly'})

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Transaction.objects.filter(customer=self.profile, gateway='paymob').count(), 0)

    def test_unverified_customer_is_redirected_to_otp(self):
        self.profile.is_whatsapp_verified = False
        self.profile.save(update_fields=['is_whatsapp_verified'])

        resp = self.client.post(self.url, data={'gateway': 'local', 'currency': 'USD', 'billing_period': 'monthly'})

        self.assertRedirects(resp, reverse('accounts:verify_otp'))


@override_settings(PAIRING_TOKEN='real-pair-token', WALLET_API_KEY='real-wallet-key')
class LocalWalletPairingTests(TestCase):
    def test_correct_pairing_token_returns_api_key_and_flips_paired(self):
        url = reverse('payments:confirm_pairing')

        resp = self.client.post(url, data=json.dumps({'pair_token': 'real-pair-token'}), content_type='application/json')

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['data']['api_key'], 'real-wallet-key')
        self.assertTrue(DevicePairing.get_singleton().paired)

    def test_wrong_pairing_token_rejected(self):
        url = reverse('payments:confirm_pairing')

        resp = self.client.post(url, data=json.dumps({'pair_token': 'wrong-token'}), content_type='application/json')

        self.assertEqual(resp.status_code, 401)
        self.assertFalse(DevicePairing.get_singleton().paired)


@NO_WHATSAPP
@override_settings(WALLET_API_KEY='real-wallet-key')
class MobileWalletTransactionTests(PaymentsTestBase):
    def setUp(self):
        super().setUp()
        self.transaction = Transaction.objects.create(
            customer=self.profile, package=self.package, amount=Decimal('100.00'),
            currency='EGP', gateway='local', status='pending', sender_phone='201234567890',
        )
        self.url = reverse('payments:mobile_post_transaction')

    def _post(self, payload, license_key='real-wallet-key'):
        headers = {'HTTP_X_LICENSE_KEY': license_key} if license_key else {}
        return self.client.post(self.url, data=json.dumps(payload), content_type='application/json', **headers)

    def test_missing_api_key_rejected(self):
        resp = self._post({'transaction_id': 'TX1', 'type': 'RECEIVED', 'amount': '100.00'}, license_key=None)

        self.assertEqual(resp.status_code, 401)

    def test_valid_transfer_completes_matching_transaction_and_issues_token(self):
        from syndicator.models import WPConnectionToken

        resp = self._post({
            'transaction_id': 'TX-REAL-1', 'type': 'RECEIVED', 'amount': '100.00',
            'counterpart': '01234567890',
        })

        self.assertEqual(resp.status_code, 200)
        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.status, 'completed')
        self.assertEqual(WPConnectionToken.objects.filter(customer=self.profile).count(), 1)

    def test_replayed_transaction_id_is_not_processed_twice(self):
        payload = {'transaction_id': 'TX-REAL-2', 'type': 'RECEIVED', 'amount': '100.00', 'counterpart': '01234567890'}

        first = self._post(payload)
        self.assertEqual(first.json()['success'], True)

        # A second, different pending transaction that would otherwise match by amount.
        Transaction.objects.create(
            customer=self.profile, package=self.package, amount=Decimal('100.00'),
            currency='EGP', gateway='local', status='pending', sender_phone='201234567890',
        )
        second = self._post(payload)

        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()['success'], False)


@NO_WHATSAPP
class PayPalConfirmPaymentTests(PaymentsTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.transaction = Transaction.objects.create(
            customer=self.profile, package=self.package, amount=Decimal('10.00'),
            currency='USD', gateway='paypal', status='pending',
        )
        self.url = reverse('payments:confirm_paypal_payment')

    @override_settings(PAYPAL_CLIENT_SECRET='fake-secret', PAYPAL_CLIENT_ID='fake-id')
    @patch('payments.views.requests.get')
    @patch('payments.views.get_paypal_access_token', return_value='fake-access-token')
    def test_completed_order_marks_transaction_completed(self, _mock_token, mock_get):
        from syndicator.models import WPConnectionToken
        mock_get.return_value.json.return_value = {'status': 'COMPLETED'}

        resp = self.client.post(self.url, data=json.dumps({
            'orderID': 'ORDER123', 'transactionID': str(self.transaction.transaction_id),
        }), content_type='application/json')

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['success'])
        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.status, 'completed')
        self.assertEqual(WPConnectionToken.objects.filter(customer=self.profile).count(), 1)

    @override_settings(PAYPAL_CLIENT_SECRET='fake-secret', PAYPAL_CLIENT_ID='fake-id')
    @patch('payments.views.requests.get')
    @patch('payments.views.get_paypal_access_token', return_value='fake-access-token')
    def test_non_completed_order_does_not_complete_transaction(self, _mock_token, mock_get):
        mock_get.return_value.json.return_value = {'status': 'VOIDED'}

        resp = self.client.post(self.url, data=json.dumps({
            'orderID': 'ORDER123', 'transactionID': str(self.transaction.transaction_id),
        }), content_type='application/json')

        self.assertEqual(resp.status_code, 400)
        self.transaction.refresh_from_db()
        self.assertEqual(self.transaction.status, 'pending')
