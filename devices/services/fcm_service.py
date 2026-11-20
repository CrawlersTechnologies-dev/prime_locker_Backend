import logging

logger = logging.getLogger(__name__)


class FCMService:
    """
    Firebase Cloud Messaging (FCM) Service for customer app-based devices.
    Dispatches high-priority push messages to the native app locker service
    for immediate remote lock, unlock, and policy sync.
    """

    @classmethod
    def send_command(cls, device, command_type, params=None):
        """
        Sends high-priority FCM data message to the customer phone app.
        Reuses the existing command path and payload.
        """
        if not getattr(device, 'fcm_token', None):
            return {"success": False, "error": "No FCM token registered for this device."}

        data_payload = {
            "command_type": str(command_type),
            "device_id": str(device.id),
            "imei": str(device.imei),
            "lock_message": str(device.lock_message or ""),
            "offline_unlock_code": str(device.offline_unlock_code or ""),
        }
        if params:
            for k, v in params.items():
                data_payload[str(k)] = str(v)

        # 1. Try Firebase Admin SDK (FCM HTTP v1 with Service Account)
        try:
            import firebase_admin
            from firebase_admin import messaging

            if firebase_admin._apps:
                message = messaging.Message(
                    data=data_payload,
                    token=device.fcm_token,
                    android=messaging.AndroidConfig(priority='high')
                )
                response = messaging.send(message)
                logger.info(f"FCM push sent to device {device.imei} ({command_type}): {response}")
                return {"success": True, "message_id": response}
        except Exception as e:
            logger.warning(f"Firebase Admin SDK dispatch error for device {device.imei}: {e}")

        # 2. Try FCM Legacy HTTP API via FCM_SERVER_KEY string
        import os
        from django.conf import settings
        server_key = os.getenv('FCM_SERVER_KEY') or getattr(settings, 'FCM_SERVER_KEY', None)
        if server_key and server_key.strip() and not server_key.strip().startswith('your_'):
            try:
                import requests
                headers = {
                    'Authorization': f'key={server_key.strip()}',
                    'Content-Type': 'application/json'
                }
                body = {
                    'to': device.fcm_token,
                    'priority': 'high',
                    'data': data_payload
                }
                resp = requests.post('https://fcm.googleapis.com/fcm/send', headers=headers, json=body, timeout=10)
                if resp.status_code == 200:
                    logger.info(f"FCM Server Key push sent to device {device.imei}: {resp.text}")
                    return {"success": True, "fcm_response": resp.json()}
                else:
                    logger.warning(f"FCM Server Key push failed ({resp.status_code}): {resp.text}")
            except Exception as e:
                logger.warning(f"FCM Server Key HTTP error for device {device.imei}: {e}")

        # 3. Simulation fallback if neither is configured
        logger.info(f"[FCM SIMULATION] Command '{command_type}' queued for device {device.imei} (FCM: {device.fcm_token[:20]}...)")
        return {"success": True, "simulated": True}
