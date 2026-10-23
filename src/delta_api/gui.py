"""Optional anonymous reading through a separate GUI path. The API path stays authenticated."""
from starlette.responses import JSONResponse


class GuiReadOnly:
    """Alias /gui-api/ to existing readers. This path never permits a data mutation."""

    def __init__(self, app, *, enabled: bool):
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/gui-api/'):
            return await self.app(scope, receive, send)
        if not self.enabled:
            return await JSONResponse({'detail': 'Anonymous GUI browsing is not enabled.'}, status_code=404)(scope, receive, send)
        path = '/api/' + scope['path'][len('/gui-api/'):]
        if scope['method'] not in ('GET', 'HEAD') and not (scope['method'] == 'POST' and path == '/api/runs/estimate'):
            return await JSONResponse({'detail': 'Sign in to start searches or save records.'}, status_code=403)(scope, receive, send)
        updated = dict(scope, path=path, raw_path=path.encode(), gui_read=True)
        return await self.app(updated, receive, send)
