import re
import shopify

from django.core.exceptions import ValidationError, ImproperlyConfigured
from ...product.error_codes import ProductErrorCode


class Shopify:
    API_KEY = "1140ed801c09cd500756cf467191814c"
    API_PASSWORD = "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"
    API_VERSION = "2021-07"

    def __init__(self, shop_url):
        self.has_init = False

        match = re.match(r"https?://(.*?)/?$", shop_url)
        if not match:
            raise ValidationError(
                "invalid shop url", code=ProductErrorCode.INVALID.value,
            )

        shop_url = "https://%s:%s@%s/admin/api/%s" % (
            self.API_KEY,
            self.API_PASSWORD,
            match.group(1),
            self.API_VERSION
        )
        shopify.ShopifyResource.set_site(shop_url)
        shop = shopify.Shop.current()
        if shop:
            self.has_init = True

    def get_collection_products(self, collection_id):
        if not self.has_init:
            raise ImproperlyConfigured("shopify has not init")

        products = shopify.Product.find(collection_id=collection_id)
        return products

    def get_collection(self, collection_id):
        if not self.has_init:
            raise ImproperlyConfigured("shopify has not init")

        return shopify.CollectionListing.find(id_=collection_id)
