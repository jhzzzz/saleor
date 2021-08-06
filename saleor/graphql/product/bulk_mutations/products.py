import datetime
import graphene
from collections import defaultdict
from decimal import Decimal
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.db import transaction
from django.utils.text import slugify
from measurement.measures import Weight

from ....core.permissions import ProductPermissions, ProductTypePermissions
from ....menu import models as menu_models
from ....order import OrderStatus, models as order_models
from ....product import models
from ....product.error_codes import ProductErrorCode
from ....product.tasks import update_product_minimal_variant_price_task
from ....product.thumbnails import create_product_images_from_url
from ....product.utils import delete_categories
from ....product.utils.attributes import generate_name_for_variant, \
    associate_attribute_values_to_instance
from ....third.shopify import Shopify
from ....warehouse import models as warehouse_models
from ....warehouse.error_codes import StockErrorCode
from ...core.mutations import (
    BaseBulkMutation,
    BaseMutation,
    ModelBulkDeleteMutation,
    ModelMutation,
)
from ...core.types.common import (
    BulkProductError,
    BulkStockError,
    ProductError,
    StockError,
)
from ...core.utils import get_duplicated_values
from ...core.validators import validate_price_precision
from ...utils import resolve_global_ids_to_primary_keys
from ...warehouse.types import Warehouse
from ..mutations.products import (
    AttributeAssignmentMixin,
    AttributeValueInput,
    ProductVariantCreate,
    ProductVariantInput,
    StockInput,
)
from ..types import Product, ProductVariant
from ..utils import create_stocks, get_used_variants_attribute_values


class CategoryBulkDelete(ModelBulkDeleteMutation):
    class Arguments:
        ids = graphene.List(
            graphene.ID, required=True, description="List of category IDs to delete."
        )

    class Meta:
        description = "Deletes categories."
        model = models.Category
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"

    @classmethod
    def bulk_action(cls, queryset):
        delete_categories(queryset.values_list("pk", flat=True))


class CollectionBulkDelete(ModelBulkDeleteMutation):
    class Arguments:
        ids = graphene.List(
            graphene.ID, required=True, description="List of collection IDs to delete."
        )

    class Meta:
        description = "Deletes collections."
        model = models.Collection
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"


class CollectionBulkPublish(BaseBulkMutation):
    class Arguments:
        ids = graphene.List(
            graphene.ID,
            required=True,
            description="List of collections IDs to (un)publish.",
        )
        is_published = graphene.Boolean(
            required=True,
            description="Determine if collections will be published or not.",
        )

    class Meta:
        description = "Publish collections."
        model = models.Collection
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"

    @classmethod
    def bulk_action(cls, queryset, is_published):
        queryset.update(is_published=is_published)


class ProductBulkCreateFromShopify(BaseMutation):
    # for code challenge, just hard code some properties
    DEF_PRODUCT_TYPE_ID = 16
    DEF_PRODUCT_CATEGORY_ID = 24
    DEF_SIZE_ATTRIBUTE_ID = 13
    DEF_COLOR_ATTRIBUTE_ID = 14
    DEF_WAREHOUSE_ID = "74b279b5-77a5-49d3-9ba6-789eac7a2829"
    DEF_MENU_ITEM_ID = 20

    size_color_cache = {}
    def_warehouse_cache = None

    products = graphene.Field(
        Product, description="List of imported products."
    )

    class Arguments:
        shop_url = graphene.String(
            required=True,
            description="A Shopify website URL, e.g. <SHOP-NAME>.myshopify.com"
        )
        access_token = graphene.String(
            required=True,
            description="An access token for the above website",
        )
        collection_id = graphene.ID(
            required=True,
            description="The ID of the collection to be imported",
        )

    class Meta:
        model = models.Product
        description = "Bulk import products from shopify collection."
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"

    @classmethod
    def create_attribute_values(cls, attribute_id, values):
        if attribute_id not in cls.size_color_cache:
            attribute_cache = {
                "attribute": models.Attribute.objects.get(pk=attribute_id),
                "values": models.AttributeValue.objects.filter(
                    attribute_id=attribute_id
                )
            }
            cls.size_color_cache[attribute_id] = attribute_cache
        else:
            attribute_cache = cls.size_color_cache[attribute_id]

        for val in values:
            val = val.replace(' ', '-')  # data bug hack
            try:
                exist_attr_val = next(
                    v for v in attribute_cache["values"].iterator() if v.name == val
                )
            except StopIteration:
                exist_attr_val = None

            if not exist_attr_val:
                new_attribute_val = models.AttributeValue.objects.create(
                    attribute=attribute_cache["attribute"], name=val, slug=slugify(val)
                )
                attribute_cache["values"] |= models.AttributeValue.objects.filter(
                    pk=new_attribute_val.pk
                )

    @classmethod
    def get_attribute_value(cls, attribute_id, value):
        value = value.replace(' ', '-')
        attribute_cache = cls.size_color_cache[attribute_id]
        exist_attr_val = next(
            v for v in attribute_cache["values"].iterator() if v.name == value
        )

        if not exist_attr_val:
            raise ObjectDoesNotExist("attribute with value not exist: " + value)

        return attribute_cache["attribute"], exist_attr_val

    @classmethod
    def get_default_warehouse(cls):
        if not cls.def_warehouse_cache:
            cls.def_warehouse_cache = warehouse_models.Warehouse.objects.get(
                pk=cls.DEF_WAREHOUSE_ID
            )
        return cls.def_warehouse_cache

    @classmethod
    def create_variants(cls, product, shopify_product):
        size_attr_index, color_attr_index = cls.get_size_color_index(shopify_product)
        if not (size_attr_index > 0 and color_attr_index > 0):
            return

        new_variants = []
        new_stocks = []
        for variant in shopify_product.variants:
            if not (variant.sku and variant.price):
                continue

            weight = Weight()
            setattr(weight, variant.weight_unit, variant.weight)
            new_variant = models.ProductVariant(
                product=product,
                weight=weight,
                sku=variant.sku,
                price_amount=Decimal(variant.price)
            )

            size_attr, exist_size = cls.get_attribute_value(
                cls.DEF_SIZE_ATTRIBUTE_ID,
                getattr(variant, "option" + str(size_attr_index))
            )
            color_attr, exist_color = cls.get_attribute_value(
                cls.DEF_COLOR_ATTRIBUTE_ID,
                getattr(variant, "option" + str(color_attr_index))
            )

            new_variant.save()
            new_stock = warehouse_models.Stock(
                warehouse=cls.get_default_warehouse(),
                product_variant=new_variant,
                quantity=variant.inventory_quantity
            )
            new_stocks.append(new_stock)

            associate_attribute_values_to_instance(new_variant, size_attr, exist_size)
            associate_attribute_values_to_instance(new_variant, color_attr, exist_color)
            new_variants.append(new_variant)

        warehouse_models.Stock.objects.bulk_create(new_stocks)
        return new_variants

    @classmethod
    def get_size_color_index(cls, product):
        size_attr_index = 0
        color_attr_index = 0
        for option in product.options:
            name = option.name.lower()
            if name == 'color':
                color_attr_index = option.position
            elif name == 'size':
                size_attr_index = option.position
        return size_attr_index, color_attr_index

    @classmethod
    def create_color_sizes(cls, shopify_products):
        size_values = []
        color_values = []
        for spd in shopify_products:
            size_attr_index, color_attr_index = cls.get_size_color_index(spd)
            if not (size_attr_index > 0 and color_attr_index > 0):
                continue

            for variant in spd.variants:
                size_val = getattr(variant, "option" + str(size_attr_index))
                color_val = getattr(variant, "option" + str(color_attr_index))
                if not (size_val in size_values):
                    size_values.append(size_val)
                if not (color_val in color_values):
                    color_values.append(color_val)

        cls.create_attribute_values(cls.DEF_SIZE_ATTRIBUTE_ID, size_values)
        cls.create_attribute_values(cls.DEF_COLOR_ATTRIBUTE_ID, color_values)

    @classmethod
    def get_product_by_shopify_id(cls, shopify_product_ids):
        products = models.Product.objects.filter(
            metadata__shopifyid__in=shopify_product_ids
        )
        return products

    @classmethod
    def create_product_images(cls, products, product_images):
        for product in products:
            image_urls = product_images[product.id]
            create_product_images_from_url.delay(product.id, image_urls)

    @classmethod
    @transaction.atomic
    def create_products(cls, shopify_products):
        def_product_type = models.ProductType.objects.get(pk=cls.DEF_PRODUCT_TYPE_ID)
        def_category = models.Category.objects.get(pk=cls.DEF_PRODUCT_CATEGORY_ID)

        products = list()
        product_images = dict()
        for spa in shopify_products:
            new_product = models.Product.objects.create(
                name=spa.title,
                slug=slugify(spa.title + str(spa.id)),
                product_type=def_product_type,
                category=def_category,
                description=(spa.body_html if spa.body_html else ""),
                is_published=True,
                visible_in_listings=True,
                available_for_purchase=datetime.date.today(),
                metadata={"shopifyid": str(spa.id)}
            )

            cls.create_variants(new_product, spa)
            products.append(new_product)

            product_images[new_product.id] = []
            for image in spa.images:
                product_images[new_product.id].append(image.src)

        return products, product_images

    @classmethod
    @transaction.atomic
    def create_collection(cls, shopify_collection, products):
        all_collections = models.Collection.objects.all()
        collection_name = shopify_collection.title
        i = 1
        while True:
            try:
                lower_collection_name = collection_name.lower()
                collection = next(filter(
                    lambda c: c.name.lower() == lower_collection_name, all_collections.iterator()
                ))
            except StopIteration:
                collection = None

            if not collection:
                break
            i = i + 1
            collection_name = shopify_collection.title + "(" + str(i) + ")"

        new_collection = models.Collection.objects.create(
            name=collection_name,
            slug=slugify(collection_name),
            is_published=True,
            description=str(shopify_collection.body_html),
            metadata={"shopifyid": str(shopify_collection.id)}
        )

        collection_products = []
        for product in products:
            new_col_product = models.CollectionProduct(
                collection=new_collection,
                product=product
            )
            collection_products.append(new_col_product)
        models.CollectionProduct.objects.bulk_create(collection_products)
        cls.create_menu_item(new_collection)

    @classmethod
    def create_menu_item(cls, collection):
        menu = menu_models.Menu.objects.get(name="navbar")
        menu_models.MenuItem.objects.create(
            name=collection.name,
            menu=menu,
            collection=collection,
            parent_id=cls.DEF_MENU_ITEM_ID
        )

    @classmethod
    def perform_mutation(cls, root, info, **data):
        shopify = Shopify(data["shop_url"], data["access_token"])
        collection_id = data["collection_id"]
        shopify_collection = shopify.get_collection(collection_id)
        shopify_products = shopify.get_collection_products(collection_id)

        shopify_product_ids = list(map(lambda p: str(p.id), shopify_products))
        exist_products = cls.get_product_by_shopify_id(shopify_product_ids)
        exist_product_ids = list(map(lambda p: p.metadata["shopifyid"], exist_products))
        shopify_products = list(filter(
            lambda p: str(p.id) not in exist_product_ids, shopify_products
        ))

        cls.create_color_sizes(shopify_products)
        products, product_images = cls.create_products(shopify_products)
        cls.create_collection(shopify_collection, products + list(exist_products))
        cls.create_product_images(products, product_images)

        return cls(products=products)


class ProductBulkDelete(ModelBulkDeleteMutation):
    class Arguments:
        ids = graphene.List(
            graphene.ID, required=True, description="List of product IDs to delete."
        )

    class Meta:
        description = "Deletes products."
        model = models.Product
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"

    @classmethod
    def perform_mutation(cls, _root, info, ids, **data):
        _, pks = resolve_global_ids_to_primary_keys(ids, Product)
        variants = models.ProductVariant.objects.filter(product__pk__in=pks)
        # get draft order lines for products
        order_line_pks = list(
            order_models.OrderLine.objects.filter(
                variant__in=variants, order__status=OrderStatus.DRAFT
            ).values_list("pk", flat=True)
        )

        response = super().perform_mutation(_root, info, ids, **data)

        # delete order lines for deleted variants
        order_models.OrderLine.objects.filter(pk__in=order_line_pks).delete()

        return response


class ProductVariantBulkCreateInput(ProductVariantInput):
    attributes = graphene.List(
        AttributeValueInput,
        required=True,
        description="List of attributes specific to this variant.",
    )
    stocks = graphene.List(
        graphene.NonNull(StockInput),
        description=("Stocks of a product available for sale."),
        required=False,
    )
    sku = graphene.String(required=True, description="Stock keeping unit.")


class ProductVariantBulkCreate(BaseMutation):
    count = graphene.Int(
        required=True,
        default_value=0,
        description="Returns how many objects were created.",
    )
    product_variants = graphene.List(
        graphene.NonNull(ProductVariant),
        required=True,
        default_value=[],
        description="List of the created variants.",
    )

    class Arguments:
        variants = graphene.List(
            ProductVariantBulkCreateInput,
            required=True,
            description="Input list of product variants to create.",
        )
        product_id = graphene.ID(
            description="ID of the product to create the variants for.",
            name="product",
            required=True,
        )

    class Meta:
        description = "Creates product variants for a given product."
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = BulkProductError
        error_type_field = "bulk_product_errors"

    @classmethod
    def clean_variant_input(
        cls,
        info,
        instance: models.ProductVariant,
        data: dict,
        errors: dict,
        variant_index: int,
    ):
        cleaned_input = ModelMutation.clean_input(
            info, instance, data, input_cls=ProductVariantBulkCreateInput
        )

        cost_price_amount = cleaned_input.pop("cost_price", None)
        if cost_price_amount is not None:
            try:
                validate_price_precision(cost_price_amount)
            except ValidationError as error:
                error.code = ProductErrorCode.INVALID.value
                raise ValidationError({"cost_price": error})
            cleaned_input["cost_price_amount"] = cost_price_amount

        price_amount = cleaned_input.pop("price", None)
        if price_amount is not None:
            try:
                validate_price_precision(price_amount)
            except ValidationError as error:
                error.code = ProductErrorCode.INVALID.value
                raise ValidationError({"price": error})
            cleaned_input["price_amount"] = price_amount

        attributes = cleaned_input.get("attributes")
        if attributes:
            try:
                cleaned_input["attributes"] = ProductVariantCreate.clean_attributes(
                    attributes, data["product_type"]
                )
            except ValidationError as exc:
                exc.params = {"index": variant_index}
                errors["attributes"] = exc

        stocks = cleaned_input.get("stocks")
        if stocks:
            cls.clean_stocks(stocks, errors, variant_index)

        return cleaned_input

    @classmethod
    def clean_stocks(cls, stocks_data, errors, variant_index):
        warehouse_ids = [stock["warehouse"] for stock in stocks_data]
        duplicates = get_duplicated_values(warehouse_ids)
        if duplicates:
            errors["stocks"] = ValidationError(
                "Duplicated warehouse ID.",
                code=ProductErrorCode.DUPLICATED_INPUT_ITEM,
                params={"warehouses": duplicates, "index": variant_index},
            )

    @classmethod
    def add_indexes_to_errors(cls, index, error, error_dict):
        """Append errors with index in params to mutation error dict."""
        for key, value in error.error_dict.items():
            for e in value:
                if e.params:
                    e.params["index"] = index
                else:
                    e.params = {"index": index}
            error_dict[key].extend(value)

    @classmethod
    def save(cls, info, instance, cleaned_input):
        instance.save()

        attributes = cleaned_input.get("attributes")
        if attributes:
            AttributeAssignmentMixin.save(instance, attributes)
            instance.name = generate_name_for_variant(instance)
            instance.save(update_fields=["name"])

    @classmethod
    def create_variants(cls, info, cleaned_inputs, product, errors):
        instances = []
        for index, cleaned_input in enumerate(cleaned_inputs):
            if not cleaned_input:
                continue
            try:
                instance = models.ProductVariant()
                cleaned_input["product"] = product
                instance = cls.construct_instance(instance, cleaned_input)
                cls.clean_instance(info, instance)
                instances.append(instance)
            except ValidationError as exc:
                cls.add_indexes_to_errors(index, exc, errors)
        return instances

    @classmethod
    def validate_duplicated_sku(cls, sku, index, sku_list, errors):
        if sku in sku_list:
            errors["sku"].append(
                ValidationError(
                    "Duplicated SKU.", ProductErrorCode.UNIQUE, params={"index": index}
                )
            )
        sku_list.append(sku)

    @classmethod
    def clean_variants(cls, info, variants, product, errors):
        cleaned_inputs = []
        sku_list = []
        used_attribute_values = get_used_variants_attribute_values(product)
        for index, variant_data in enumerate(variants):
            try:
                ProductVariantCreate.validate_duplicated_attribute_values(
                    variant_data.attributes, used_attribute_values
                )
            except ValidationError as exc:
                errors["attributes"].append(
                    ValidationError(exc.message, exc.code, params={"index": index})
                )

            cleaned_input = None
            variant_data["product_type"] = product.product_type
            cleaned_input = cls.clean_variant_input(
                info, None, variant_data, errors, index
            )

            cleaned_inputs.append(cleaned_input if cleaned_input else None)

            if not variant_data.sku:
                continue
            cls.validate_duplicated_sku(variant_data.sku, index, sku_list, errors)
        return cleaned_inputs

    @classmethod
    @transaction.atomic
    def save_variants(cls, info, instances, product, cleaned_inputs):
        assert len(instances) == len(
            cleaned_inputs
        ), "There should be the same number of instances and cleaned inputs."
        for instance, cleaned_input in zip(instances, cleaned_inputs):
            cls.save(info, instance, cleaned_input)
            cls.create_variant_stocks(instance, cleaned_input)
        if not product.default_variant:
            product.default_variant = instances[0]
            product.save(update_fields=["default_variant", "updated_at"])

    @classmethod
    def create_variant_stocks(cls, variant, cleaned_input):
        stocks = cleaned_input.get("stocks")
        if not stocks:
            return
        warehouse_ids = [stock["warehouse"] for stock in stocks]
        warehouses = cls.get_nodes_or_error(
            warehouse_ids, "warehouse", only_type=Warehouse
        )
        create_stocks(variant, stocks, warehouses)

    @classmethod
    def perform_mutation(cls, root, info, **data):
        product = cls.get_node_or_error(info, data["product_id"], models.Product)
        errors = defaultdict(list)

        cleaned_inputs = cls.clean_variants(info, data["variants"], product, errors)
        instances = cls.create_variants(info, cleaned_inputs, product, errors)
        if errors:
            raise ValidationError(errors)
        cls.save_variants(info, instances, product, cleaned_inputs)

        # Recalculate the "minimal variant price" for the parent product
        update_product_minimal_variant_price_task.delay(product.pk)

        return ProductVariantBulkCreate(
            count=len(instances), product_variants=instances
        )


class ProductVariantBulkDelete(ModelBulkDeleteMutation):
    class Arguments:
        ids = graphene.List(
            graphene.ID,
            required=True,
            description="List of product variant IDs to delete.",
        )

    class Meta:
        description = "Deletes product variants."
        model = models.ProductVariant
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"

    @classmethod
    @transaction.atomic
    def perform_mutation(cls, _root, info, ids, **data):
        _, pks = resolve_global_ids_to_primary_keys(ids, ProductVariant)
        # get draft order lines for variants
        order_line_pks = list(
            order_models.OrderLine.objects.filter(
                variant__pk__in=pks, order__status=OrderStatus.DRAFT
            ).values_list("pk", flat=True)
        )

        product_pks = list(
            models.Product.objects.filter(variants__in=pks)
            .distinct()
            .values_list("pk", flat=True)
        )

        response = super().perform_mutation(_root, info, ids, **data)

        # delete order lines for deleted variants
        order_models.OrderLine.objects.filter(pk__in=order_line_pks).delete()

        # set new product default variant if any has been removed
        products = models.Product.objects.filter(
            pk__in=product_pks, default_variant__isnull=True
        )
        for product in products:
            product.default_variant = product.variants.first()
            product.save(update_fields=["default_variant"])

        return response


class ProductVariantStocksCreate(BaseMutation):
    product_variant = graphene.Field(
        ProductVariant, description="Updated product variant."
    )

    class Arguments:
        variant_id = graphene.ID(
            required=True,
            description="ID of a product variant for which stocks will be created.",
        )
        stocks = graphene.List(
            graphene.NonNull(StockInput),
            required=True,
            description="Input list of stocks to create.",
        )

    class Meta:
        description = "Creates stocks for product variant."
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = BulkStockError
        error_type_field = "bulk_stock_errors"

    @classmethod
    def perform_mutation(cls, root, info, **data):
        errors = defaultdict(list)
        stocks = data["stocks"]
        variant = cls.get_node_or_error(
            info, data["variant_id"], only_type=ProductVariant
        )
        if stocks:
            warehouses = cls.clean_stocks_input(variant, stocks, errors)
            if errors:
                raise ValidationError(errors)
            create_stocks(variant, stocks, warehouses)
        return cls(product_variant=variant)

    @classmethod
    def clean_stocks_input(cls, variant, stocks_data, errors):
        warehouse_ids = [stock["warehouse"] for stock in stocks_data]
        cls.check_for_duplicates(warehouse_ids, errors)
        warehouses = cls.get_nodes_or_error(
            warehouse_ids, "warehouse", only_type=Warehouse
        )
        existing_stocks = variant.stocks.filter(warehouse__in=warehouses).values_list(
            "warehouse__pk", flat=True
        )
        error_msg = "Stock for this warehouse already exists for this product variant."
        indexes = []
        for warehouse_pk in existing_stocks:
            warehouse_id = graphene.Node.to_global_id("Warehouse", warehouse_pk)
            indexes.extend(
                [i for i, id in enumerate(warehouse_ids) if id == warehouse_id]
            )
        cls.update_errors(
            errors, error_msg, "warehouse", StockErrorCode.UNIQUE, indexes
        )

        return warehouses

    @classmethod
    def check_for_duplicates(cls, warehouse_ids, errors):
        duplicates = {id for id in warehouse_ids if warehouse_ids.count(id) > 1}
        error_msg = "Duplicated warehouse ID."
        indexes = []
        for duplicated_id in duplicates:
            indexes.append(
                [i for i, id in enumerate(warehouse_ids) if id == duplicated_id][-1]
            )
        cls.update_errors(
            errors, error_msg, "warehouse", StockErrorCode.UNIQUE, indexes
        )

    @classmethod
    def update_errors(cls, errors, msg, field, code, indexes):
        for index in indexes:
            error = ValidationError(msg, code=code, params={"index": index})
            errors[field].append(error)


class ProductVariantStocksUpdate(ProductVariantStocksCreate):
    class Meta:
        description = "Update stocks for product variant."
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = BulkStockError
        error_type_field = "bulk_stock_errors"

    @classmethod
    def perform_mutation(cls, root, info, **data):
        errors = defaultdict(list)
        stocks = data["stocks"]
        variant = cls.get_node_or_error(
            info, data["variant_id"], only_type=ProductVariant
        )
        if stocks:
            warehouse_ids = [stock["warehouse"] for stock in stocks]
            cls.check_for_duplicates(warehouse_ids, errors)
            if errors:
                raise ValidationError(errors)
            warehouses = cls.get_nodes_or_error(
                warehouse_ids, "warehouse", only_type=Warehouse
            )
            cls.update_or_create_variant_stocks(variant, stocks, warehouses)
        return cls(product_variant=variant)

    @classmethod
    @transaction.atomic
    def update_or_create_variant_stocks(cls, variant, stocks_data, warehouses):
        stocks = []
        for stock_data, warehouse in zip(stocks_data, warehouses):
            stock, _ = warehouse_models.Stock.objects.get_or_create(
                product_variant=variant, warehouse=warehouse
            )
            stock.quantity = stock_data["quantity"]
            stocks.append(stock)
        warehouse_models.Stock.objects.bulk_update(stocks, ["quantity"])


class ProductVariantStocksDelete(BaseMutation):
    product_variant = graphene.Field(
        ProductVariant, description="Updated product variant."
    )

    class Arguments:
        variant_id = graphene.ID(
            required=True,
            description="ID of product variant for which stocks will be deleted.",
        )
        warehouse_ids = graphene.List(graphene.NonNull(graphene.ID),)

    class Meta:
        description = "Delete stocks from product variant."
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = StockError
        error_type_field = "stock_errors"

    @classmethod
    def perform_mutation(cls, root, info, **data):
        variant = cls.get_node_or_error(
            info, data["variant_id"], only_type=ProductVariant
        )
        _, warehouses_pks = resolve_global_ids_to_primary_keys(
            data["warehouse_ids"], Warehouse
        )
        warehouse_models.Stock.objects.filter(
            product_variant=variant, warehouse__pk__in=warehouses_pks
        ).delete()
        return cls(product_variant=variant)


class ProductTypeBulkDelete(ModelBulkDeleteMutation):
    class Arguments:
        ids = graphene.List(
            graphene.ID,
            required=True,
            description="List of product type IDs to delete.",
        )

    class Meta:
        description = "Deletes product types."
        model = models.ProductType
        permissions = (ProductTypePermissions.MANAGE_PRODUCT_TYPES_AND_ATTRIBUTES,)
        error_type_class = ProductError
        error_type_field = "product_errors"


class ProductImageBulkDelete(ModelBulkDeleteMutation):
    class Arguments:
        ids = graphene.List(
            graphene.ID,
            required=True,
            description="List of product image IDs to delete.",
        )

    class Meta:
        description = "Deletes product images."
        model = models.ProductImage
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"


class ProductBulkPublish(BaseBulkMutation):
    class Arguments:
        ids = graphene.List(
            graphene.ID, required=True, description="List of products IDs to publish."
        )
        is_published = graphene.Boolean(
            required=True, description="Determine if products will be published or not."
        )

    class Meta:
        description = "Publish products."
        model = models.Product
        permissions = (ProductPermissions.MANAGE_PRODUCTS,)
        error_type_class = ProductError
        error_type_field = "product_errors"

    @classmethod
    def bulk_action(cls, queryset, is_published):
        queryset.update(is_published=is_published)
