from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils import timezone
from decimal import Decimal
import math

from .models import (
    EMIPlanTemplate,
    CustomerEMIContract,
    EMIInstallment,
    InstallmentStatus,
    PaymentMode,
    EMIContractStatus,
    add_months
)

User = get_user_model()


class EMIPlanTemplateSerializer(serializers.ModelSerializer):
    """
    Serializer for Master EMI Plans (Screen A-19).
    Created & managed exclusively by Super-Admin.
    """
    created_by_name = serializers.SerializerMethodField(read_only=True)
    active_contracts_count = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = EMIPlanTemplate
        fields = [
            'id',
            'name',
            'description',
            'device_type',
            'tenure_months',
            'min_down_payment_percentage',
            'processing_fee',
            'interest_rate_percentage',
            'grace_period_days',
            'is_active',
            'created_by',
            'created_by_name',
            'active_contracts_count',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            name = f"{obj.created_by.first_name} {obj.created_by.last_name}".strip()
            return name or obj.created_by.username
        return None

    def get_active_contracts_count(self, obj):
        return obj.contracts.filter(status=EMIContractStatus.ACTIVE).count()


class EMIInstallmentSerializer(serializers.ModelSerializer):
    """
    Serializer for individual installment schedule rows (Screen A-21).
    """
    collected_by_name = serializers.SerializerMethodField(read_only=True)
    days_overdue = serializers.ReadOnlyField()

    class Meta:
        model = EMIInstallment
        fields = [
            'id',
            'contract',
            'installment_number',
            'due_date',
            'amount',
            'paid_amount',
            'status',
            'collected_by',
            'collected_by_name',
            'payment_mode',
            'collection_date',
            'transaction_ref',
            'notes',
            'days_overdue'
        ]
        read_only_fields = [
            'id', 'contract', 'installment_number', 'due_date', 'amount',
            'paid_amount', 'status', 'collected_by', 'collection_date', 'days_overdue'
        ]

    def get_collected_by_name(self, obj):
        if obj.collected_by:
            name = f"{obj.collected_by.first_name} {obj.collected_by.last_name}".strip()
            return name or obj.collected_by.username
        return None


class CustomerEMIContractSerializer(serializers.ModelSerializer):
    """
    Serializer for setting up and managing an EMI contract (Screen A-20).
    """
    customer_details = serializers.SerializerMethodField(read_only=True)
    retailer_details = serializers.SerializerMethodField(read_only=True)
    plan_template_name = serializers.ReadOnlyField(source='plan_template.name')
    total_paid = serializers.ReadOnlyField()
    total_remaining = serializers.ReadOnlyField()
    paid_installments_count = serializers.ReadOnlyField()
    remaining_installments_count = serializers.ReadOnlyField()

    class Meta:
        model = CustomerEMIContract
        fields = [
            'id',
            'customer',
            'customer_details',
            'retailer',
            'retailer_details',
            'plan_template',
            'plan_template_name',
            'device_imei',
            'device_name',
            'device_type',
            'device_state',
            'device_brand',
            'device_model',
            'loan_provider',
            'loan_provider_other',
            'loan_account_number',
            'total_amount',
            'down_payment',
            'processing_fee',
            'financed_amount',
            'tenure_months',
            'monthly_emi_amount',
            'first_emi_date',
            'auto_lock_after_grace',
            'grace_period_days',
            'whatsapp_reminders',
            'status',
            'total_paid',
            'total_remaining',
            'paid_installments_count',
            'remaining_installments_count',
            'created_at',
            'updated_at'
        ]
        read_only_fields = ['id', 'retailer', 'financed_amount', 'monthly_emi_amount', 'created_at', 'updated_at']

    def get_customer_details(self, obj):
        c = obj.customer
        name = f"{c.first_name} {c.last_name}".strip() or c.username
        return {
            'id': c.id,
            'name': name,
            'mobile_number': c.mobile_number,
            'alternate_mobile_number': c.alternate_mobile_number,
            'email': c.email
        }

    def get_retailer_details(self, obj):
        r = obj.retailer
        name = f"{r.first_name} {r.last_name}".strip() or r.username
        return {
            'id': r.id,
            'name': name,
            'business_name': r.business_name,
            'mobile_number': r.mobile_number
        }

    def validate(self, attrs):
        total_amount = attrs.get('total_amount')
        down_payment = attrs.get('down_payment', Decimal('0.00'))
        processing_fee = attrs.get('processing_fee', Decimal('0.00'))
        tenure_months = attrs.get('tenure_months', 12)
        plan_template = attrs.get('plan_template')

        if down_payment > total_amount:
            raise serializers.ValidationError({"down_payment": "Down payment cannot exceed total device price."})

        # Calculate financed amount
        # Principal = Total - Down Payment + Processing Fee + (Optional Interest Markup)
        principal = total_amount - down_payment + processing_fee
        interest_pct = Decimal('0.00')
        if plan_template and plan_template.interest_rate_percentage:
            interest_pct = plan_template.interest_rate_percentage

        interest_amount = (principal * interest_pct) / Decimal('100.00')
        financed_amount = principal + interest_amount

        if tenure_months <= 0:
            raise serializers.ValidationError({"tenure_months": "Tenure must be at least 1 month."})

        monthly_emi = financed_amount / Decimal(tenure_months)

        attrs['financed_amount'] = round(financed_amount, 2)
        attrs['monthly_emi_amount'] = round(monthly_emi, 2)
        return attrs

    def create(self, validated_data):
        contract = CustomerEMIContract.objects.create(**validated_data)
        
        # Automatically generate the schedule installments
        tenure = contract.tenure_months
        first_date = contract.first_emi_date
        monthly_amount = contract.monthly_emi_amount
        
        installments = []
        for i in range(1, tenure + 1):
            due_date = add_months(first_date, i - 1)
            # Check if this first installment is already due
            init_status = InstallmentStatus.UPCOMING
            if due_date <= timezone.localdate():
                init_status = InstallmentStatus.DUE
                
            installments.append(
                EMIInstallment(
                    contract=contract,
                    installment_number=i,
                    due_date=due_date,
                    amount=monthly_amount,
                    status=init_status
                )
            )
        EMIInstallment.objects.bulk_create(installments)

        from devices.models import Device
        device = Device.objects.filter(imei=contract.device_imei).first()
        if device:
            device.emi_contract = contract
            device.save(update_fields=['emi_contract'])

        return contract


class RecordPaymentSerializer(serializers.Serializer):
    """
    Serializer for collecting payment on an installment (Screen A-21 Record Payment).
    """
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=True)
    payment_mode = serializers.ChoiceField(choices=PaymentMode.choices, default=PaymentMode.CASH)
    transaction_ref = serializers.CharField(max_length=100, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class OverdueCustomerItemSerializer(serializers.ModelSerializer):
    """
    Serializer for Overdue Customers list (Screen A-22).
    """
    customer_name = serializers.SerializerMethodField()
    customer_phone = serializers.CharField(source='customer.mobile_number', read_only=True)
    retailer_name = serializers.SerializerMethodField()
    retailer_store = serializers.CharField(source='retailer.business_name', read_only=True)
    overdue_installment_id = serializers.IntegerField(read_only=True)
    due_date = serializers.DateField(read_only=True)
    overdue_amount = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    days_overdue = serializers.IntegerField(read_only=True)
    device_status = serializers.CharField(source='status', read_only=True)

    class Meta:
        model = CustomerEMIContract
        fields = [
            'id',
            'customer_name',
            'customer_phone',
            'device_name',
            'device_imei',
            'device_status',
            'retailer_name',
            'retailer_store',
            'overdue_installment_id',
            'due_date',
            'overdue_amount',
            'days_overdue'
        ]

    def get_customer_name(self, obj):
        c = obj.customer
        name = f"{c.first_name} {c.last_name}".strip()
        return name or c.username

    def get_retailer_name(self, obj):
        r = obj.retailer
        name = f"{r.first_name} {r.last_name}".strip()
        return name or r.username
