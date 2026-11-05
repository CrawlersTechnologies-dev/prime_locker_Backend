from django.apps import AppConfig
import os
import logging

logger = logging.getLogger(__name__)


class DevicesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'devices'

    def ready(self):
        # Auto-initialize Firebase Admin SDK if credentials JSON exists
        from django.conf import settings
        fb_creds_path = getattr(settings, 'FIREBASE_CREDENTIALS', None) or os.getenv('FIREBASE_CREDENTIALS') or os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
        default_json_path = os.path.join(settings.BASE_DIR, 'firebase-service-account.json')
        if not fb_creds_path and os.path.exists(default_json_path):
            fb_creds_path = default_json_path

        if fb_creds_path and os.path.exists(fb_creds_path):
            try:
                import firebase_admin
                from firebase_admin import credentials
                if not firebase_admin._apps:
                    cred = credentials.Certificate(fb_creds_path)
                    firebase_admin.initialize_app(cred)
                    logger.info("Firebase Admin SDK successfully initialized from %s", fb_creds_path)
            except Exception as e:
                logger.warning("Failed to initialize Firebase Admin SDK: %s", e)
