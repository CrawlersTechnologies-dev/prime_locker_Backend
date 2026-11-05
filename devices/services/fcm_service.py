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

        try:
            import firebase_admin
            from firebase_admin import messaging

            # If Firebase Admin is initialized with credentials, send message
            if firebase_admin._apps:
                message = messaging.Message(
                    data=data_payload,
                    token=device.fcm_token,
                    android=messaging.AndroidConfig(priority='high')
                )
                response = messaging.send(message)
                logger.info(f"FCM push sent to device {device.imei} ({command_type}): {response}")
                return {"success": True, "message_id": response}
            else:
                logger.info(f"[FCM SIMULATION] Command '{command_type}' queued for device {device.imei} (FCM: {device.fcm_token[:20]}...)")
                return {"success": True, "simulated": True}
        except Exception as e:
            logger.warning(f"FCM dispatch error for device {device.imei}: {e}")
            return {"success": False, "error": str(e)}
