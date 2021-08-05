import logging
import urllib

from django.core.files.uploadedfile import SimpleUploadedFile

from ..celeryconf import app
from ..core.utils import create_thumbnails
from .models import Category, Collection, Product, ProductImage

logger = logging.getLogger(__name__)


@app.task
def create_product_thumbnails(image_id: str):
    """Take a ProductImage model and create thumbnails for it."""
    create_thumbnails(pk=image_id, model=ProductImage, size_set="products")


@app.task
def create_category_background_image_thumbnails(category_id: str):
    """Take a Product model and create the background image thumbnails for it."""
    create_thumbnails(
        pk=category_id,
        model=Category,
        size_set="background_images",
        image_attr="background_image",
    )


@app.task
def create_collection_background_image_thumbnails(collection_id: str):
    """Take a Collection model and create the background image thumbnails for it."""
    create_thumbnails(
        pk=collection_id,
        model=Collection,
        size_set="background_images",
        image_attr="background_image",
    )


@app.task
def create_product_images_from_url(product_id, image_urls):
    product = Product.objects.get(pk=product_id)
    index = 1
    for image_url in image_urls:
        # url_path = urllib.parse.urlparse(image_url).path
        # ext = os.path.splitext(url_path)[1]
        # img_path = os.path.join("/", "%s_%d%s" % (product.slug, index, ext))

        try:
            image_bytes = urllib.request.urlopen(image_url).read()
            image = SimpleUploadedFile(product.slug + ".jpg", image_bytes, "image/png")
        except Exception as e:
            logger.exception("Unable to download image: " + image_url, e)
            continue

        product_image = product.images.create(image=image, alt="")
        create_product_thumbnails(product_image.pk)
        index = index + 1
