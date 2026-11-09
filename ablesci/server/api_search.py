"""Literature search and OpenAlex key routes (POST /api/...), moved unchanged from app.do_POST in refactor stage 1.
Each route(c, h, body, path, origin): c holds the server objects, h is the request handler; a route that does not
respond itself gets {"ok": true} (see api_routes.dispatch)."""
import os
import re


def route_literature_search(c, h, body, path, origin):
    openalex_vault = c.openalex_vault
    if body.get("source") == "pubmed":
        from ablesci.papers.literature_search import search_pubmed
        return h.respond(200, search_pubmed(body.get("query"), body.get("cursor", "*"), body.get("year_from"), body.get("year_to"),
                                               body.get("open_access", False), body.get("page_size", 50)))
    from ablesci.papers.literature_search import search_works
    key = openalex_vault.load() if openalex_vault.exists() else os.environ.get("OPENALEX_API_KEY", "")
    return h.respond(200, search_works(body.get("query"), body.get("cursor", "*"),
                                          body.get("year_from"), body.get("year_to"), body.get("open_access", False), body.get("page_size", 50), body.get("mode", "keyword"), key))


def route_openalex_key(c, h, body, path, origin):
    openalex_vault = c.openalex_vault
    key = body.get("key")
    if not isinstance(key, str) or len(key) > 256 or not key.strip() or re.search(r"\s", key):
        raise ValueError("请输入有效的 OpenAlex API Key")
    openalex_vault.save(key)
    return h.respond(200, {"ok": True, "saved": True})


def route_openalex_key_clear(c, h, body, path, origin):
    openalex_vault = c.openalex_vault
    openalex_vault.forget()
    return h.respond(200, {"ok": True, "saved": False})


ROUTES = {
    "/api/literature-search": route_literature_search,
    "/api/openalex-key": route_openalex_key,
    "/api/openalex-key-clear": route_openalex_key_clear,
}
PREFIX_ROUTES = {
}
