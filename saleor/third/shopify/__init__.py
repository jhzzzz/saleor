import re
import shopify

from django.core.exceptions import ValidationError, ImproperlyConfigured
from ...product.error_codes import ProductErrorCode


class Shopify:
    API_VERSION = "2021-04"
    session = None

    def __init__(self, shop_url, access_token):
        match = re.match(r"https?://(.*?)/?$", shop_url)
        if not match:
            raise ValidationError(
                "invalid shop url", code=ProductErrorCode.INVALID.value,
            )

        if not access_token:
            raise ValidationError(
                "invalid access token", code=ProductErrorCode.INVALID.value,
            )

        self.session = shopify.Session(shop_url, self.API_VERSION, access_token)
        shopify.ShopifyResource.activate_session(self.session)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.session:
            shopify.ShopifyResource.clear_session()

    def get_collection_products(self, collection_id):
        if not self.session:
            raise ImproperlyConfigured("shopify has not init")

        products = shopify.Product.find(collection_id=collection_id)
        return products

    def get_collection(self, collection_id):
        if not self.session:
            raise ImproperlyConfigured("shopify has not init")

        return shopify.CollectionListing.find(id_=collection_id)
