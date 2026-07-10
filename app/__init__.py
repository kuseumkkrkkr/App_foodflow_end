from .core import app
from .extra_data import register_extra_routes

register_extra_routes(app)

__all__ = ["app"]
