from django.db import migrations

CATEGORIES = ["Rent", "Electricity", "Water", "Salaries", "Equipment", "Maintenance", "Cleaning supplies",
              "Inventory & supplies", "Marketing", "Other operating costs"]


def seed(apps, schema_editor):
    ExpenseCategory = apps.get_model("expenses", "ExpenseCategory")
    for name in CATEGORIES:
        if not ExpenseCategory.objects.filter(name__iexact=name).exists():
            ExpenseCategory.objects.create(name=name)


class Migration(migrations.Migration):
    dependencies = [("expenses", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
