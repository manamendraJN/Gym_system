from django.db import models
from django.db.models.functions import Lower
from django.urls import reverse


class Supplier(models.Model):
    """Shared by equipment, inventory and expenses."""

    name = models.CharField(max_length=150)
    contact_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("name"), name="unique_supplier_name_ci")]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("suppliers:list")
