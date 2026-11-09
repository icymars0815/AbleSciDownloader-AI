"""POST route table (refactor stage 1). app.do_POST validates the request, then calls dispatch()."""
import api_intake
import api_items
import api_search
import api_settings
import api_system

MODULES = (api_search, api_intake, api_settings, api_items, api_system)
ROUTES = {}
for _module in MODULES:
    for _path, _route in _module.ROUTES.items():
        assert _path not in ROUTES, _path
        ROUTES[_path] = _route
PREFIX_ROUTES = [(p, r) for m in MODULES for p, r in m.PREFIX_ROUTES.items()]


def find(path):
    route = ROUTES.get(path)
    if route is None:
        route = next((r for p, r in PREFIX_ROUTES if path.startswith(p)), None)
    return route


def dispatch(c, h, path, body, origin):
    """Run the route for path. Returns False when there is none (the caller answers 404).
    A route that did not send a response itself gets the default {"ok": true}, as the old if/elif chain did."""
    route = find(path)
    if route is None:
        return False
    h.responded = False
    route(c, h, body, path, origin)
    if not h.responded:
        h.respond(200, {"ok": True})
    return True
