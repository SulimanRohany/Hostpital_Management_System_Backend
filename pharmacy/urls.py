from rest_framework.routers import DefaultRouter

from .views import (
    MedicineBatchViewSet, MedicineCategoryViewSet, MedicineViewSet, PurchaseViewSet,
    SaleViewSet, StockMovementViewSet, SupplierPaymentViewSet, SupplierViewSet,
)

router = DefaultRouter()
router.register('medicine-categories', MedicineCategoryViewSet)
router.register('suppliers', SupplierViewSet)
router.register('medicines', MedicineViewSet)
router.register('medicine-batches', MedicineBatchViewSet, basename='medicine-batch')
router.register('purchases', PurchaseViewSet)
router.register('sales', SaleViewSet)
router.register('stock-movements', StockMovementViewSet, basename='stock-movement')
router.register('supplier-payments', SupplierPaymentViewSet)

urlpatterns = router.urls
