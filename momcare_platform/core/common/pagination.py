from __future__ import annotations

from collections import OrderedDict
from typing import Any

from rest_framework.filters import OrderingFilter
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class DefaultPagination(PageNumberPagination):
    """The single pagination implementation for every list endpoint.

    Emits more than DRF's stock envelope: the frontend needs ``total_pages``
    and the echoed ``page``/``page_size`` so the UI can trust the server's
    interpretation of the query rather than its own optimistic copy.

    ``page_size`` is client-controllable but capped — an uncapped page size
    turns one request into a full-table scan.
    """

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data: Any) -> Response:
        return Response(
            OrderedDict(
                [
                    ("count", self.page.paginator.count),
                    ("page", self.page.number),
                    ("page_size", self.get_page_size(self.request)),
                    ("total_pages", self.page.paginator.num_pages),
                    ("next", self.get_next_link()),
                    ("previous", self.get_previous_link()),
                    ("results", data),
                ]
            )
        )

    def get_paginated_response_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Keep /api/docs/ honest — drf-spectacular documents the stock envelope
        unless we describe the real one."""
        return {
            "type": "object",
            "required": ["count", "page", "page_size", "total_pages", "results"],
            "properties": {
                "count": {"type": "integer", "example": 130},
                "page": {"type": "integer", "example": 3},
                "page_size": {"type": "integer", "example": 25},
                "total_pages": {"type": "integer", "example": 6},
                "next": {"type": "string", "nullable": True, "format": "uri"},
                "previous": {"type": "string", "nullable": True, "format": "uri"},
                "results": schema,
            },
        }


class StableOrderingFilter(OrderingFilter):
    """``OrderingFilter`` that always terminates in a unique key.

    Any user-selected ordering column may contain duplicates or NULLs.
    Appending ``id`` makes the sort a total order, which page-number
    pagination requires to avoid repeating or dropping rows across pages.
    """

    def get_ordering(self, request, queryset, view) -> list[str]:
        ordering = list(super().get_ordering(request, queryset, view) or [])
        if not any(field.lstrip("-") == "id" for field in ordering):
            ordering.append("id")
        return ordering
