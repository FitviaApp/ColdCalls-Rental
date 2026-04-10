"""
Template helpers compatible with both old and new Starlette signatures.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi.templating import Jinja2Templates as FastAPIJinja2Templates


class Jinja2Templates(FastAPIJinja2Templates):
    """
    Accept both of these call styles:

    - TemplateResponse("template.html", {"request": request, ...})
    - TemplateResponse(request=request, name="template.html", context={...})
    """

    def TemplateResponse(self, *args: Any, **kwargs: Any):  # noqa: N802
        if args and isinstance(args[0], str):
            name = args[0]
            context = args[1] if len(args) > 1 else kwargs.pop("context", {})
            if not isinstance(context, Mapping):
                raise TypeError("Template context must be a mapping")

            request = kwargs.pop("request", None) or context.get("request")
            if request is None:
                raise ValueError("Template context must include 'request'")

            return super().TemplateResponse(
                request=request,
                name=name,
                context=dict(context),
                **kwargs,
            )

        return super().TemplateResponse(*args, **kwargs)
