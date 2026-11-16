from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "billing"

    def ready(self):
        from django.db.models.signals import post_save

        from network.models import Franchisee

        from . import recruiters
        from .models import FranchiseePayment

        def on_payment(sender, instance, **kwargs):
            if instance.contract_id:
                recruiters.recompute(instance.franchisee)

        def on_partner(sender, instance, **kwargs):
            if instance.recruited_by_id:
                recruiters.ensure_reward(instance)

        from clients.models import ClientSale

        def on_sale(sender, instance, created, **kwargs):
            if created and not kwargs.get("raw"):
                from . import accruals
                accruals.on_sale_created(instance)

        post_save.connect(on_sale, sender=ClientSale, dispatch_uid="accrual_on_sale")

        from .models import CommissionPayout, Payment

        def cash_payment(sender, instance, created, **kwargs):
            if created and instance.status == "confirmed" and not kwargs.get("raw"):
                from . import cash
                cash.on_payment_confirmed(instance)

        def cash_fr_payment(sender, instance, created, **kwargs):
            if kwargs.get("raw"):
                return
            from . import cash
            if created:
                cash.on_franchisee_payment(instance)
            elif instance.cancelled_at is not None:
                cash.on_franchisee_payment_cancelled(instance)

        def cash_payout(sender, instance, created, **kwargs):
            if kwargs.get("raw"):
                return
            from . import cash
            if created:
                cash.on_legacy_payout(instance)
                if instance.method != "offset":
                    from . import legacy
                    from .models import CommissionAccrual
                    legacy.apply_legacy_credit(instance.franchisee_id, CommissionAccrual, CommissionPayout)
            elif instance.cancelled_at is not None:
                cash.on_legacy_payout_cancelled(instance)

        post_save.connect(cash_payment, sender=Payment, dispatch_uid="cash_on_payment")
        post_save.connect(cash_fr_payment, sender=FranchiseePayment, dispatch_uid="cash_on_fr_payment")
        post_save.connect(cash_payout, sender=CommissionPayout, dispatch_uid="cash_on_payout")
        post_save.connect(on_payment, sender=FranchiseePayment, dispatch_uid="reward_on_payment")
        post_save.connect(on_partner, sender=Franchisee, dispatch_uid="reward_on_partner")
