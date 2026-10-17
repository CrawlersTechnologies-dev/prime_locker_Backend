"""
Apple MDM / Supervised DEP Integration Service.
Handles communication with Apple Business Manager / MDM Gateway for iOS devices.
If credentials are not configured in settings, operates in MOCK / SIMULATION mode.
"""
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class AppleMDMService:
    @staticmethod
    def get_config():
        return getattr(settings, 'APPLE_MDM_CONFIG', {
            'BASE_URL': '',
            'API_KEY': '',
            'TENANT_ID': ''
        })

    @classmethod
    def send_command(cls, device, command_type, payload=None):
        """
        Sends an MDM command to the Apple Supervised device.
        Supported commands:
          - LOCK -> DeviceLock
          - UNLOCK -> ClearPasscode / UnlockUserAccount
          - REBOOT -> RestartDevice
          - UPDATE_POLICY -> Settings / Restrictions (Camera, Factory Reset, FRP)
          - SET_WALLPAPER -> SetWallpaper
        """
        config = cls.get_config()
        device_identifier = device.serial_number or device.imei

        # Map internal CommandType to Apple MDM command payload
        apple_command_mapping = {
            'LOCK': 'DeviceLock',
            'UNLOCK': 'ClearPasscode',
            'REBOOT': 'RestartDevice',
            'UPDATE_POLICY': 'ApplyRestrictions',
            'SET_WALLPAPER': 'SetWallpaper',
            'PUSH_MESSAGE': 'ShowUserMessage'
        }

        apple_cmd = apple_command_mapping.get(command_type, command_type)

        # If no 3rd-party API key configured, operate gracefully in simulation mode
        if not config.get('API_KEY') or not config.get('BASE_URL'):
            logger.info(
                f"[SIMULATION] Apple MDM command '{apple_cmd}' queued for iOS device {device_identifier}. "
                f"Payload: {payload}"
            )
            return {
                "status": "SIMULATED",
                "message": f"Apple MDM '{apple_cmd}' simulated successfully (No external API key set).",
                "device_identifier": device_identifier,
                "command": apple_cmd
            }

        # Real 3rd-Party Gateway API Call (e.g. MicroMDM, SimpleMDM, or Apple APNs)
        headers = {
            "Authorization": f"Bearer {config['API_KEY']}",
            "Content-Type": "application/json"
        }
        url = f"{config['BASE_URL']}/devices/{device_identifier}/commands"

        body = {
            "command": apple_cmd,
            "tenant_id": config.get('TENANT_ID', ''),
            "parameters": payload or {}
        }

        try:
            response = requests.post(url, json=body, headers=headers, timeout=10)
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Error calling Apple MDM Gateway for device {device_identifier}: {e}")
            return {
                "status": "ERROR",
                "detail": str(e),
                "device_identifier": device_identifier
            }
