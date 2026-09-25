from rest_framework.routers import DefaultRouter

from .views import ExpenseCategoryViewSet, ExpenseViewSet, TurnoverViewSet, WalletTransactionViewSet, WalletViewSet

router = DefaultRouter()
router.register('wallets', WalletViewSet)
router.register('wallet-transactions', WalletTransactionViewSet, basename='wallet-transaction')
router.register('expense-categories', ExpenseCategoryViewSet)
router.register('expenses', ExpenseViewSet)
router.register('turnovers', TurnoverViewSet)

urlpatterns = router.urls
