from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from .models import MedicineBatch, StockMovement


@transaction.atomic
def change_stock(*, batch, quantity_change, movement_type, reference, reason, user):
    locked = MedicineBatch.objects.select_for_update().get(pk=batch.pk)
    existing = StockMovement.objects.filter(reference=reference).first()
    if existing:
        if (existing.batch_id != locked.pk or existing.quantity_change != Decimal(quantity_change) or
                existing.movement_type != movement_type):
            raise serializers.ValidationError({'reference': 'This stock reference was already used for another movement.'})
        return existing
    if Decimal(quantity_change) == 0:
        raise serializers.ValidationError({'quantity_change': 'Quantity change cannot be zero.'})
    new_quantity = locked.quantity_available + Decimal(quantity_change)
    if new_quantity < 0:
        raise serializers.ValidationError({'quantity': f'Insufficient stock in batch {locked.batch_number}.'})
    if new_quantity > locked.quantity_received:
        raise serializers.ValidationError({'quantity': f'Stock cannot exceed received quantity for batch {locked.batch_number}.'})
    movement = StockMovement.objects.create(
        batch=locked, movement_type=movement_type, quantity_change=quantity_change,
        quantity_after=new_quantity, reference=reference, reason=reason, created_by=user,
    )
    locked.quantity_available = new_quantity
    locked.save(update_fields=('quantity_available', 'updated_at'))
    return movement
