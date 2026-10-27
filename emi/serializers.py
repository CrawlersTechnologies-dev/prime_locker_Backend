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
    late_fee = serializers.ReadOnlyField()
    total_due = serializers.ReadOnlyField()

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
            'days_overdue',
            'late_fee',
            'total_due'
        ]
        read_only_fields = [
            'id', 'contract', 'installment_number', 'due_date', 'amount',
            'paid_amount', 'status', 'collected_by', 'collection_date',
            'days_overdue', 'late_fee', 'total_due'
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
    lock_device_imei = serializers.ReadOnlyField(source='lock_device.imei')
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
            'lock_device',
            'lock_device_imei',
            'product_type',
            'product_brand',
            'product_model',
            'product_serial_number',
            'product_price',
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
    Supports optional collection_date for recording payments back-dated (e.g. yesterday).
    """
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=True)
    payment_mode = serializers.ChoiceField(choices=PaymentMode.choices, default=PaymentMode.CASH)
    transaction_ref = serializers.CharField(max_length=100, required=False, allow_blank=True)
    collection_date = serializers.DateTimeField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class VerifyOnlinePaymentSerializer(serializers.Serializer):
    """
    Serializer for verifying Cashfree payment response from mobile or web app.
    """
    order_id = serializers.CharField(max_length=150, required=False, allow_blank=True)
    cashfree_order_id = serializers.CharField(max_length=150, required=False, allow_blank=True)

    def validate(self, attrs):
        order_id = attrs.get('order_id') or attrs.get('cashfree_order_id')
        if not order_id:
            raise serializers.ValidationError("Either 'order_id' or 'cashfree_order_id' must be provided.")
        return attrs


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


class PaymentItemSerializer(serializers.ModelSerializer):
    """
    Serializer for Payments Screen (Screen A-23).
    Displays all collected installments with full context.
    """
    receipt_number = serializers.SerializerMethodField()
    customer = serializers.SerializerMethodField()
    retailer = serializers.SerializerMethodField()
    device = serializers.SerializerMethodField()
    collected_by_name = serializers.SerializerMethodField()

    class Meta:
        model = EMIInstallment
        fields = [
            'id',
            'receipt_number',
            'installment_number',
            'paid_amount',
            'amount',
            'payment_mode',
            'collection_date',
            'transaction_ref',
            'notes',
            'collected_by',
            'collected_by_name',
            'customer',
            'retailer',
            'device'
        ]

    def get_receipt_number(self, obj):
        return f"REC-{obj.id:06d}"

    def get_customer(self, obj):
        c = obj.contract.customer
        name = f"{c.first_name} {c.last_name}".strip() or c.username
        return {
            "id": c.id,
            "name": name,
            "mobile_number": c.mobile_number,
            "email": c.email
        }

    def get_retailer(self, obj):
        r = obj.contract.retailer
        name = f"{r.first_name} {r.last_name}".strip() or r.username
        return {
            "id": r.id,
            "name": name,
            "store_name": r.business_name or name,
            "mobile_number": r.mobile_number
        }

    def get_device(self, obj):
        return {
            "imei": obj.contract.device_imei,
            "device_name": obj.contract.device_name,
            "brand": obj.contract.device_brand,
            "model": obj.contract.device_model
        }

    def get_collected_by_name(self, obj):
        if obj.collected_by:
            name = f"{obj.collected_by.first_name} {obj.collected_by.last_name}".strip()
            return name or obj.collected_by.username
        return "System"


class PaymentReceiptSerializer(PaymentItemSerializer):
    """
    Serializer for Payment Receipt (Screen A-25).
    Includes complete financial contract breakdown and customer/store addresses.
    """
    contract_summary = serializers.SerializerMethodField()
    customer_address = serializers.SerializerMethodField()
    retailer_address = serializers.SerializerMethodField()

    class Meta(PaymentItemSerializer.Meta):
        fields = PaymentItemSerializer.Meta.fields + [
            'contract_summary',
            'customer_address',
            'retailer_address'
        ]

    def get_contract_summary(self, obj):
        con = obj.contract
        return {
            "contract_id": con.id,
            "device_imei": con.device_imei,
            "device_name": con.device_name,
            "total_device_price": con.total_amount,
            "down_payment": con.down_payment,
            "financed_amount": con.financed_amount,
            "tenure_months": con.tenure_months,
            "monthly_emi_amount": con.monthly_emi_amount,
            "total_paid": con.total_paid,
            "total_remaining": con.total_remaining,
            "paid_installments_count": con.paid_installments_count,
            "remaining_installments_count": con.remaining_installments_count,
            "status": con.status
        }

    def get_customer_address(self, obj):
        c = obj.contract.customer
        return {
            "address": c.address or "",
            "city": c.city or "",
            "state": c.state or "",
            "pincode": c.pincode or ""
        }

    def get_retailer_address(self, obj):
        r = obj.contract.retailer
        return {
            "business_name": r.business_name or "",
            "address": r.address or "",
            "city": r.city or "",
            "state": r.state or "",
            "pincode": r.pincode or ""
        }

