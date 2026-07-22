import requests
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

def send_whatsapp_otp(phone_number, otp_code):
    """
    Sends an OTP code via WhatsApp using Infobip API.
    """
    api_key = getattr(settings, 'INFOBIP_API_KEY', '')
    base_url = getattr(settings, 'INFOBIP_BASE_URL', '')
    sender_number = getattr(settings, 'INFOBIP_SENDER', '')

    if not api_key or not base_url:
        logger.warning(f"Infobip credentials missing. Simulated OTP {otp_code} for {phone_number}")
        # Return True for local development
        return True

    url = f"{base_url}/whatsapp/1/message/text"

    headers = {
        "Authorization": f"App {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Ensure phone number is in international format without '+'
    formatted_number = phone_number.replace('+', '').replace(' ', '')

    payload = {
        "from": sender_number,
        "to": formatted_number,
        "content": {
            "text": f"مرحباً بك في خدمة النشر الآلي! كود التفعيل الخاص بك هو: {otp_code}"
        }
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Infobip Error: {str(e)} - {response.text if hasattr(response, 'text') else ''}")
        return False
