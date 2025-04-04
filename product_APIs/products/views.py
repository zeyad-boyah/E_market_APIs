from rest_framework import generics, status
from . import serializers
from . import models
from rest_framework.pagination import PageNumberPagination
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response 
from django_filters.rest_framework import DjangoFilterBackend
from .filters import ProductFilter
from django.db import transaction
from django.db.models import F

# customize the pagination from the endpoint 
class ProductPagination(PageNumberPagination):
    page_size = 10  # 10 items per page (default)
    page_size_query_param = 'page_size'  # Allow users to specify their page size
    max_page_size = 100  # Max page size allowed

class ProductsListAPIView(generics.ListAPIView):
    queryset= models.Product.objects.all()
    serializer_class = serializers.ProductSerializer
    pagination_class = ProductPagination
    # DjangoFilterBackend is already the global filter backend but i want to add SearchFilters as well
    filter_backends= [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class  = ProductFilter
    # this will make it so that you can search with those fields using the rest_framework.filters.SearchFilter
    search_fields = ['name', 'description']
    # uses rest_framework.filters.OrderingFilter to "order" 🥹🥹🥹
    ordering_fields = ['price']


class CartItemCreateAPIView(generics.GenericAPIView):
    serializer_class = serializers.AddToCartSerializer
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        product_id = serializer.validated_data['product_id']
        quantity = serializer.validated_data['quantity']
        user = request.user

        # use Atomic transaction to ensure that when you add an item to the cart no one else buys it 
        with transaction.atomic():
            # Lock the product row for update
            product = models.Product.objects.select_for_update().get(id=product_id)
            cart, _ = models.ShoppingCart.objects.get_or_create(user=user)

            # Get or create cart item with atomic update
            cart_item, created = models.CartItem.objects.get_or_create(
                product=product,
                shopping_cart=cart,
                defaults={'quantity': quantity}
            )

            if not created:
                # Use F() expression to prevent race conditions
                cart_item.quantity = F('quantity') + quantity
                cart_item.save(update_fields=['quantity'])
                # update all the cart_item with the latest values after the change done to quantity
                # as the change happens on the data base level only when using F()
                cart_item.refresh_from_db()

            # Double-check stock after update
            if cart_item.quantity > product.stock_count:
                cart_item.quantity = product.stock_count
                cart_item.save()
                raise serializers.ValidationError(
                    "Quantity reduced to available stock limit."
                )
        # all good and the product was added :))))
        return Response(
            serializers.ShoppingCartSerializer(cart).data,
            status=status.HTTP_200_OK
        )
    

class ShoppingCartAPIView(generics.RetrieveAPIView):
    serializer_class = serializers.ShoppingCartSerializer
    permission_classes = [IsAuthenticated,]

    def get_object(self):
        cart, created = models.ShoppingCart.objects.get_or_create(user=self.request.user)
        return cart
    

class CartItemUpdateDeleteAPIView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = serializers.CartItemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return models.CartItem.objects.filter(shopping_cart__user=self.request.user)

    # change the serializer in the case of partial update or full update on an item 
    def get_serializer_class(self):
        if self.request.method in ['PUT', 'PATCH']:
            return serializers.CartItemUpdateSerializer
        return super().get_serializer_class()
    
    
    # atomic transaction is needed her since this endpoint is capable of increasing the quantity of 
    # product so the stock need to be checked first  
    # also to avoid Concurrency Risks
    @transaction.atomic
    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        # partial= True for patch requests
        serializer = self.get_serializer(instance, data=request.data, partial=True)  
        serializer.is_valid(raise_exception=True)

        new_quantity = serializer.validated_data.get('quantity', instance.quantity)
        product = instance.product
        message = None  # Initialize message

        with transaction.atomic():
            # Lock product and cart item for update
            product = models.Product.objects.select_for_update().get(id=product.id)
            cart_item = models.CartItem.objects.select_for_update().get(pk=instance.pk)

            # Check stock availability
            if new_quantity > product.stock_count:
                available = product.stock_count
                if available == 0:
                    cart_item.delete()
                    return Response(
                        {"detail": "Item removed from cart as product is out of stock"},
                        status=status.HTTP_200_OK
                    )
                new_quantity = available
                message = f"Quantity reduced to available stock ({available})"

            # Update quantity
            cart_item.quantity = new_quantity
            cart_item.save()

            # Double-check stock after update
            if cart_item.quantity > product.stock_count:
                cart_item.quantity = product.stock_count
                cart_item.save()
                message = "Quantity adjusted to current stock limit"

        response_data = serializers.CartItemSerializer(cart_item).data
        if message:
            response_data = {"detail": message, "data": response_data}
            
        return Response(response_data, status=status.HTTP_200_OK)
    
    # delete an item from the cart
    def perform_destroy(self, instance):
        with transaction.atomic():
            # No need to update stock here since we're removing from cart
            instance.delete()
