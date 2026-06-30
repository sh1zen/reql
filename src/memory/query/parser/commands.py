"""Command-level parsers for REQL statements."""
from __future__ import annotations

from ..ast import (
    Activate,
    CacheStatus,
    Communities,
    Condition,
    Explain,
    FindEdges,
    FindNodes,
    Hubs,
    Match,
    PathQuery,
    Retrieve,
    ReturnSpec,
    Search,
    SortSpec,
    Stats,
    TypedNodeList,
    VerifyFinding,
)
from ..errors import REQLSyntaxError
from .clauses import RESERVED_EXPRESSION_ENDS, SYMBOL_NODE_TYPES, ClauseParserMixin


class CommandParserMixin(ClauseParserMixin):
    """Parses top-level REQL commands after the command keyword has been consumed."""

    def _parse_find(self) -> FindNodes | FindEdges:
        if self.match_keyword("NODE") or self.match_keyword("NODES"):
            kind = "nodes"
        elif self.match_keyword("EDGE") or self.match_keyword("EDGES"):
            kind = "edges"
        else:
            raise REQLSyntaxError("FIND must be followed by NODE(S) or EDGE(S)")

        types: tuple[str, ...] = ()
        where: Condition | None = None
        returns = ReturnSpec()
        order_by: SortSpec | None = None
        limit = 50
        include_archived = False

        while self.current.kind != "EOF":
            if self.match_keyword("TYPE") or self.match_keyword("TYPES"):
                types = self._parse_ident_list()
            elif self.match_keyword("WHERE"):
                where = self._parse_condition_until(RESERVED_EXPRESSION_ENDS)
            elif self.match_keyword("RETURN"):
                returns = ReturnSpec(self._parse_return_fields())
            elif self.match_keyword("ORDER"):
                self.expect_keyword("BY")
                order_by = self._parse_order_by()
            elif self.match_keyword("LIMIT") or self.match_keyword("TOP"):
                limit = self._parse_positive_int("limit")
            elif self.match_keyword("INCLUDE"):
                self.expect_keyword("ARCHIVED")
                include_archived = True
            else:
                if not types and self.current.kind in {"IDENT", "KEYWORD"}:
                    types = self._parse_ident_list()
                else:
                    raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in FIND statement")

        if kind == "nodes":
            return FindNodes(types, where, returns, order_by, limit, include_archived)
        return FindEdges(types, where, returns, order_by, limit)

    def _parse_search(self) -> Search:
        text = self.expect_value()
        if not isinstance(text, str):
            raise REQLSyntaxError("SEARCH expects a string literal or text value")
        node_types: tuple[str, ...] = ()
        returns = ReturnSpec()
        top_k = 20
        depth = 3
        include_archived = False
        context = False
        while self.current.kind != "EOF":
            if self.match_keyword("TYPE") or self.match_keyword("TYPES"):
                node_types = self._parse_ident_list()
            elif self.match_keyword("TOP") or self.match_keyword("LIMIT"):
                top_k = self._parse_positive_int("top")
            elif self.match_keyword("DEPTH"):
                depth = self._parse_positive_int("depth")
            elif self.match_keyword("CONTEXT"):
                context = True
            elif self.match_keyword("INCLUDE"):
                self.expect_keyword("ARCHIVED")
                include_archived = True
            elif self.match_keyword("RETURN"):
                returns = ReturnSpec(self._parse_return_fields())
            else:
                raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in SEARCH statement")
        return Search(text=text, node_types=node_types, returns=returns, top_k=top_k, max_depth=depth, include_archived=include_archived, context=context)

    def _parse_retrieve(self) -> Retrieve:
        text = self.expect_value()
        if not isinstance(text, str):
            raise REQLSyntaxError("RETRIEVE expects a string literal or text value")
        node_types: tuple[str, ...] = ()
        returns = ReturnSpec()
        top_k = 12
        limit = 12
        depth = 2
        include_sources = True
        include_archived = False
        filter_generic = True
        max_text_chars = 600
        while self.current.kind != "EOF":
            if self.match_keyword("TYPE") or self.match_keyword("TYPES"):
                node_types = self._parse_ident_list()
            elif self.match_keyword("TOP"):
                top_k = self._parse_positive_int("top")
            elif self.match_keyword("LIMIT"):
                limit = self._parse_positive_int("limit")
            elif self.match_keyword("DEPTH"):
                depth = self._parse_positive_int("depth")
            elif self.match_keyword("INCLUDE"):
                if self.match_keyword("ARCHIVED"):
                    include_archived = True
                elif self.match_keyword("SOURCE") or self.match_keyword("SOURCES"):
                    include_sources = True
                else:
                    raise REQLSyntaxError("RETRIEVE INCLUDE expects ARCHIVED or SOURCES")
            elif self.match_keyword("NO"):
                if self.match_keyword("SOURCE") or self.match_keyword("SOURCES"):
                    include_sources = False
                elif self.match_keyword("FILTER"):
                    filter_generic = False
                else:
                    raise REQLSyntaxError("RETRIEVE NO expects SOURCES or FILTER")
            elif self.match_keyword("RETURN"):
                returns = ReturnSpec(self._parse_return_fields())
            elif self.match_keyword("MAX"):
                self.expect_keyword("TEXT")
                max_text_chars = self._parse_positive_int("max text chars")
            else:
                raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in RETRIEVE statement")
        return Retrieve(
            text=text,
            node_types=node_types,
            returns=returns,
            top_k=top_k,
            max_depth=depth,
            limit=limit,
            include_sources=include_sources,
            include_archived=include_archived,
            filter_generic=filter_generic,
            max_text_chars=max_text_chars,
        )

    def _parse_activate(self) -> Activate:
        if self.match_keyword("NODE") or self.match_keyword("NODES"):
            pass
        node_ids = self._parse_value_list_as_strings()
        returns = ReturnSpec()
        depth = 3
        min_activation = 0.03
        limit = 50
        while self.current.kind != "EOF":
            if self.match_keyword("DEPTH"):
                depth = self._parse_positive_int("depth")
            elif self.match_keyword("MIN"):
                value = self.expect_value()
                min_activation = float(value)
            elif self.match_keyword("LIMIT") or self.match_keyword("TOP"):
                limit = self._parse_positive_int("limit")
            elif self.match_keyword("RETURN"):
                returns = ReturnSpec(self._parse_return_fields())
            else:
                raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in ACTIVATE statement")
        return Activate(tuple(node_ids), returns, depth, min_activation, limit)

    def _parse_match(self) -> Match:
        pattern_tokens = self._collect_until({"WHERE", "RETURN", "ORDER", "LIMIT", "TOP"})
        pattern = self._join_pattern_tokens(pattern_tokens)
        left, edge, right = self._parse_pattern(pattern)
        where: Condition | None = None
        returns = ReturnSpec()
        order_by: SortSpec | None = None
        limit = 50
        while self.current.kind != "EOF":
            if self.match_keyword("WHERE"):
                where = self._parse_condition_until(RESERVED_EXPRESSION_ENDS)
            elif self.match_keyword("RETURN"):
                returns = ReturnSpec(self._parse_return_fields())
            elif self.match_keyword("ORDER"):
                self.expect_keyword("BY")
                order_by = self._parse_order_by()
            elif self.match_keyword("LIMIT") or self.match_keyword("TOP"):
                limit = self._parse_positive_int("limit")
            else:
                raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in MATCH statement")
        return Match(left=left, edge=edge, right=right, where=where, returns=returns, order_by=order_by, limit=limit)

    def _parse_path(self) -> PathQuery:
        self.expect_keyword("FROM")
        start = self._parse_selector()
        self.expect_keyword("TO")
        end = self._parse_selector()
        edge_types: tuple[str, ...] = ()
        returns = ReturnSpec()
        depth = 4
        limit = 10
        while self.current.kind != "EOF":
            if self.match_keyword("DEPTH"):
                depth = self._parse_positive_int("depth")
            elif self.match_keyword("VIA") or self.match_keyword("TYPE") or self.match_keyword("TYPES"):
                edge_types = self._parse_ident_list()
            elif self.match_keyword("LIMIT") or self.match_keyword("TOP"):
                limit = self._parse_positive_int("limit")
            elif self.match_keyword("RETURN"):
                returns = ReturnSpec(self._parse_return_fields())
            else:
                raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in PATH statement")
        return PathQuery(start=start, end=end, edge_types=edge_types, returns=returns, max_depth=depth, limit=limit)

    def _parse_explain(self) -> Explain:
        if self.match_keyword("HUB"):
            target = self.expect_value()
            return Explain(mode="hub", target=str(target))
        if self.match_keyword("NODE") or self.match_keyword("ID"):
            target = self.expect_value()
            if not isinstance(target, str):
                target = str(target)
            limit = 30
            while self.current.kind != "EOF":
                if self.match_keyword("LIMIT"):
                    limit = self._parse_positive_int("limit")
                else:
                    raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in EXPLAIN NODE statement")
            return Explain(mode="node", target=target, limit=limit)
        if self.match_keyword("SEARCH"):
            target = self.expect_value()
            if not isinstance(target, str):
                raise REQLSyntaxError("EXPLAIN SEARCH expects text")
            top_k = 10
            depth = 2
            while self.current.kind != "EOF":
                if self.match_keyword("TOP") or self.match_keyword("LIMIT"):
                    top_k = self._parse_positive_int("top")
                elif self.match_keyword("DEPTH"):
                    depth = self._parse_positive_int("depth")
                else:
                    raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in EXPLAIN SEARCH statement")
            return Explain(mode="search", target=target, top_k=top_k, max_depth=depth)
        target = self.expect_value()
        if not isinstance(target, str):
            target = str(target)
        return Explain(mode="search", target=target)

    def _parse_communities(self) -> Communities:
        limit = 20
        where: Condition | None = None
        order_by: SortSpec | None = None
        while self.current.kind != "EOF":
            if self.match_keyword("WHERE"):
                where = self._parse_condition_until({"ORDER", "LIMIT", "TOP"})
            elif self.match_keyword("ORDER"):
                self.expect_keyword("BY")
                order_by = self._parse_order_by()
            elif self.match_keyword("LIMIT") or self.match_keyword("TOP"):
                limit = self._parse_positive_int("limit")
            else:
                raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in COMMUNITIES statement")
        return Communities(where=where, order_by=order_by, limit=limit)

    def _parse_hubs(self) -> Hubs:
        node_types: tuple[str, ...] = ()
        where: Condition | None = None
        order_by: SortSpec | None = None
        limit = 20
        while self.current.kind != "EOF":
            if self.match_keyword("TYPE") or self.match_keyword("TYPES"):
                node_types = self._parse_ident_list()
            elif self.match_keyword("WHERE"):
                where = self._parse_condition_until({"ORDER", "LIMIT", "TOP"})
            elif self.match_keyword("ORDER"):
                self.expect_keyword("BY")
                order_by = self._parse_order_by()
            elif self.match_keyword("LIMIT") or self.match_keyword("TOP"):
                limit = self._parse_positive_int("limit")
            else:
                raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in HUBS statement")
        return Hubs(node_types=node_types, where=where, order_by=order_by, limit=limit)

    def _parse_typed_node_list(
        self,
        command: str,
        default_types: tuple[str, ...],
        *,
        allow_type: bool,
        limit: int = 20,
    ) -> TypedNodeList:
        node_types = default_types
        where: Condition | None = None
        returns = ReturnSpec()
        order_by: SortSpec | None = None
        while self.current.kind != "EOF":
            if allow_type and (self.match_keyword("TYPE") or self.match_keyword("TYPES")):
                requested = self._parse_ident_list()
                node_types = tuple(value for value in requested if value in SYMBOL_NODE_TYPES) or requested
            elif self.match_keyword("WHERE"):
                where = self._parse_condition_until({"RETURN", "ORDER", "LIMIT", "TOP"})
            elif self.match_keyword("RETURN"):
                returns = ReturnSpec(self._parse_return_fields())
            elif self.match_keyword("ORDER"):
                self.expect_keyword("BY")
                order_by = self._parse_order_by()
            elif self.match_keyword("LIMIT") or self.match_keyword("TOP"):
                limit = self._parse_positive_int("limit")
            else:
                raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in {command} statement")
        return TypedNodeList(command=command, node_types=node_types, where=where, returns=returns, order_by=order_by, limit=limit)

    def _parse_cache(self) -> CacheStatus:
        if self.match_keyword("STATUS"):
            limit = 20
            while self.current.kind != "EOF":
                if self.match_keyword("LIMIT") or self.match_keyword("TOP"):
                    limit = self._parse_positive_int("limit")
                else:
                    raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in CACHE STATUS statement")
            return CacheStatus(limit=limit)
        raise REQLSyntaxError(f"CACHE must be followed by STATUS at position {self.current.position}")

    def _parse_verify(self) -> VerifyFinding:
        self.expect_keyword("FINDING")
        finding_id = self._parse_rest_as_identifier("VERIFY FINDING")
        if not finding_id:
            raise REQLSyntaxError("VERIFY FINDING expects a finding id")
        return VerifyFinding(finding_id=finding_id)

    def _parse_stats(self) -> Stats:
        group_by: tuple[str, ...] = ()
        node_types: tuple[str, ...] = ()
        while self.current.kind != "EOF":
            if self.match_keyword("BY"):
                group_by = tuple(field.lower() for field in self._parse_field_list())
            elif self.match_keyword("TYPE") or self.match_keyword("TYPES"):
                node_types = self._parse_ident_list()
            else:
                raise REQLSyntaxError(f"Unexpected token {self.current.value!r} in STATS statement")
        return Stats(group_by=group_by, node_types=node_types)

    def _parse_rest_as_identifier(self, command: str) -> str:
        parts: list[str] = []
        while self.current.kind != "EOF":
            if self.current.kind == "STRING":
                value = self.current.value
            else:
                value = str(self.current.value)
            parts.append(value)
            self.advance()
        identifier = "".join(parts).strip()
        if any(ch.isspace() for ch in identifier):
            raise REQLSyntaxError(f"{command} id must be quoted when it contains whitespace")
        return identifier
