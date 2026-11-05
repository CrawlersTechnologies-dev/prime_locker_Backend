from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('devices', '0008_device_fcm_token'),
    ]

    operations = [
        migrations.AlterField(
            model_name='appversion',
            name='app_type',
            field=models.CharField(
                choices=[
                    ('ANDROID_LOCKER', 'Android Locker APK'),
                    ('TV_LOCKER', 'Smart TV Locker APK'),
                    ('RETAILER_APP', 'Retailer Management App'),
                    ('CUSTOMER_APP', 'Customer Mobile App'),
                    ('IOS_AGENT', 'iOS / Apple DEP Profile')
                ],
                default='ANDROID_LOCKER',
                max_length=30
            ),
        ),
    ]
