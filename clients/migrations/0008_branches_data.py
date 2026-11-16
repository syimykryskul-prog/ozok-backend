"""C5: основной филиал на каждого клиента; seller/implementer/responsible = франчайзи клиента; подписки — к филиалу."""
from django.db import migrations


def forwards(apps, schema_editor):
    Client = apps.get_model("clients", "Client")
    Branch = apps.get_model("clients", "Branch")
    Subscription = apps.get_model("clients", "Subscription")
    for client in Client.objects.all():
        if client.owner_partner_id is None:
            client.owner_partner_id = client.franchisee_id
            client.save(update_fields=["owner_partner"])
        branch = Branch.objects.filter(client=client, is_main=True).first()
        if branch is None:
            lead = client.lead
            branch = Branch.objects.create(
                client=client, name="Основной", address=client.address, city=client.city, region=client.region,
                is_main=True, seller_partner_id=client.franchisee_id, implementer_partner_id=client.franchisee_id,
                responsible_partner_id=client.franchisee_id, lead=lead, lead_source=lead.source if lead else "",
                status="active")
        Subscription.objects.filter(client=client, branch__isnull=True).update(branch=branch)
    Subscription.objects.filter(status="blocked").update(status="restricted")


def backwards(apps, schema_editor):
    apps.get_model("clients", "Subscription").objects.filter(status="restricted").update(status="blocked")


class Migration(migrations.Migration):
    dependencies = [("clients", "0007_client_inn_client_legal_name_client_owner_partner_and_more")]
    operations = [migrations.RunPython(forwards, backwards)]
