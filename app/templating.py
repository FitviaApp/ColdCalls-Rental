"""
Compatibility wrapper for template rendering across Starlette versions.
"""
from fastapi.templating import Jinja2Templates as FastAPIJinja2Templates


class Jinja2Templates(FastAPIJinja2Templates):
    def TemplateResponse(self, *args, **kwargs):  # noqa: N802 - keep Starlette API name
        """
        Support both legacy `TemplateResponse(name, context, ...)` and newer
        `TemplateResponse(request, name, context, ...)` call styles.
        """
        if args and isinstance(args[0], str):
            name = args[0]
            context = args[1] if len(args) > 1 else kwargs.pop("context", None)
            if not isinstance(context, dict):
                raise TypeError("Legacy TemplateResponse usage requires a dict context.")

            request = context.get("request") or kwargs.pop("request", None)
            if request is None:
                raise ValueError("Template context must include 'request'.")

            return super().TemplateResponse(
                request,
                name,
                context,
                status_code=kwargs.pop("status_code", 200),
                headers=kwargs.pop("headers", None),
                media_type=kwargs.pop("media_type", None),
                background=kwargs.pop("background", None),
            )

        return super().TemplateResponse(*args, **kwargs)
