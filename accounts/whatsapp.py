import logging
from .models import WhatsAppMessageLog, PlatformSettings

logger = logging.getLogger(__name__)

class WhatsAppService:
    """
    WhatsApp Business API Service Adapter.
    Supports Simulation Mode (default) and live providers (WATI, GUPSHUP, INTERAKT).
    """

    TEMPLATES = {
        'emi_due_reminder': {
            'description': 'EMI payment due reminder before due date',
            'sample_body': 'Dear {{customer_name}}, your EMI installment of Rs {{amount}} for {{device_name}} is due on {{due_date}}. Pay online to avoid phone lock: {{payment_link}}'
        },
        'emi_overdue_warning': {
            'description': 'Overdue EMI reminder warning about device lock',
            'sample_body': 'Urgent: Your EMI of Rs {{amount}} is overdue. Please pay immediately to prevent automatic device locking: {{payment_link}}'
        },
        'device_locked_notice': {
            'description': 'Notice that device has been locked due to missed EMI',
            'sample_body': 'Notice: Your device (IMEI: {{device_imei}}) has been locked due to overdue payment of Rs {{amount}}. Pay now to unlock immediately: {{payment_link}}'
        },
        'payment_receipt_success': {
            'description': 'Confirmation receipt on successful EMI payment',
            'sample_body': 'Thank you {{customer_name}}! We received Rs {{amount}} (Receipt: {{receipt_number}}). Your device is active and unlocked.'
        },
        'welcome_onboarding': {
            'description': 'Welcome message on successful enrollment',
            'sample_body': 'Welcome {{customer_name}} to Prime Locker! Your device {{device_name}} has been enrolled under EMI plan. Monthly due: Rs {{monthly_emi}}.'
        }
    }

    @classmethod
    def send_template(cls, phone_number, template_name, parameters=None, recipient_name=None):
        parameters = parameters or {}
        settings = PlatformSettings.get_settings()
        provider = settings.whatsapp_provider or 'SIMULATION'
        
        # Format phone to include country code if missing
        clean_phone = ''.join(c for c in str(phone_number) if c.isdigit())
        if len(clean_phone) == 10:
            clean_phone = '91' + clean_phone

        log_entry = WhatsAppMessageLog(
            recipient_phone=clean_phone,
            recipient_name=recipient_name or '',
            template_name=template_name,
            parameters=parameters,
            provider=provider
        )

        if provider == 'SIMULATION' or not settings.whatsapp_api_key:
            log_entry.status = 'SIMULATED'
            log_entry.response_payload = {
                'simulated': True,
                'message': f"WhatsApp message simulated to {clean_phone} using template '{template_name}'",
                'parameters': parameters
            }
            log_entry.save()
            logger.info(f"[WHATSAPP SIMULATION] Sent '{template_name}' to {clean_phone}: {parameters}")
            return {'status': 'SIMULATED', 'message_id': log_entry.id}

        # If live provider configured (WATI / GUPSHUP / INTERAKT):
        try:
            import requests
            if provider.upper() == 'WATI':
                url = f"https://api.wati.io/api/v1/sendTemplateMessage?whatsappNumber={clean_phone}"
                headers = {"Authorization": f"Bearer {settings.whatsapp_api_key}"}
                payload = {"template_name": template_name, "broadcast_name": template_name, "parameters": parameters}
                res = requests.post(url, json=payload, headers=headers, timeout=10)
                log_entry.status = 'SENT' if res.status_code == 200 else 'FAILED'
                log_entry.response_payload = res.json() if res.content else {}
            else:
                log_entry.status = 'SIMULATED'
                log_entry.response_payload = {'note': f'Provider {provider} configured, live dispatch simulated'}
            
            log_entry.save()
            return {'status': log_entry.status, 'message_id': log_entry.id}
        except Exception as e:
            log_entry.status = 'FAILED'
            log_entry.response_payload = {'error': str(e)}
            log_entry.save()
            logger.error(f"[WHATSAPP ERROR] Failed sending to {clean_phone}: {e}")
            return {'status': 'FAILED', 'error': str(e)}
